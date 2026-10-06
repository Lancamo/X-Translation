#!/usr/bin/env python3
"""
抽取「翻译单元」(TU)：把 PyMuPDF 的碎块按段落重新聚合。

规则
----
1. 同一块内，垂直方向重叠的行 = 并排（左右分栏/编号+标题），不合并。
2. 垂直相邻且 字体/字号/颜色 一致、行距正常 的行 = 同一段落，合并。
3. 对齐方式由各行左右边界**粗判**（左/右/居中）。
   ⚠️ 这只是第一遍估计，**必须**再跑 22_fix_align.py 精判：
   本步对单行单元一律给 left、多行用固定 1.5pt 阈值，两类都会判错（见该脚本 docstring）。
4. 纯数字/符号单元标记为 passthrough，不翻译不重排。
"""
import json
import re
import unicodedata
import pymupdf
import os
from pathlib import Path
BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()

SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))
OUT = BASE / "work"

LIGATURES = {
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl",
    "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
}


def norm(s: str) -> str:
    for k, v in LIGATURES.items():
        s = s.replace(k, v)
    s = s.replace("\u00ad", "")          # soft hyphen
    s = unicodedata.normalize("NFC", s)
    return s


def v_overlap(a, b) -> bool:
    """两行垂直方向大幅重叠 → 视为并排（同一行上的不同栏）。

    阈值取 0.6：大字号标题的字框会自然超出行距（如 24pt 标题溢出约 10%），
    真正的并排项（编号 + 标题）重叠接近 100%。
    """
    top = max(a[1], b[1]); bot = min(a[3], b[3])
    ov = bot - top
    if ov <= 0:
        return False
    return ov > 0.6 * min(a[3] - a[1], b[3] - b[1])


def x_overlap_ratio(a, b) -> float:
    """水平投影重叠比例（相对较窄者）。同栏 >0，分栏 ≈0。"""
    l = max(a[0], b[0]); r = min(a[2], b[2])
    ov = r - l
    if ov <= 0:
        return 0.0
    return ov / max(1e-6, min(a[2] - a[0], b[2] - b[0]))


def line_rec(l, block_bbox):
    spans = [s for s in l["spans"] if s["text"].strip()]
    if not spans:
        return None
    # 以字符数最多的 span 定义样式
    dom = max(spans, key=lambda s: len(s["text"]))
    txt = norm("".join(s["text"] for s in spans))
    return {
        "bbox": [round(v, 2) for v in l["bbox"]],
        "text": txt,
        "font": dom["font"],
        "size": round(dom["size"], 2),
        "color": dom["color"],
        "nspans": len(spans),
        # 首行基线 y：中文回填时按基线锚定，避免字体上下留白差异导致视觉错位
        "baseline": round(spans[0]["origin"][1], 2),
        "x_origin": round(spans[0]["origin"][0], 2),
    }


def build_units(page, pno):
    d = page.get_text("dict")
    lines = []
    for b in d["blocks"]:
        if b["type"] == 1:
            continue
        for l in b.get("lines", []):
            r = line_rec(l, b["bbox"])
            if r:
                r["dir"] = tuple(round(v, 3) for v in l.get("dir", (1, 0)))
                lines.append(r)

    # 丢弃旋转文本（本刊实测无，保守跳过）
    rot = [l for l in lines if abs(l["dir"][0] - 1) > 1e-3]
    if rot:
        print(f"  !! page {pno} rotated lines: {len(rot)}")

    # 按 y 排序，再按「并排」拆列
    lines.sort(key=lambda l: (round(l["bbox"][1], 1), l["bbox"][0]))

    # 分组成段落。
    # 注意：必须与「所有仍开放的段落组」比较，不能只比上一行——
    # 双栏版面上左右栏的行在排序后是交错的，只比上一行会把两栏都切碎。
    groups = []
    for l in lines:
        best, best_gap = None, None
        for g in groups:
            prev = g["last"]
            if not (prev["font"] == l["font"]
                    and abs(prev["size"] - l["size"]) < 0.15
                    and prev["color"] == l["color"]):
                continue
            if x_overlap_ratio(prev["bbox"], l["bbox"]) <= 0.3:
                continue
            if v_overlap(prev["bbox"], l["bbox"]):
                continue
            h_prev = prev["bbox"][3] - prev["bbox"][1]
            gap = l["bbox"][1] - prev["bbox"][3]
            if not (-0.35 * h_prev <= gap <= max(0.55 * h_prev, 5.0)):
                continue
            if best is None or abs(gap) < abs(best_gap):
                best, best_gap = g, gap
        if best is None:
            groups.append({"lines": [l], "last": l})
        else:
            best["lines"].append(l)
            best["last"] = l

    # 按版面位置排序，保证 id 稳定
    groups.sort(key=lambda g: (round(min(x["bbox"][1] for x in g["lines"]), 1),
                               min(x["bbox"][0] for x in g["lines"])))
    units = [g["lines"] for g in groups]

    out = []
    for ui, grp in enumerate(units):
        xs0 = [g["bbox"][0] for g in grp]
        xs1 = [g["bbox"][2] for g in grp]
        cx = [(g["bbox"][0] + g["bbox"][2]) / 2 for g in grp]
        if len(grp) == 1:
            align = "left"
        elif max(xs0) - min(xs0) < 1.5:
            align = "left"
        elif max(xs1) - min(xs1) < 1.5:
            align = "right"
        elif max(cx) - min(cx) < 1.5:
            align = "center"
        else:
            align = "left"

        # 拼接文本
        parts = []
        for i, g in enumerate(grp):
            t = g["text"].strip()
            if i and parts and parts[-1].endswith("-"):
                parts[-1] = parts[-1][:-1] + t
            else:
                parts.append(t)
        text = " ".join(parts).strip()
        text = re.sub(r"\s+", " ", text)

        bbox = [min(g["bbox"][0] for g in grp), min(g["bbox"][1] for g in grp),
                max(g["bbox"][2] for g in grp), max(g["bbox"][3] for g in grp)]

        lh = None
        if len(grp) > 1:
            lh = round((grp[-1]["bbox"][1] - grp[0]["bbox"][1]) / (len(grp) - 1), 2)

        dom = max(grp, key=lambda g: len(g["text"]))
        out.append({
            "id": f"p{pno:02d}u{ui:02d}",
            "page": pno,
            "bbox": [round(v, 2) for v in bbox],
            "baseline": grp[0]["baseline"],
            "x_origin": grp[0]["x_origin"],
            "lines": [[round(v, 2) for v in g["bbox"]] for g in grp],
            "text": text,
            "font": dom["font"],
            "size": dom["size"],
            "color": dom["color"],
            "align": align,
            "lineheight": lh,
            "nlines": len(grp),
        })
    return out


doc = pymupdf.open(SRC)
all_units = []
for pno in range(doc.page_count):
    all_units += build_units(doc[pno], pno)
doc.close()

# 标记 passthrough（无字母 → 不需翻译）
for u in all_units:
    u["passthrough"] = not re.search(r"[A-Za-z]", u["text"])

with open(f"{OUT}/units.json", "w", encoding="utf-8") as f:
    json.dump(all_units, f, ensure_ascii=False, indent=1)

tr = [u for u in all_units if not u["passthrough"]]
print("total units:", len(all_units), "| to translate:", len(tr),
      "| passthrough:", len(all_units) - len(tr))
print("chars to translate:", sum(len(u["text"]) for u in tr))

# 字体使用统计
import collections
fc = collections.Counter((u["font"], u["size"]) for u in tr)
print("\n-- font/size of translatable units --")
for (fn, sz), c in fc.most_common(30):
    print(f"  {c:>4}  {fn:<28} {sz}")
