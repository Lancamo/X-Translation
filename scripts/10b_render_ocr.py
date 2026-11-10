#!/usr/bin/env python3
"""10b_render_ocr.py — 扫描型文档的 OCR 入口：渲染 → (去斜/增强) → 整页 OCR。

定位：可编辑型文档的 ocr_raw4.json 由 workflow.md 里的手工片段生成；
扫描型（无文字层）整本都靠 OCR，值得一个正式入口，把去斜、增强、
分页子集、二进制自动编译这些前置步骤收拢成一个脚本。

流程：
  1. 渲染每页（默认 4 倍，zoom 必须 >=4，低倍率小字误识严重）；
  2. --deskew 只估计倾角并报告；--deskew-apply 把页面旋转回正再 OCR，
     并把 OCR 框逆映射回原始页面坐标（补丁坐标必须落在未旋转的页面上）；
  3. --enhance 轻度增强（自动对比度 + 锐化），图像质量差的扫描件用；
  4. 调 scripts/ocr_vision（macOS Vision，本地免费）整页 OCR；
  5. 输出 work/ocr_raw4.json —— 与可编辑型管线的格式完全一致
     （file/conf/text/x/y/w/h，坐标归一化、y 从底部起算），
     下游 12 -> 18 -> 30 -> 31 -> 40 不感知文档类型。

用法：
  python 10b_render_ocr.py --pages 1-5            # 抽检 5 页（接单前判断 OCR 可行性）
  python 10b_render_ocr.py                        # 全量
  python 10b_render_ocr.py --deskew               # 只估倾角，不动手
  python 10b_render_ocr.py --deskew-apply --enhance
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image, ImageEnhance, ImageFilter, ImageOps

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))
SCRIPTS = Path(__file__).resolve().parent
OCR_BIN = SCRIPTS / "ocr_vision"


def ensure_ocr_bin():
    if OCR_BIN.exists():
        return OCR_BIN
    swift = SCRIPTS / "ocr_vision.swift"
    if not swift.exists():
        sys.exit(f"找不到 OCR 二进制，也没有源码: {OCR_BIN}")
    print("ocr_vision 二进制不存在，自动编译（仓库只带 .swift）...")
    subprocess.run(["swiftc", "-O", str(swift), "-o", str(OCR_BIN)], check=True)
    return OCR_BIN


def run_ocr(png_paths):
    """调 ocr_vision，返回 JSON 块列表。图片多时分批，避免单条命令行过长。"""
    exe = ensure_ocr_bin()
    out = []
    CH = 40
    for i in range(0, len(png_paths), CH):
        r = subprocess.run([str(exe)] + [str(p) for p in png_paths[i:i + CH]],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"OCR 失败: {r.stderr[:400]}")
        out.extend(json.loads(r.stdout))
    return out


def estimate_skew(im):
    """估计页面倾角（度）。白底黑字假设：暗像素为墨迹，
    对每个候选角做旋转后按行投影，投影方差最大的角即回正角。"""
    w = 1200
    small = im.convert("L").resize((w, max(1, int(w * im.height / im.width))))
    a = np.asarray(small, dtype=np.float32)
    ink = (a < a.mean() * 0.75).astype(np.uint8) * 255
    ink_im = Image.fromarray(ink)
    best_ang, best_var = 0.0, -1.0
    for ang in np.arange(-5.0, 5.01, 0.25):
        rot = np.asarray(ink_im.rotate(float(ang), resample=Image.BILINEAR,
                                       fillcolor=0), dtype=np.float32) / 255.0
        proj = rot.sum(axis=1)
        v = float(proj.var())
        if v > best_var:
            best_var, best_ang = v, float(ang)
    return best_ang


def rot_forward(p, c0, c1, ang):
    """原始像素坐标 -> 旋转后像素坐标（y 向下，视觉逆时针 ang 度，expand）。"""
    rad = np.deg2rad(ang)
    ca, sa = np.cos(rad), np.sin(rad)
    dx, dy = p[0] - c0[0], p[1] - c0[1]
    return (ca * dx + sa * dy + c1[0], -sa * dx + ca * dy + c1[1])


def rot_back(p, c0, c1, ang):
    """旋转后像素坐标 -> 原始像素坐标（rot_forward 的逆）。"""
    rad = np.deg2rad(ang)
    ca, sa = np.cos(rad), np.sin(rad)
    dx, dy = p[0] - c1[0], p[1] - c1[1]
    return (ca * dx - sa * dy + c0[0], sa * dx + ca * dy + c0[1])


def map_box_back(box_norm, W1, H1, W0, H0, ang):
    """OCR 框（归一化、y 自底部）从旋转图逆映射回原始页面的归一化框。"""
    x0, y0 = box_norm["x"] * W1, (1 - box_norm["y"] - box_norm["h"]) * H1
    x1, y1 = (box_norm["x"] + box_norm["w"]) * W1, (1 - box_norm["y"]) * H1
    c0, c1 = (W0 / 2, H0 / 2), (W1 / 2, H1 / 2)
    corners = [rot_back((x0, y0), c0, c1, ang), rot_back((x1, y0), c0, c1, ang),
               rot_back((x0, y1), c0, c1, ang), rot_back((x1, y1), c0, c1, ang)]
    xs = [min(max(c[0], 0), W0) for c in corners]
    ys = [min(max(c[1], 0), H0) for c in corners]
    return {"x": min(xs) / W0, "y": 1 - max(ys) / H0,
            "w": (max(xs) - min(xs)) / W0, "h": (max(ys) - min(ys)) / H0}


def enhance(im):
    im = ImageOps.autocontrast(im.convert("RGB"), cutoff=1)
    im = ImageEnhance.Contrast(im).enhance(1.15)
    return im.filter(ImageFilter.UnsharpMask(radius=1, percent=80, threshold=2))


def parse_pages(spec, n):
    """'1-5,12' -> [1,2,3,4,5,12]；None -> 全部。"""
    if not spec:
        return list(range(1, n + 1))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    bad = [p for p in out if p < 1 or p > n]
    if bad:
        sys.exit(f"--pages 超出范围: {bad}（共 {n} 页）")
    return sorted(set(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--out", default=str(BASE / "work" / "ocr_raw4.json"))
    ap.add_argument("--zoom", type=float, default=4.0)
    ap.add_argument("--pages", default=None, help="如 '1-5,12'，缺省全部")
    ap.add_argument("--pages-dir", default=str(BASE / "work" / "ocr_pages4"))
    ap.add_argument("--deskew", action="store_true", help="只估计并报告倾角")
    ap.add_argument("--deskew-apply", action="store_true",
                    help="回正后再 OCR，并把框逆映射回原页面坐标")
    ap.add_argument("--enhance", action="store_true", help="轻度增强（对比度+锐化）")
    ap.add_argument("--deskew-min", type=float, default=0.3,
                    help="|倾角| 低于此度数不旋转")
    args = ap.parse_args()

    zoom = args.zoom
    if zoom < 4:
        print("[warn] zoom < 4：5pt 以下小字误识会明显变多（实测 2.2 倍出 $OB 类错读）")

    doc = pymupdf.open(args.src)
    pages = parse_pages(args.pages, doc.page_count)
    pages_dir = Path(args.pages_dir)
    pages_dir.mkdir(parents=True, exist_ok=True)

    meta = {"zoom": zoom, "src": str(args.src), "pages": pages,
            "deskew_requested": bool(args.deskew or args.deskew_apply),
            "deskew_applied": bool(args.deskew_apply),
            "enhanced": bool(args.enhance), "angles": {}}
    rendered = []   # (pno, png_path, ang, W0, H0)  W0/H0 = 未旋转的原始像素尺寸
    results = []

    # —— 阶段一：渲染（+去斜/增强）。与 OCR 分开，OCR 才能大批量喂给同一个进程 ——
    for pno in pages:
        page = doc[pno - 1]
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        W0, H0 = pix.width, pix.height

        ang = 0.0
        if args.deskew or args.deskew_apply:
            ang = estimate_skew(im)
            meta["angles"][str(pno)] = round(ang, 2)
            if abs(ang) < args.deskew_min:
                ang = 0.0
            if args.deskew and abs(ang) >= args.deskew_min:
                print(f"  p{pno}: 倾角 {meta['angles'][str(pno)]}°"
                      + ("（超 --deskew-min，建议 --deskew-apply）" if not args.deskew_apply else ""))
        if args.deskew_apply and abs(ang) >= args.deskew_min:
            im = im.rotate(ang, expand=True, resample=Image.BICUBIC, fillcolor=(255, 255, 255))
        if args.enhance:
            im = enhance(im)

        png = pages_dir / f"p{pno:03d}.png"
        im.save(png)
        rendered.append((pno, png, ang, W0, H0))
        print(f"  渲染 p{pno}" + (f"（回正 {ang:+.2f}°）" if ang else ""), flush=True)

    # —— 阶段二：整页 OCR，坐标逆映射回原始页面 ——
    all_blocks = run_ocr([png for _, png, _, _, _ in rendered])
    by_file = {}
    for b in all_blocks:
        by_file.setdefault(b["file"], []).append(b)

    for pno, png, ang, W0, H0 in rendered:
        im = Image.open(png)
        W1, H1 = im.width, im.height
        for b in by_file.get(png.name, []):
            if not str(b.get("text", "")).strip():
                continue
            if W1 != W0 or H1 != H0:
                box = map_box_back(b, W1, H1, W0, H0, ang)
            else:
                box = {"x": b["x"], "y": b["y"], "w": b["w"], "h": b["h"]}
            results.append({"file": png.name, "conf": b["conf"],
                            "text": b["text"], **box})
        print(f"  OCR p{pno}: {len(by_file.get(png.name, []))} 块")

    doc.close()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")
    (BASE / "work" / "ocr_render_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    confs = [r["conf"] for r in results]
    print(f"\n=== 完成 ===")
    print(f"页数 {len(pages)} / OCR 块 {len(results)}"
          + (f" / 平均置信度 {sum(confs)/len(confs):.3f}" if confs else ""))
    print(f"JSON -> {out.relative_to(BASE) if out.is_relative_to(BASE) else out}")
    print("下一步：python3 12_scan_image_text.py 生成 ocr_scope4.json，"
          "然后 python3 18_ocr_qc.py 过校对门")


if __name__ == "__main__":
    main()
