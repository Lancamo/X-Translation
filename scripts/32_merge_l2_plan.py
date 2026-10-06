#!/usr/bin/env python3
"""32_merge_l2_plan.py — 把补充覆盖条目并入既有覆盖计划（保留原 1277 条，不重算）。

为什么不让 79 重算：79 从工作单全局重建成计划，任何工作单层面的变动（gid 段、
文本重复）都会连带改变原有条目的取舍。补充轮不该动已验收的部分 —— 只做增量并集。

用法：python 32_merge_l2_plan.py --old work/l2_queue/_plan_v11_backup.json \
                              --new work/l2_queue/overlay_plan_l2.json
"""
import argparse
import json
from pathlib import Path
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()


def overlap_ratio(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    small = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return inter / max(small, 1e-6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--old", default="work/l2_queue/_plan_v11_backup.json")
    ap.add_argument("--new", default="work/l2_queue/overlay_plan_l2.json")
    ap.add_argument("--dup", type=float, default=0.55)
    ap.add_argument("--out", default="work/l2_queue/overlay_plan_l2.json")
    args = ap.parse_args()

    old = json.loads((BASE / args.old).read_text(encoding="utf-8"))
    new = json.loads((BASE / args.new).read_text(encoding="utf-8"))

    have = [(it["page"], it["bbox"]) for it in old["items"]]
    added = []
    for it in new["items"]:
        if any(it["page"] == p and overlap_ratio(it["bbox"], b) > args.dup for p, b in have):
            continue
        added.append(it)
        have.append((it["page"], it["bbox"]))

    merged = dict(old)
    merged["items"] = old["items"] + added
    merged["items"].sort(key=lambda r: (r["page"], r["bbox"][1], r["bbox"][0]))
    (BASE / args.out).write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    print(f"原计划 {len(old['items'])} 条 + 新增 {len(added)} 条 = {len(merged['items'])} 条")
    print(f"涉及 {len({it['page'] for it in merged['items']})} 页")
    for it in added:
        b = it["bbox"]
        print(f"  p{it['page']:<3} {b[2]-b[0]:>5.1f}x{b[3]-b[1]:<6.1f} rot={it.get('rotate', 0):<3} "
              f"{it['zh'][:40]!r}")


if __name__ == "__main__":
    main()
