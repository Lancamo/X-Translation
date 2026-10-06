#!/usr/bin/env python3
"""14_scan_bg.py — 判断「原位覆盖法」的可行性：图内文字的背景是不是均匀色块。

方法：在文字 bbox **内部**统计颜色众数（量化到 4 的倍数以抗噪）。
      - 众数占比高 → 文字压在均匀色块上，可以用该色铺底后再写中文（覆盖法可行）
      - 众数占比低 → 文字压在图案/渐变/其他图形上，直接铺底色会露出补丁（需另法）
同时给出**文字色**（bbox 内与背景差最大的主要簇），供重写中文时取色。

输出：work/bg_probe.json + 控制台统计
"""
import json
from collections import Counter
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))
SCOPE = BASE / "work" / "ocr_scope.json"
OUT = BASE / "work" / "bg_probe.json"
ZOOM = 2.2
UNIFORM_RATIO = 0.55      # 众数占比阈值


def analyze(pix, rect):
    """返回 bbox 内的 (背景色, 背景占比, 文字色)"""
    x0 = max(0, int(rect.x0 * ZOOM))
    y0 = max(0, int(rect.y0 * ZOOM))
    x1 = min(pix.width, int(rect.x1 * ZOOM))
    y1 = min(pix.height, int(rect.y1 * ZOOM))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return None
    step = 1 if (x1 - x0) * (y1 - y0) < 40000 else 2
    counts = Counter()
    for x in range(x0, x1, step):
        for y in range(y0, y1, step):
            p = pix.pixel(x, y)
            counts[(p[0] // 4 * 4, p[1] // 4 * 4, p[2] // 4 * 4)] += 1
    if not counts:
        return None
    total = sum(counts.values())
    bg, bg_n = counts.most_common(1)[0]
    ratio = bg_n / total
    # 放宽判据：背景色 ±10 邻域的像素占比（白底小字时，文字笔画会拉低精确众数占比）
    broad = sum(n for c, n in counts.items()
                if max(abs(c[i] - bg[i]) for i in range(3)) <= 10)
    broad_ratio = broad / total

    def dist(c):
        return sum((c[i] - bg[i]) ** 2 for i in range(3)) ** 0.5

    far = [(c, n) for c, n in counts.items() if dist(c) > 90]
    txt = max(far, key=lambda kv: kv[1])[0] if far else None
    lum = sum(bg) / 3
    return {"bg": bg, "bg_ratio": round(ratio, 3), "broad_ratio": round(broad_ratio, 3),
            "text_color": txt, "bg_lum": round(lum),
            "uniform": broad_ratio >= UNIFORM_RATIO}


def main():
    scope = json.loads(SCOPE.read_text(encoding="utf-8"))
    src = pymupdf.open(SRC)
    cache = {}
    results = []
    for item in scope["in_image"]:
        if item["cls"] == "skip_numeric":
            continue
        pno = item["page"]
        if pno not in cache:
            cache[pno] = src[pno - 1].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False)
        a = analyze(cache[pno], pymupdf.Rect(*item["bbox"]))
        if not a:
            continue
        a.update({"page": pno, "text": item["text"], "cls": item["cls"], "bbox": item["bbox"]})
        results.append(a)
    src.close()

    n = len(results)
    uni = sum(1 for r in results if r["uniform"])
    bgs = Counter(r["bg"] for r in results)
    per_page_bad = Counter(r["page"] for r in results if not r["uniform"])
    light = sum(1 for r in results if r["bg_lum"] >= 200)
    rotated = sum(1 for r in results
                  if (r["bbox"][3] - r["bbox"][1]) > 2 * (r["bbox"][2] - r["bbox"][0])
                  and len(r["text"]) > 3)
    out = {
        "total_checked": n,
        "uniform": uni,
        "ratio_uniform": round(uni / max(n, 1), 3),
        "light_bg": light,
        "rotated_text": rotated,
        "threshold": UNIFORM_RATIO,
        "top_bg_colors": bgs.most_common(10),
        "pages_with_most_nonuniform": per_page_bad.most_common(15),
        "non_uniform_samples": sorted(
            [{"page": r["page"], "text": r["text"][:64], "broad_ratio": r["broad_ratio"],
              "bg": r["bg"], "bg_lum": r["bg_lum"]}
             for r in results if not r["uniform"]], key=lambda x: x["broad_ratio"])[:30],
        "detail": results,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print("=== 覆盖法可行性（文字背后是否为均匀色块）===")
    print(f"检查块数                : {n}")
    print(f"背景均匀（可直接覆盖）  : {uni}  ({100*uni/max(n,1):.1f}%)")
    print(f"背景非均匀（需另法）    : {n-uni}  ({100*(n-uni)/max(n,1):.1f}%)")
    print(f"浅色背景(亮度>=200)     : {light}  ({100*light/max(n,1):.1f}%)")
    print(f"疑似竖排/旋转文字       : {rotated}")
    print()
    print("最常见背景色：")
    for c, k in bgs.most_common(8):
        print(f"  rgb{c} ×{k}")
    print()
    print("=== 非均匀最严重的样例（前 20）===")
    for r in out["non_uniform_samples"][:20]:
        print(f"  p{r['page']:>2} broad={r['broad_ratio']:.2f} lum={r['bg_lum']:>3} bg={r['bg']} {r['text']!r}")
    print()
    print("非均匀块最多的页：", per_page_bad.most_common(10))
    print(f"\nJSON -> {OUT.relative_to(BASE)}")


if __name__ == "__main__":
    main()
