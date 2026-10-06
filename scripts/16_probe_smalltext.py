#!/usr/bin/env python3
"""16_probe_smalltext.py — 验证不同块高的图内文字「原图可见性 + 中文覆盖可行性」。

回答两个问题：
  1. h<5pt 的微型字在原图上到底能不能看清（值不值得翻）？
  2. 小框（h=5~12pt）里塞中文会不会被压得比原文更小、更不可读？

用 8x 渲染原 PDF 局部（比 OCR 用的 4x 更清楚），横向并排比较。

用法：python 16_probe_smalltext.py
"""
import json
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SCOPE = BASE / "work" / "ocr_scope4.json"
OUT = BASE / "work" / "proto"
RENDER = 8.0

# 每档挑几条有代表性的（page, 文本关键词）
PICKS = {
    "h<4":   [(7, "Coverage limitations"), (6, "Source: CaplQ"), (9, "Health"), (11, "% relative")],
    "h4-6":  [(41, "Micron"), (29, "Total"), (71, "50th"), (49, "Automation")],
    "h6-8":  [(9, "Energy"), (57, "Hardware"), (41, "Median"), (41, "Google")],
    "h8-12": [(29, "Technology"), (41, "amazon"), (82, "Datadog"), (55, "Raw Inputs")],
    "h12-20": [(17, "Source: JPMAM"), (67, "Typical Time"), (33, "Top 1%"), (6, "Share of total")],
    "h>20":  [(49, "# of Components"), (66, "$1.73T cumulative"), (86, "$4.49M per round")],
}


def find(pno, key):
    items = json.loads(SCOPE.read_text(encoding="utf-8"))["in_image"]
    best = None
    for x in items:
        if x["page"] != pno:
            continue
        if key.lower() in x["text"].lower():
            h = x["bbox"][3] - x["bbox"][1]
            if best is None or abs(h - 0) < 1e9:
                best = x
    return best


def main():
    doc = pymupdf.open(BASE / "input.pdf")
    rows = []
    for band, picks in PICKS.items():
        for pno, key in picks:
            x = find(pno, key)
            if not x:
                print("miss", band, pno, key)
                continue
            b = x["bbox"]
            clip = pymupdf.Rect(b[0] - 3, b[1] - 3, b[2] + 3, b[3] + 3)
            pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(RENDER, RENDER),
                                           clip=clip, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            h = b[3] - b[1]
            rows.append((band, x, im, h))

    # 画布：左=裁剪原图（等比缩放到统一宽度），右=说明
    W = 1400
    LW = 900
    parts = []
    for band, x, im, h in rows:
        k = min(LW / im.width, 6.0)
        im2 = im.resize((max(1, int(im.width * k)), max(1, int(im.height * k))))
        row_h = max(58, im2.height + 18)
        parts.append((band, x, im2, h, row_h))

    canvas = Image.new("RGB", (W, sum(p[4] for p in parts) + 4), (255, 255, 255))
    dr = ImageDraw.Draw(canvas)
    y = 0
    for band, x, im2, h, row_h in parts:
        canvas.paste(im2, (6, y + 8))
        dr.rectangle([4, y + 6, 6 + im2.width + 2, y + 8 + im2.height + 2], outline=(190, 190, 190))
        dr.text((LW + 24, y + 10), f"{band}  h={h:.1f}pt  p{x['page']}", fill=(0, 0, 0))
        dr.text((LW + 24, y + 26), f"OCR: {x['text'][:70]}", fill=(60, 60, 60))
        dr.line([(0, y), (W, y)], fill=(220, 220, 220))
        y += row_h

    OUT.mkdir(parents=True, exist_ok=True)

    # 每档单独出一张，避免整图过高看不清
    for band in PICKS:
        sub = [p for p in parts if p[0] == band]
        if not sub:
            continue
        c = Image.new("RGB", (W, sum(p[4] for p in sub) + 4), (255, 255, 255))
        d2 = ImageDraw.Draw(c)
        yy = 0
        for band_, x, im2, h, row_h in sub:
            c.paste(im2, (6, yy + 8))
            d2.rectangle([4, yy + 6, 6 + im2.width + 2, yy + 8 + im2.height + 2],
                         outline=(190, 190, 190))
            d2.text((LW + 24, yy + 12), f"h={h:.1f}pt  p{x['page']}", fill=(0, 0, 0))
            d2.text((LW + 24, yy + 30), f"OCR: {x['text'][:60]}", fill=(60, 60, 60))
            d2.line([(0, yy), (W, yy)], fill=(220, 220, 220))
            yy += row_h
        f = OUT / f"smalltext_{band.replace('<','lt').replace('>','gt')}.png"
        c.save(f)
        print("saved", f.name, c.size)


if __name__ == "__main__":
    main()
