#!/usr/bin/env python3
"""17_audit_ocr.py — OCR 质量抽检：把「页面裁剪图」与「OCR 文本」并排，肉眼核对误识率。

全量覆盖前的前置质检。误识会被直接写进成品 PDF，必须先量化。
用已渲染好的 work/ocr_pages4/*.png（4x），不重新渲染。

用法：
  python 17_audit_ocr.py --sample 24            # 分层随机抽样
  python 17_audit_ocr.py --text "AI6ZGROWTH"   # 查某个文本的所有实例
"""
import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SCOPE = BASE / "work" / "ocr_scope4.json"
PAGES = BASE / "work" / "ocr_pages4"
OUT = BASE / "work" / "proto"
ZOOM = 4.0
CROP_W = 760          # 裁剪图在画布上的显示宽度
PAD = 2.0             # 坐标外扩（pt）


def load_page(pno):
    return Image.open(PAGES / f"p{pno:02d}.png").convert("RGB")


def crop(pt_box, pno):
    x0, y0, x1, y1 = pt_box
    box = (max(0, int((x0 - PAD) * ZOOM)), max(0, int((y0 - PAD) * ZOOM)),
           int((x1 + PAD) * ZOOM), int((y1 + PAD) * ZOOM))
    im = load_page(pno).crop(box)
    if im.width < 8 or im.height < 8:
        im = im.resize((max(8, im.width), max(8, im.height)))
    scale = min(CROP_W / im.width, 3.0)
    if scale < 1 or im.width < CROP_W:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))))
    if im.height > 180:                       # 竖排/长条块，限高
        k = 180 / im.height
        im = im.resize((max(1, int(im.width * k)), 180))
    return im


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--text", type=str, default="")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--limit", type=int, default=14)
    ap.add_argument("--out", type=str, default="")
    args = ap.parse_args()

    data = json.loads(SCOPE.read_text(encoding="utf-8"))
    items = data["in_image"]

    if args.text:
        sel = [x for x in items if x["text"].strip() == args.text]
        tag = f"text_{args.text[:20]}"
    else:
        rnd = random.Random(args.seed)
        buckets = {}
        for x in items:
            buckets.setdefault(x["cls"], []).append(x)
        plan = {"skip_numeric": max(3, args.sample // 6),
                "label": args.sample // 2,
                "sentence": args.sample - max(3, args.sample // 6) - args.sample // 2}
        sel = []
        for c, n in plan.items():
            pool = buckets.get(c, [])
            sel += rnd.sample(pool, min(n, len(pool)))
        rnd.shuffle(sel)
        tag = f"sample{args.sample}_seed{args.seed}"
    sel = sel[:args.limit]

    OUT.mkdir(parents=True, exist_ok=True)
    rows = []
    for x in sel:
        try:
            im = crop(x["bbox"], x["page"])
        except Exception as e:
            print("crop fail", x["page"], e)
            continue
        h = max(62, im.height + 16)
        rows.append((x, im, h))

    W = 1500
    total_h = sum(h for _, _, h in rows) + 6
    canvas = Image.new("RGB", (W, total_h), (255, 255, 255))
    dr = ImageDraw.Draw(canvas)
    y = 0
    for x, im, h in rows:
        # 左侧裁剪
        canvas.paste(im, (6, y + 4))
        dr.rectangle([4, y + 2, 6 + im.width + 2, y + h - 2], outline=(200, 200, 200))
        # 右侧信息
        b = x["bbox"]
        info = [f"p{x['page']}  [{x['cls']}]  conf={x['conf']:.2f}",
                f"bbox=({b[0]:.0f},{b[1]:.0f},{b[2]:.0f},{b[3]:.0f})  h={b[3]-b[1]:.1f}pt",
                f"OCR: {x['text']}"]
        ty = y + 6
        for line in info:
            dr.text((CROP_W + 20, ty), line, fill=(0, 0, 0))
            ty += 15
        dr.line([(0, y), (W, y)], fill=(225, 225, 225))
        y += h
    out = OUT / (args.out or f"audit_{tag}.png")
    canvas.save(out)
    print(f"{len(rows)} rows -> {out}")
    print(out)


if __name__ == "__main__":
    main()
