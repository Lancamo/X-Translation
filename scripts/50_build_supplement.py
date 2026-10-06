#!/usr/bin/env python3
"""50_build_supplement.py — 生成 L2 补充覆盖候选（竖排漏检 + 高度门槛下调的干净项）。

残迹扫描（60_scan_residue.py）给出两类系统性漏项：
  1. unseen  —— 竖排文字，主 OCR 读不出 → 由 13_scan_vertical.py 的旋转扫描补上
  2. filtered/过小 —— 块高 <5pt 被门槛挡掉，其中 3.6~5pt 段实际可读

本脚本把两类合流，用严格判据过一遍（只留"看着就是正常英文"的），输出
work/l2_queue/batch_s1.txt 供翻译，再合并进覆盖计划。

用法：python 50_build_supplement.py [--min-h 3.6]
"""
import argparse
import json
import re
from pathlib import Path
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = BASE / "work"

DECOR = re.compile(os.environ.get("XTRANS_DECOR", r"^AI\s*[6OG&]\s*Z?\s*GROWTH$"), re.IGNORECASE)
NUMERIC = re.compile(r"^[\s0-9$%¢€£¥.,+\-–—/×x:*()\[\]<>|~='\"#&°]+$")

# 明显是 OCR 碎屑：长度过短、缺元音的长串、字符种类异常
VOWELS = set("aeiouAEIOU")


def clean_english(t: str) -> bool:
    t = t.strip()
    if len(t) < 5 or re.search(r"[^\x20-\x7E]", t):
        return False
    if DECOR.match(t) or NUMERIC.match(t):
        return False
    if not re.search(r"[A-Za-z]{3,}", t):
        return False
    words = re.findall(r"[A-Za-z]{2,}", t)
    if not words:
        return False
    letters = sum(c.isalpha() for c in t)
    if letters / len(t) < 0.55:
        return False
    # 每个词至少含一个元音（'HVEiNK' 例外过多；'sxP' 这类会被滤掉）
    bad = sum(1 for w in words if len(w) >= 4 and not (set(w) & VOWELS))
    if bad > max(1, len(words) // 4):
        return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-h", type=float, default=3.6)
    ap.add_argument("--drop", default="")   # 逗号分隔，手工剔除
    args = ap.parse_args()

    scope = json.loads((WORK / "ocr_scope4.json").read_text(encoding="utf-8"))
    vert = json.loads((WORK / "ocr_vertical_new.json").read_text(encoding="utf-8"))
    queue = WORK / "l2_queue"
    row = re.compile(r"^(\d{4})\t\[[^\]]*\]\t(.*)$")
    seen = set()
    for f in sorted(queue.glob("batch_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = row.match(line)
            if m:
                seen.add(re.sub(r"\s+", "", m.group(2)).lower())

    drop = {d.strip() for d in args.drop.split(",") if d.strip()}
    cand = {}

    for x in scope["in_image"]:
        h = x["bbox"][3] - x["bbox"][1]
        if not (args.min_h <= h < 5.0):
            continue
        t = x["text"].strip()
        if not clean_english(t) or t in drop:
            continue
        if re.sub(r"\s+", "", t).lower() in seen:
            continue
        cand.setdefault(t, {"text": t, "pages": set(), "bbox": x["bbox"], "page": x["page"],
                            "h": h, "src": "small"})

    for v in vert:
        t = v["text"].strip()
        if not clean_english(t) or t in drop:
            continue
        if re.sub(r"\s+", "", t).lower() in seen:
            continue
        key = t
        if key in cand:
            cand[key]["pages"].add(v["page"])
        else:
            cand[key] = {"text": t, "pages": {v["page"]}, "bbox": v["bbox"],
                         "page": v["page"], "h": v["h"], "src": "vertical"}

    items = sorted(cand.values(), key=lambda c: (c["src"] != "vertical", -c["h"], c["text"]))
    lines = [f"# L2 补充候选  {len(items)} 条", "# 格式：序号\\t[来源/页/块高]\\t原文", ""]
    for i, c in enumerate(items, 1):
        tag = f"{'竖排' if c['src'] == 'vertical' else '过小'} p{c['page']} h={c['h']:.1f}"
        lines.append(f"{i:04d}\t[{tag}]\t{c['text']}")
    out = queue / "batch_s1.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"补充候选 {len(items)} 条 -> {out.relative_to(BASE)}")
    print(f"  竖排 {sum(1 for c in items if c['src'] == 'vertical')} 条 / "
          f"3.6~5pt {sum(1 for c in items if c['src'] == 'small')} 条")
    for i, c in enumerate(items, 1):
        print(f"  {i:>3} [{'竖' if c['src'] == 'vertical' else '小'}] "
              f"p{c['page']:<3} h={c['h']:<5.1f} {c['text'][:70]!r}")


if __name__ == "__main__":
    main()
