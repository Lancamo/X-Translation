#!/usr/bin/env python3
"""70_verify_visual.py — L2 覆盖后的视觉核验：整页对照 + 局部放大。

用法：
  python 70_verify_visual.py --before output/..._v1.0.pdf --after output/..._v1.1.pdf \
         --pages 6,49,58,71 --mode full       # 整页上下对照
  python 70_verify_visual.py ... --pages 58 --mode zoom --rect 40,150,400,320   # 局部放大
"""
import argparse
from pathlib import Path

import pymupdf
from PIL import Image
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
OUT = BASE / "work" / "verify"


def render(doc, pno, zoom, clip=None):
    pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip, alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def stack(imgs, width, gap=14, bg=(255, 0, 255)):
    scaled = []
    for im in imgs:
        k = width / im.width
        scaled.append(im.resize((width, max(1, int(im.height * k)))))
    total = sum(i.height for i in scaled) + gap * (len(scaled) - 1)
    canvas = Image.new("RGB", (width, total), bg)
    y = 0
    for i in scaled:
        canvas.paste(i, (0, y))
        y += i.height + gap
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", default="output/product.pdf")
    ap.add_argument("--after", default="output/product.pdf")
    ap.add_argument("--pages", required=True)
    ap.add_argument("--mode", default="full", choices=["full", "zoom"])
    ap.add_argument("--rect", default="")
    ap.add_argument("--zoom", type=float, default=0)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    before = pymupdf.open(BASE / args.before)
    after = pymupdf.open(BASE / args.after)
    pages = [int(p) for p in args.pages.split(",") if p.strip()]

    for pno in pages:
        if args.mode == "full":
            z = args.zoom or 1.35
            a = render(before, pno, z); b = render(after, pno, z)
            f = OUT / f"cmp_full_p{pno:02d}.png"
            stack([a, b], 1180).save(f)
        else:
            clip = pymupdf.Rect(*[float(v) for v in args.rect.split(",")])
            z = args.zoom or 3.0
            a = render(before, pno, z, clip); b = render(after, pno, z, clip)
            f = OUT / f"cmp_zoom_p{pno:02d}.png"
            stack([a, b], min(1300, a.width)).save(f)
        print("saved", f.relative_to(BASE))

    print(f"\nbefore {before.page_count} 页 / after {after.page_count} 页")
    print(f"体积 {BASE/args.before}: {(BASE/args.before).stat().st_size/1e6:.2f} MB")
    print(f"     {BASE/args.after}: {(BASE/args.after).stat().st_size/1e6:.2f} MB")


if __name__ == "__main__":
    main()
