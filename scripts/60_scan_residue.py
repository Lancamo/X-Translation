#!/usr/bin/env python3
"""60_scan_residue.py — 成品残迹扫描：找出译后 PDF 里仍能读到的英文，并按**成因**归类。

为什么要"反向验证"：靠翻自己的处理清单来找漏项，只会找到"我知道自己没做的事"。
真正的残迹要用成品本身说话 —— 重新渲染 + OCR，把成品里仍读得出的英文全部捞出来，
再回溯它在**源 OCR** 里有没有对应块、在**覆盖计划**里有没有对应条目，从而判因。

分类（按优先级）：
  keep      有意保留：品牌 / 代码 / 数字刻度 / 页脚装饰字
  textlayer 成品文字层本就带这段英文（专有名词、网址）
  covered   源块进了覆盖计划，但中文比英文短 —— 英文从旁边露出来
  filtered  源块被过滤规则挡掉（过小 / 数字 / 装饰 / 标 `=` 保留）
  unseen    源 OCR 根本没读到该块（典型：竖排轴标题）→ 真正的"看不见的漏"
  noise     纯 OCR 误读（把中文认成拉丁字母，置信度通常 ≤0.5）

用法：
  python 60_scan_residue.py --pdf output/xxx_v1.1.pdf --ocr work/ocr_raw_v11.json
输出：work/residue/residue.json + report.txt
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = BASE / "work"
OUT = WORK / "residue"

DECOR = re.compile(os.environ.get("XTRANS_DECOR", r"^AI\s*[6OG&]\s*Z?\s*GROWTH$"), re.IGNORECASE)
NUMERIC = re.compile(r"^[\s0-9$%¢€£¥.,+\-–—/×x:*()\[\]<>|~='\"#&°]+$")
ASCII_WORD = re.compile(r"[A-Za-z]{2,}")


def norm(s):
    return re.sub(r"\s+", "", s).lower()


def overlap_ratio(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / max(small, 1e-6)


def is_english(text, conf):
    """够“像英文”才算：整串 ASCII 可打印 + 字母占比 ≥0.6 + 置信度 ≥0.5。

    中文被误读成拉丁字母时，串里几乎必然混入西里尔 / 符号（'RTШНУDA'），
    且 Vision 置信度稳定落在 0.3 —— 两道闸即可滤掉绝大部分假阳性。
    """
    t = text.strip()
    if len(t) < 3 or re.search(r"[^\x20-\x7E]", t):
        return False
    if conf is not None and conf < 0.5:
        return False
    letters = sum(c.isalpha() for c in t)
    return letters >= 3 and letters / len(t) >= 0.6 and bool(ASCII_WORD.search(t))


def load_queue():
    """L2 工作单 + 译文 -> (原文集合, 标 `=` 的原文集合)"""
    q = WORK / "l2_queue"
    row = re.compile(r"^(\d{4})\t\[[^\]]*\]\t(.*)$")
    units = {}
    for f in sorted(q.glob("batch_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = row.match(line)
            if m:
                units[int(m.group(1))] = m.group(2).strip()
    keep = set()
    for f in sorted(q.glob("trans_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if "\t" not in line:
                continue
            g, v = line.split("\t", 1)
            if g.strip().isdigit() and v.strip() == "=" and int(g) in units:
                keep.add(norm(units[int(g)]))
    return keep


def text_layer_words(doc):
    out = defaultdict(set)
    for pno in range(doc.page_count):
        for b in doc[pno].get_text("dict")["blocks"]:
            if b["type"] == 1:
                continue
            for line in b.get("lines", []):
                for span in line["spans"]:
                    for w in ASCII_WORD.findall(span["text"]):
                        out[pno + 1].add(w.lower())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="output/product.pdf")
    ap.add_argument("--ocr", default="work/ocr_raw_v11.json")
    ap.add_argument("--scope", default="work/ocr_scope4.json")
    ap.add_argument("--plan", default="work/l2_queue/overlay_plan_l2.json")
    ap.add_argument("--min-h", type=float, default=5.0)
    args = ap.parse_args()

    def load(p):
        return json.loads((BASE / p).read_text(encoding="utf-8"))

    doc = pymupdf.open(BASE / args.pdf)
    ocr_raw, scope, plan = load(args.ocr), load(args.scope), load(args.plan)
    keep_set = load_queue()
    tl_words = text_layer_words(doc)

    rects = [doc[i].rect for i in range(doc.page_count)]

    def to_pt(o):
        pno = int(re.search(r"p(\d+)", o["file"]).group(1))
        if not (1 <= pno <= doc.page_count):
            return None
        r = rects[pno - 1]
        return {"page": pno, "conf": float(o.get("conf", 0)), "text": o["text"],
                "bbox": [o["x"] * r.width, o["y"] * r.height,
                         (o["x"] + o["w"]) * r.width, (o["y"] + o["h"]) * r.height]}

    final_blocks = [b for b in (to_pt(o) for o in ocr_raw)
                    if b and is_english(b["text"], b["conf"])]

    # 源侧索引：页 -> 块。池子 = 主扫描 + 竖排补扫（竖排是主扫描的已知盲区）
    src_by_page = defaultdict(list)
    for x in scope["in_image"]:
        src_by_page[x["page"]].append(x)
    vert_path = WORK / "ocr_vertical.json"
    if vert_path.exists():
        for v in json.loads(vert_path.read_text(encoding="utf-8")):
            src_by_page[v["page"]].append(
                {"page": v["page"], "bbox": v["bbox"], "text": v["text"],
                 "conf": v["conf"], "cls": "vertical"})
    # 覆盖计划没有 src 字段，只能按 bbox 判定「这处是否已被覆盖」
    plan_by_page = defaultdict(list)
    for it in plan["items"]:
        plan_by_page[it["page"]].append(it["bbox"])

    rows = []
    for b in final_blocks:
        t = b["text"].strip()
        nt = norm(t)
        page = b["page"]
        h = b["bbox"][3] - b["bbox"][1]
        cls, detail = "residual", ""

        if nt in keep_set or DECOR.match(t) or NUMERIC.match(t):
            cls = "keep"
        elif any(w in tl_words.get(page, set()) for w in ASCII_WORD.findall(t) if len(w) >= 3):
            cls = "textlayer"
        else:
            # 该处是否落在某个已应用的补丁范围内
            cov = max((overlap_ratio(b["bbox"], pb) for pb in plan_by_page.get(page, [])),
                      default=0.0)
            # 回溯源侧：找同页、文本互相包含的块
            hit = None
            for x in src_by_page.get(page, []):
                nx = norm(x["text"])
                if len(nx) < 6:
                    continue
                if nx == nt or nx in nt or nt in nx:
                    hit = x
                    break
            if cov >= 0.5:
                cls = "covered"
                detail = f"补丁已应用(重合 {cov:.2f})，英文仍可读"
            elif hit is None:
                cls = "unseen"
                detail = "源 OCR 未检出（常见于竖排文字）"
            else:
                nx = norm(hit["text"])
                xh = hit["bbox"][3] - hit["bbox"][1]
                cls = "filtered"
                why = []
                if hit.get("cls") == "skip_numeric":
                    why.append("数字")
                if DECOR.match(hit["text"].strip()):
                    why.append("装饰")
                if xh < args.min_h:
                    why.append(f"过小 {xh:.1f}pt")
                if nx in keep_set:
                    why.append("标 `=` 保留")
                detail = "/".join(why) or "未进入计划"
        rows.append({"page": page, "bbox": [round(v, 1) for v in b["bbox"]], "h": round(h, 1),
                     "w": round(b["bbox"][2] - b["bbox"][0], 1),
                     "text": t, "cls": cls, "detail": detail, "conf": round(b["conf"], 2)})

    # 去掉被更长残迹完全包住的重复项
    rows.sort(key=lambda r: (r["page"], r["bbox"][1], r["bbox"][0], -len(r["text"])))
    dedup = []
    for r in rows:
        if any(r["page"] == a["page"] and r["text"] in a["text"] and r is not a
               for a in dedup):
            continue
        dedup.append(r)
    rows = dedup

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "residue.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                      encoding="utf-8")

    cnt = Counter(r["cls"] for r in rows)
    lines = [f"成品          : {Path(args.pdf).name}  "
             f"({(BASE/args.pdf).stat().st_size/1e6:.2f} MB, {doc.page_count} 页)",
             f"成品中「英文感」块: {len(rows)}", ""]
    for k, label in (("keep", "有意保留（品牌/代码/刻度/装饰）"), ("textlayer", "文字层自带（专有名词）"),
                     ("covered", "已覆盖但英文仍露出"), ("filtered", "被过滤规则挡掉"),
                     ("unseen", "源 OCR 未检出（真漏）"), ("residual", "其他")):
        if cnt.get(k):
            lines.append(f"  {label:<26}: {cnt[k]}")
    lines.append("")
    for k in ("unseen", "covered", "filtered"):
        grp = [r for r in rows if r["cls"] == k and r["h"] >= args.min_h]
        if not grp:
            continue
        lines.append(f"=== [{k}] 高度 ≥{args.min_h}pt 共 {len(grp)} 处，涉及 "
                     f"{len({r['page'] for r in grp})} 页 ===")
        for r in sorted(grp, key=lambda r: -r["h"])[:40]:
            lines.append(f"  p{r['page']:<3} {r['w']:>6.1f}x{r['h']:<5.1f} {r['detail']:<14} "
                         f"{r['text'][:62]!r}")
        lines.append("")
    (OUT / "report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
