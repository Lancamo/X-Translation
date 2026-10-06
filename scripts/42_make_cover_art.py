#!/usr/bin/env python3
"""
生成「铬金属」美术字位图（RGBA PNG），供 43_cover_title.py 原位替换封面标题。

要不要用
--------
只有当封面标题是**美术字位图**（不是文字层）时才需要。若标题在文字层，
L1 直接回填即可，不必走这条路。

设计要点（都是踩过坑才定下来的）
--------------------------------
1. **亮度不能从原图取**。原图逐行平均是「暗斑 + 亮边」的混合，拿来填色只能是
   灰调（试过，像拉丝铝）。改为：**只从原图取色温**（高光像素均值归一化），
   亮度用程序化的「亮-暗-亮」纵向节奏（CHROME_STOPS）映射到字高上。
   于是色温与原设计一致，明暗节奏可控。
2. **倒角必须窄**。把蒙版高斯模糊当高度场，其梯度 ≈ 向内法向，与左上光源点乘
   得到棱边明暗。倒角宽度取字高的 1.3% 量级；宽了笔画会整体渐变、看着发胖。
3. **必须有内凹暗线**。笔画交界处的深色刻线才是「铬」感的关键，缺了就是塑料片。
4. 不依赖 scipy，只用 numpy + Pillow。

用法
----
  # 1) 在**原始 PDF** 上列出图像层，拿到候选标题层的尺寸与矩形
  #    （在管线中间产物上看也行，但取色温要用原始 PDF）
  python3 43_cover_title.py --list --src input.pdf

  # 2) 按候选尺寸生成目标语言美术字（色温自动从原始标题图层采样）
  python3 42_make_cover_art.py --text "市场状况" --size 2048 878 \
      --tint-pdf input.pdf --tint-xref 14 --out work/cover_art.png

  # 3) 原位换图（在 L1 阶段产物上做；注意定位用尺寸+矩形，不用 xref）
  python3 43_cover_title.py --src work/stage/L1.pdf --out work/stage/L3.pdf \
      --title-size 2048 878 --title-rect 212.9 139.4 507.1 265.6 \
      --art work/cover_art.png

字重选择：中文笔画密度高于拉丁字母，直接沿用原图的粗细观感通常会偏重。
换 `--font` 试几个字重（Regular / Medium / Bold / Black），叠在原封面背景上对比再定。
实测某细笔画拉丁衬线标题对应 Noto Serif SC 的 Medium。
"""
import argparse
import io
import os
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageDraw, ImageFilter, ImageFont

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()

# 铬金属纵向亮度节奏（只有明暗，颜色由原图色温决定）
CHROME_STOPS = [(0.00, 0.70), (0.07, 0.98), (0.20, 0.52), (0.36, 0.90),
                (0.50, 1.00), (0.64, 0.58), (0.80, 0.96), (0.93, 0.74),
                (1.00, 0.88)]


def load_image(doc, xref):
    """取图像层的 RGB 与 alpha（无 SMask 则视为全不透明）。"""
    info = doc.extract_image(xref)
    rgb = np.asarray(Image.open(io.BytesIO(info["image"])).convert("RGB"),
                     dtype=np.float32) / 255.0
    key = doc.xref_get_key(xref, "SMask")[1]
    if key and key != "null":
        alpha = np.asarray(Image.open(io.BytesIO(
            doc.extract_image(int(key.split()[0]))["image"])).convert("L"))
        if alpha.shape != rgb.shape[:2]:
            alpha = np.asarray(Image.fromarray(alpha).resize(
                (rgb.shape[1], rgb.shape[0]), Image.LANCZOS))
    else:
        alpha = np.full(rgb.shape[:2], 255, np.uint8)
    return rgb, alpha


def sample_tint(src_pdf, xref):
    """从原图取金属的**色温**（RGB 比例），不取亮度。

    用高光像素（亮度上 15%）定色温，再归一化到自身均值——原图可能整体偏暗或偏亮，
    归一化后只保留色偏。
    """
    doc = pymupdf.open(src_pdf)
    rgb, alpha = load_image(doc, xref)
    doc.close()
    px = rgb[alpha > 240]
    if len(px) == 0:
        return np.ones(3, np.float32)
    lum = px.mean(axis=1)
    tint = px[lum > np.percentile(lum, 85)].mean(axis=0)
    return tint / max(tint.mean(), 1e-6)


def chrome_ramp(h, tint):
    """程序化铬金属色带：纵向「亮-暗-亮」节奏 × 原图色温。"""
    pos = np.linspace(0, 1, h)
    xs = np.array([p for p, _ in CHROME_STOPS])
    ys = np.array([v for _, v in CHROME_STOPS])
    return np.interp(pos, xs, ys)[:, None] * tint[None, :]


def render_glyph_mask(text, fontfile, canvas, target_w, tracking=0.06):
    """把文字渲染成软边蒙版（0..1 float）。

    逐字绘制以获得**均匀字距**——直接 `draw.text` 一个整串时，
    中文字距由字体默认值决定，和原设计的疏密往往对不上。
    """
    W, H = canvas
    probe = ImageFont.truetype(str(fontfile), 400)
    bb = probe.getbbox(text)
    txt_w = bb[2] - bb[0]
    n = max(1, len(text))
    size = int(400 * (target_w / (txt_w + tracking * 400 * (n - 1))))
    font = ImageFont.truetype(str(fontfile), size)
    big = Image.new("L", (W * 2, H * 2), 0)
    d = ImageDraw.Draw(big)
    adv = font.getlength(text) / n
    x = (W * 2 - adv * n) / 2
    for ch in text:
        d.text((x, H), ch, font=font, fill=255, anchor="lm")
        x += adv
    return np.asarray(big.crop((W // 2, H // 2, W // 2 + W, H // 2 + H))
                      ).astype(np.float32) / 255.0


def chrome(mask, ramp_fn, bevel=1.0, cleft=0.34, outline=6, dark=18, rim=0.013):
    """色带 + 细倒角棱边 + 内凹暗线 + 外描边 → RGBA uint8。"""
    H, W = mask.shape
    a = mask > 0.5
    ys, _ = np.where(a)
    if len(ys) == 0:
        raise SystemExit("字形蒙版为空（检查 --text 与 --font）")
    gy0, gy1 = int(ys.min()), int(ys.max())
    gh = max(1, gy1 - gy0)

    # 1) 色带按字形实际高度生成，再贴到画布对应位置
    rgb = np.zeros((H, W, 3), np.float32)
    rgb[gy0:gy1 + 1] = ramp_fn(gh + 1)[:, None, :]

    # 2) 倒角：模糊高度场的梯度 ≈ 向内法向，与左上光源点乘（窄棱边）
    blur = max(3, int(rim * gh))
    h = np.asarray(Image.fromarray((mask * 255).astype(np.uint8))
                   .filter(ImageFilter.GaussianBlur(blur))).astype(np.float32) / 255.0
    gy, gx = np.gradient(h)
    norm = np.hypot(gx, gy) + 1e-6
    lam = (-gx / norm) * (-0.50) + (-gy / norm) * (-0.86)
    edge = np.clip(np.hypot(gx, gy) / (np.hypot(gx, gy).max() + 1e-6) * 3.2, 0, 1)
    rgb *= (1.0 + bevel * lam * edge)[:, :, None]

    # 3) 内凹暗线
    m_img = Image.fromarray((mask * 255).astype(np.uint8))
    k = 2 * max(1, gh // 150) + 1
    inner = a & ~(np.asarray(m_img.filter(ImageFilter.MinFilter(k))) > 128)
    rgb[inner] *= cleft

    # 4) 外描边，把字从背景里剥出来
    ring = (np.asarray(m_img.filter(ImageFilter.MaxFilter(2 * outline + 1))) > 128) & ~a
    rgb[ring] = dark / 255.0
    alpha = np.where(ring, 255, np.clip(mask * 255, 0, 255))

    return np.dstack([np.clip(rgb * 255, 0, 255).astype(np.uint8),
                      alpha.astype(np.uint8)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", required=True, help="目标语言标题文字")
    ap.add_argument("--size", nargs=2, type=int, required=True, metavar=("W", "H"),
                    help="输出画布像素尺寸，应与目标标题图层一致（43_cover_title.py --list 查看）")
    ap.add_argument("--font", default=str(BASE / "work/fonts/NotoSerifSC-Medium.ttf"))
    ap.add_argument("--tint-pdf", default="", help="取色温用的源 PDF（一般就是原始 PDF）")
    ap.add_argument("--tint-xref", type=int, default=0, help="原封面标题图层的 xref")
    ap.add_argument("--width", type=float, default=0.92, help="文字占画布宽度比例")
    ap.add_argument("--tracking", type=float, default=0.06, help="字距（相对字号）")
    ap.add_argument("--outline", type=int, default=6, help="外描边宽度（像素）")
    ap.add_argument("--bevel", type=float, default=1.0, help="倒角强度")
    ap.add_argument("--cleft", type=float, default=0.34, help="内凹暗线深度（越小越深）")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    tint = np.ones(3, np.float32)
    if args.tint_pdf and args.tint_xref:
        tint = sample_tint(args.tint_pdf, args.tint_xref)
    else:
        print("[warn] 未提供 --tint-pdf/--tint-xref，色温按中性处理")

    mask = render_glyph_mask(args.text, Path(args.font), tuple(args.size),
                             args.size[0] * args.width, args.tracking)
    art = Image.fromarray(chrome(mask, lambda h: chrome_ramp(h, tint),
                                 bevel=args.bevel, cleft=args.cleft, outline=args.outline))
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    art.save(args.out)
    print(f"art -> {args.out}  {art.size}  文字={args.text}  色温={np.round(tint, 3)}")


if __name__ == "__main__":
    main()
