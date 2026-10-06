#!/usr/bin/env python3
"""
封面大标题原位换字：把封面那层「外文美术字位图」换成目标语言的位图。

为什么需要这一步
----------------
设计稿常把大标题做成**透明位图**叠在背景照片上，而不是文字层。
翻译流程只处理文字层，永远碰不到它 —— 于是"标题没翻译"。
删掉它不行（背景会露洞），不删也不行（译文比原文短，外文从旁边露出来）。
唯一解法：**原位换图**。新图沿用完全相同的像素尺寸与放置矩形，
画布位置、大小、层级全部不变，只是字换了。

⚠️ 为什么按「像素尺寸 + 放置矩形」定位，而不是 xref 号
----------------------------------------------------
**xref 号在管线里会变**。同一张标题图在原始 PDF 里是 xref 14，
到 L1 阶段（`24_rebuild_text.py` 重写过文档）就变成 9 了 —— 按 xref 定位会直接失手。
像素尺寸与放置矩形则由管线原样保留（浮点误差 < 0.02pt），是可靠的锚。

流程：先 `--list` 拿到候选层的尺寸与矩形，再照抄进换图命令。

阴影层自动找：其它带透明蒙版、放置矩形与标题重叠最大的层。`--shadow-rect` 可指定。

⚠️ 写图像流的两个坑（都踩过，别再踩）
------------------------------------
1. **不能塞 JPEG 字节**。保存时 `deflate_images=True` 会把 /DCTDecode 改成
   /FlateDecode 但内容仍是 JPEG 字节 → 解码出来是雪花噪点。
2. `update_stream` **默认 compress=1 自己就会压**。再手动 `zlib.compress` 一次
   就是**双重压缩**，解码只剩开头一小段是真数据，其余全黑
   （实测蒙版 878 行里只有 17 行有墨）。
所以：传**解压后的原始像素**，压缩交给 `update_stream`，再显式声明
Filter / ColorSpace / BitsPerComponent / DecodeParms。

用法
----
  python3 43_cover_title.py --list --src input.pdf
  python3 43_cover_title.py --src work/stage/L1.pdf --out work/stage/L3.pdf \
      --title-rect 212.9 139.4 507.1 265.6 --title-size 2048 878 \
      --art work/cover_art.png
  # 不传 --art 则整步直通（只复制 PDF），用于先跑通链路
"""
import argparse
import io
import os
import shutil
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageFilter

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()


def image_size(doc, xref):
    """图像的像素尺寸（读 xref 自身的 Width/Height，不依赖解码）。"""
    w = doc.xref_get_key(xref, "Width")[1]
    h = doc.xref_get_key(xref, "Height")[1]
    if w is None or h is None:
        return None
    return int(w), int(h)


def layer_info(doc, page, xref):
    """图像层的像素尺寸 / 放置矩形 / 蒙版 xref 与平均不透明度。

    rect 为 None 表示该 xref 没有作为图像被放置在本页（例如它是别人的蒙版）。
    """
    wh = image_size(doc, xref)
    if wh is None:
        return None
    smask = doc.xref_get_key(xref, "SMask")[1]
    mask = int(smask.split()[0]) if smask and smask != "null" else 0
    amean = 1.0
    if mask:
        a = Image.open(io.BytesIO(doc.extract_image(mask)["image"])).convert("L")
        amean = float(np.asarray(a).mean() / 255.0)
    rects = page.get_image_rects(xref)
    return {"xref": xref, "wh": wh, "rect": rects[0] if rects else None,
            "mask": mask, "alpha_mean": amean}


def all_layers(doc, page):
    out = []
    for info in page.get_images(full=True):
        li = layer_info(doc, page, info[0])
        if li and li["rect"] is not None:
            out.append(li)
    return out


def rect_str(r):
    return f"{r.x0:.1f} {r.y0:.1f} {r.x1:.1f} {r.y1:.1f}"


def list_layers(doc, page):
    rows = all_layers(doc, page)
    print(f"page {page.number} 共 {len(rows)} 个已放置的图像层")
    for li in sorted(rows, key=lambda r: -r["wh"][0] * r["wh"][1]):
        tag = "   ← 透明美术层" if li["mask"] and li["alpha_mean"] < 0.6 else ""
        print(f"  {li['wh'][0]}x{li['wh'][1]:<5} rect=({rect_str(li['rect'])})  "
              f"alpha={li['alpha_mean']:.3f}  xref={li['xref']}{tag}")
    arts = [li for li in rows if li["mask"] and li["alpha_mean"] < 0.6]
    if arts:
        top = max(arts, key=lambda li: li["wh"][0] * li["wh"][1])
        print(f"\n候选标题层（透明美术层里像素面积最大）：\n"
              f"  --title-size {top['wh'][0]} {top['wh'][1]} "
              f"--title-rect {rect_str(top['rect'])}")
        print("  核对无误后照抄进换图命令。--art 的画布尺寸应与此 size 一致。")
    return rows


def find_layer(doc, page, size=None, rect=None, tol=1.5):
    """按「像素尺寸 + 放置矩形」定位图像层。tol 单位 pt（管线重写后浮点误差 < 0.02）。"""
    for li in all_layers(doc, page):
        if size and li["wh"] != tuple(size):
            continue
        if rect and any(abs(li["rect"][i] - rect[i]) > tol for i in range(4)):
            continue
        return li
    return None


def find_shadow(doc, page, title):
    """阴影层：其它带透明蒙版、与标题放置矩形重叠最大的层。"""
    tr = title["rect"]
    ta = max(1e-6, (tr.x1 - tr.x0) * (tr.y1 - tr.y0))
    best, best_ov = None, 0.0
    for li in all_layers(doc, page):
        if li["xref"] == title["xref"] or not li["mask"]:
            continue
        r = li["rect"]
        ox = max(0.0, min(tr.x1, r.x1) - max(tr.x0, r.x0))
        oy = max(0.0, min(tr.y1, r.y1) - max(tr.y0, r.y0))
        ov = (ox * oy) / ta
        if ov > best_ov:
            best, best_ov = li, ov
    return best


def set_image(doc, xref, raw, colorspace):
    """写**解压后的原始像素**并显式声明滤镜与色彩空间。见文件头两条坑。"""
    doc.update_stream(xref, raw)
    doc.xref_set_key(xref, "Filter", "/FlateDecode")
    doc.xref_set_key(xref, "ColorSpace", "/DeviceRGB" if colorspace == 3 else "/DeviceGray")
    doc.xref_set_key(xref, "BitsPerComponent", "8")
    doc.xref_set_key(xref, "DecodeParms", "null")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--page", type=int, default=0, help="封面页序号（0 基）")
    ap.add_argument("--list", action="store_true", help="只列出图像层供选择")
    ap.add_argument("--title-size", nargs=2, type=int, metavar=("W", "H"),
                    help="标题图层的像素尺寸")
    ap.add_argument("--title-rect", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="标题图层的放置矩形（pt）")
    ap.add_argument("--shadow-rect", nargs=4, type=float, metavar=("X0", "Y0", "X1", "Y1"),
                    help="阴影图层矩形；默认自动找重叠最大者")
    ap.add_argument("--art", default="", help="RGBA 标题美术图；不传则直通")
    ap.add_argument("--shadow", type=float, default=0.55, help="阴影不透明度")
    args = ap.parse_args()

    doc = pymupdf.open(args.src)
    page = doc[args.page]

    if args.list:
        list_layers(doc, page)
        return

    if not args.title_rect and not args.title_size:
        raise SystemExit("需要 --title-rect（或 --title-size）；先用 --list 查看")
    if not args.out:
        raise SystemExit("需要 --out")
    if not args.art:
        shutil.copy(args.src, args.out)
        print(f"[cover] 未提供 --art，直通 -> {args.out}")
        return

    title = find_layer(doc, page, args.title_size, args.title_rect)
    if title is None:
        raise SystemExit("按给定的尺寸/矩形找不到标题图层，用 --list 核对")
    tw, th = title["wh"]
    shadow = (find_layer(doc, page, rect=args.shadow_rect) if args.shadow_rect
              else find_shadow(doc, page, title))

    art = Image.open(args.art).convert("RGBA").resize((tw, th), Image.LANCZOS)
    alpha = art.getchannel("A")
    rgb = Image.new("RGB", (tw, th), (0, 0, 0))
    rgb.paste(art.convert("RGB"), (0, 0), alpha)

    # --- 标题本体：原始像素装底图，同一张 alpha 装蒙版（各自按自身尺寸缩放）---
    set_image(doc, title["xref"], rgb.tobytes(), 3)
    if title["mask"]:
        mwh = image_size(doc, title["mask"]) or (tw, th)
        set_image(doc, title["mask"], alpha.resize(mwh, Image.LANCZOS).tobytes(), 1)

    # --- 阴影层：底图（纯黑）不动，按新字形重生成它的蒙版 ---
    if shadow and shadow["mask"]:
        mwh = image_size(doc, shadow["mask"]) or shadow["wh"]
        arr = alpha.resize(mwh, Image.LANCZOS)
        arr = arr.filter(ImageFilter.GaussianBlur(max(1.0, mwh[0] / 120.0)))
        arr = np.roll(np.asarray(arr).astype(np.float32) * args.shadow,
                      int(round(mwh[1] * 0.0135)), axis=0)
        set_image(doc, shadow["mask"], arr.astype(np.uint8).tobytes(), 1)
        print(f"[cover] 阴影层 {shadow['wh'][0]}x{shadow['wh'][1]} 的蒙版已按新字形重建")
    else:
        print("[cover] 未找到阴影层，跳过")

    doc.save(args.out, garbage=4, clean=True, deflate=True, deflate_images=True)
    doc.close()
    print(f"[cover] 标题已替换 -> {args.out}  ({tw}x{th})")


if __name__ == "__main__":
    main()
