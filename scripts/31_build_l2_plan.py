#!/usr/bin/env python3
"""31_build_l2_plan.py — 把译文与 OCR 位置合成 overlay plan（供 40_apply_overlay.py 消费）。

输入：
  work/ocr_scope4.json          图内文字块（含 bbox）
  work/l2_queue/batch_NN.txt    工作单（gid -> 原文）
  work/l2_queue/trans_NN.txt    译文（gid -> 中文 / `=` 表示保留原文）
输出：
  work/l2_queue/overlay_plan_l2.json
  work/l2_queue/align_report.txt

处理要点：
  1. 过滤规则必须与 30_build_l2_queue.py 完全一致（否则译文对不上）；
  2. **重叠去重**：同一位置 OCR 常出多条（如长句与它的前半段），重叠过高会叠字，只保留文本更长的一条；
  3. **竖排判定**：窄高块按 rotate=90 处理，避免横排中文挤出框外。

用法：python 31_build_l2_plan.py --source work/stage/V1.1_L1.pdf \
        --output output/product.pdf
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
QUEUE = BASE / "work" / "l2_queue"
SCOPE = BASE / "work" / "ocr_scope4.json"

DECORATION = re.compile(os.environ.get("XTRANS_DECOR", r"^AI\s*[6OG&]\s*Z?\s*GROWTH$"), re.IGNORECASE)
CJK = re.compile(r"[\u4e00-\u9fff]")

GID_RE = re.compile(r"^(\d{4})\t")
BATCH_ROW = re.compile(r"^(\d{4})\t\[[^\]]*\]\t(.*)$")


def load_units():
    """batch_NN.txt -> {gid: text}"""
    units = {}
    for f in sorted(QUEUE.glob("batch_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = BATCH_ROW.match(line)
            if m:
                units[int(m.group(1))] = m.group(2).strip()
    return units


def load_trans():
    """trans_NN.txt -> {gid: zh}"""
    out = {}
    for f in sorted(QUEUE.glob("trans_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            if "\t" not in line:
                print(f"  [warn] 无制表符，跳过: {line[:50]!r}")
                continue
            gid_s, zh = line.split("\t", 1)
            try:
                gid = int(gid_s.strip())
            except ValueError:
                continue
            out[gid] = zh.strip()
    return out


def overlap(a, b):
    """两个 bbox 的交面积 / 较小者面积"""
    x0 = max(a[0], b[0]); y0 = max(a[1], b[1])
    x1 = min(a[2], b[2]); y1 = min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    sa = (a[2] - a[0]) * (a[3] - a[1])
    sb = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(min(sa, sb), 1e-6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="work/stage/V1.1_L1.pdf")
    ap.add_argument("--output", default="output/product.pdf")
    ap.add_argument("--min-h", type=float, default=5.0)
    ap.add_argument("--dup-threshold", type=float, default=0.55)
    args = ap.parse_args()

    units = load_units()
    trans = load_trans()

    missing = [g for g in units if g not in trans]
    extra = [g for g in trans if g not in units]
    keep_map = {units[g]: trans[g] for g in units if g in trans}
    to_translate = {t: zh for t, zh in keep_map.items() if zh and zh != "="}

    print(f"工作单 {len(units)} 条 / 译文 {len(trans)} 条 / 缺译 {len(missing)} / 多余 {len(extra)}")
    if missing:
        print("  缺译 gid:", missing[:20])
    if extra:
        print("  多余 gid:", extra[:20])

    # 与 73 一致的过滤
    items_in = json.loads(SCOPE.read_text(encoding="utf-8"))["in_image"]
    # 竖排补扫结果合并进候选池（主扫描对旋转文字漏检，见 13_scan_vertical.py）。
    # 格式归一后与主扫描同一套过滤规则处理，重复的由下面的重叠去重兜住。
    vert_path = BASE / "work" / "ocr_vertical_new.json"
    if vert_path.exists():
        for v in json.loads(vert_path.read_text(encoding="utf-8")):
            items_in.append({"page": v["page"], "bbox": v["bbox"], "text": v["text"],
                             "conf": v.get("conf", 1.0), "cls": "label"})
        print(f"  已并入竖排补扫 {len(json.loads(vert_path.read_text(encoding='utf-8')))} 条")
    kept = []
    for x in items_in:
        t = x["text"].strip()
        if not t or x["cls"] == "skip_numeric":
            continue
        if DECORATION.match(t):
            continue
        if (x["bbox"][3] - x["bbox"][1]) < args.min_h:
            continue
        if CJK.search(t) and not re.search(r"[A-Za-z]{3,}", t):
            continue
        zh = to_translate.get(t)
        if not zh:
            continue                     # 标 `=` 或未译 -> 不动原图
        kept.append({"page": x["page"], "bbox": list(x["bbox"]), "zh": zh,
                     "src": t, "h": round(x["bbox"][3] - x["bbox"][1], 1),
                     "w": round(x["bbox"][2] - x["bbox"][0], 1), "conf": x["conf"]})

    # 重叠去重（同页内，保留原文更长的）
    by_page = defaultdict(list)
    for k in kept:
        by_page[k["page"]].append(k)
    final, dropped = [], 0
    for pno, rows in by_page.items():
        rows.sort(key=lambda r: -len(r["src"]))
        accepted = []
        for r in rows:
            if any(overlap(r["bbox"], a["bbox"]) > args.dup_threshold for a in accepted):
                dropped += 1
                continue
            accepted.append(r)
        final.extend(accepted)

    # 竖排判定
    rot = 0
    for r in final:
        r["rotate"] = 0
        r["align"] = "left"
        if r["w"] < 14 and r["h"] > 18 and len(r["zh"]) >= 2:
            r["rotate"] = 90
            rot += 1
        elif len(r["zh"]) <= 2 and r["w"] < 30:
            r["align"] = "left"

    final.sort(key=lambda r: (r["page"], r["bbox"][1], r["bbox"][0]))
    plan = {
        "source": args.source,
        "output": args.output,
        "zoom": 4,
        "font": "NotoSerifSC-Regular.ttf",
        "defaults": {"pad": 0.7, "size_ratio": 0.78, "size_cap": 1.15, "scale_floor": 0.985},
        "items": [{"page": r["page"], "bbox": r["bbox"], "zh": r["zh"],
                   "align": r["align"], "rotate": r["rotate"]} for r in final],
    }
    out = QUEUE / "overlay_plan_l2.json"
    out.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    # 对齐报告
    lines = [
        f"工作单条目      : {len(units)}",
        f"已给出译文      : {len(trans)}",
        f"  其中标 `=` 保留: {sum(1 for v in trans.values() if v == '=')}",
        f"  实际待覆盖    : {len(to_translate)}",
        f"缺译 gid        : {len(missing)} {missing[:20]}",
        f"命中原始块      : {len(kept)}",
        f"重叠丢弃        : {dropped}",
        f"竖排(rotate 90) : {rot}",
        f"最终覆盖条目    : {len(final)}",
        f"涉及页数        : {len({r['page'] for r in final})}",
    ]
    (QUEUE / "align_report.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"plan -> {out.relative_to(BASE)}")


if __name__ == "__main__":
    main()
