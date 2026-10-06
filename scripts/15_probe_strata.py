#!/usr/bin/env python3
"""15_probe_strata.py — 按「块高 + OCR 置信度」给图内文字分层，评估可安全覆盖的比例。

背景：OCR 在小字号/艺术字/logotype 上误识率高，误识会被直接写进成品 PDF。
需要先知道「哪些层级可信、哪些必须人工核对或跳过」。

用法：python 15_probe_strata.py
"""
import json
from collections import defaultdict
from pathlib import Path
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SCOPE = BASE / "work" / "ocr_scope4.json"

BANDS = [(0, 4), (4, 5), (5, 6), (6, 8), (8, 12), (12, 20), (20, 1000)]


def main():
    items = json.loads(SCOPE.read_text(encoding="utf-8"))["in_image"]
    keep = [x for x in items if x["cls"] != "skip_numeric"]

    # 去重：按文本聚合，取该文本所有实例的最大高度（同一文本字号可能因缩放不同）
    uniq = {}
    for x in keep:
        t = x["text"].strip()
        h = x["bbox"][3] - x["bbox"][1]
        u = uniq.setdefault(t, {"h": [], "conf": [], "n": 0, "cls": x["cls"]})
        u["h"].append(h)
        u["conf"].append(x["conf"])
        u["n"] += 1

    conf_vals = [round(c, 2) for x in keep for c in [x["conf"]]]
    from collections import Counter
    print("=== OCR 置信度分布（全部块）===")
    for v, n in Counter(conf_vals).most_common(8):
        print(f"  conf={v}: {n}")

    print("\n=== 按「文本最大块高」分层（去重后）===")
    print(f"{'档位(pt)':<12}{'条数':>6}{'占比':>8}{'字符':>8}{'平均conf':>10}{'conf<0.5':>10}")
    total = len(uniq)
    band_rows = defaultdict(list)
    for t, u in uniq.items():
        hm = max(u["h"])
        for lo, hi in BANDS:
            if lo <= hm < hi:
                band_rows[(lo, hi)].append((t, u, hm))
                break
    for (lo, hi) in BANDS:
        rows = band_rows.get((lo, hi), [])
        if not rows:
            continue
        chars = sum(len(t) for t, _, _ in rows)
        cf = [sum(u["conf"]) / len(u["conf"]) for _, u, _ in rows]
        low = sum(1 for c in cf if c < 0.5)
        print(f"{lo}-{hi:<9}{len(rows):>6}{100*len(rows)/total:>7.1f}%{chars:>8}"
              f"{sum(cf)/len(cf):>10.2f}{low:>10}")

    print("\n=== 极小块（h<5pt）样例 30 条 —— OCR 最可疑区 ===")
    small = [(t, u, hm) for t, u, hm in
             [r for rows in band_rows.values() for r in rows] if hm < 5]
    small.sort(key=lambda r: -r[1]["n"])
    for t, u, hm in small[:30]:
        print(f"  h={hm:4.1f} n={u['n']:<3} conf={sum(u['conf'])/len(u['conf']):.2f} {t[:72]!r}")

    print("\n=== 大块（h>=12pt，标题级）样例 20 条 ===")
    big = [r for rows in band_rows.values() for r in rows if r[2] >= 12]
    big.sort(key=lambda r: -r[2])
    for t, u, hm in big[:20]:
        print(f"  h={hm:5.1f} n={u['n']:<3} {t[:72]!r}")

    # 覆盖率估算
    for thr in (5, 6, 8):
        rows = [r for rows in band_rows.values() for r in rows if r[2] >= thr]
        print(f"\n若只覆盖 h>={thr}pt：{len(rows)} 条 / {100*len(rows)/total:.1f}%，"
              f"字符 {sum(len(t) for t,_,_ in rows)}")


if __name__ == "__main__":
    main()
