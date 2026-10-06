#!/usr/bin/env python3
"""61_show_residue.py — 把残迹清单渲染成对照图，供肉眼判读。

用法：
  python 61_show_residue.py --min-h 9 --limit 12 --out work/residue/sheet_top.png
"""
import argparse
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", default="work/residue/residue.json")
    ap.add_argument("--pdf", default="output/product.pdf")
    ap.add_argument("--cls", default="residual")
    ap.add_argument("--min-h", type=float, default=9.0)
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--zoom", type=float, default=4.0)
    ap.add_argument("--out", default="work/residue/sheet_top.png")
    args = ap.parse_args()

    rows = json.loads((BASE / args.rows).read_text(encoding="utf-8"))
    pick = [r for r in rows if r["cls"] == args.cls and r["h"] >= args.min_h][:args.limit]
    doc = pymupdf.open(BASE / args.pdf)

    items = []
    for r in pick:
        b = r["bbox"]
        pad = 6
        pr = doc[r["page"] - 1].rect
        rect = pymupdf.Rect(max(0, b[0] - pad), max(0, b[1] - pad),
                            min(pr.width, b[2] + pad), min(pr.height, b[3] + pad))
        pix = doc[r["page"] - 1].get_pixmap(matrix=pymupdf.Matrix(args.zoom, args.zoom),
                                            clip=rect, alpha=False)
        items.append((r, Image.frombytes("RGB", (pix.width, pix.height), pix.samples)))

    W = 1000
    sized = [((W, max(1, int(im.height * W / im.width))), im) for _, im in items]
    H = sum(s[0][1] for s in sized) + 16 * len(sized)
    canvas = Image.new("RGB", (W, H), (255, 0, 255))
    dr = ImageDraw.Draw(canvas)
    y = 0
    for (r, _), ((w, h), im) in zip(items, sized):
        canvas.paste(im.resize((w, h)), (0, y))
        dr.text((8, y + 2), f"p{r['page']}  h={r['h']}  cov={r['covered']}", fill=(0, 0, 0))
        y += h + 16
    out = BASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out)
    print("saved", out.relative_to(BASE), canvas.size)
    for r in pick:
        print(f"  p{r['page']:<3} h={r['h']:<5} cov={r['covered']:<5} {r['text'][:64]!r}")


if __name__ == "__main__":
    main()
