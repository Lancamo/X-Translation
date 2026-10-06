#!/usr/bin/env python3
"""41_drop_title_bitmap.py — 抹掉「标题文字图片」（在译文 PDF 上操作）。

问题：若干页的大标题同时以「透明文字位图 + 文字层」两种形态存在。中文写在文字层，
底下的英文位图没消失；中文通常比英文短，于是侧面露出英文。

做法（无损，不改内容流）：从标题图下方那张全页背景图里**裁出标题图所在位置的原始
背景**，用它替换标题图。效果 = 英文消失、背景无缝衔接。

前提判据（三条同时满足才动手）：
  1. 图片与某个**标题级文字块（字号 >=18pt）**纵向高度高度重合、横向被覆盖；
  2. 图片高度与文字块高度相当（0.5~2.5 倍）——远高于文字块的是侧栏面板图，不动；
  3. 图片的 alpha 蒙版**大部分透明**（mean alpha <= 0.35）——确认是"纯文字图"而非
     不透明设计面板（替换不透明面板会改变底色）。

用法：
  python 41_drop_title_bitmap.py --src <pdf> --out <pdf>
  python 41_drop_title_bitmap.py --src <pdf> --out <pdf> --apply
"""
import argparse
import json
from io import BytesIO
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
ALPHA_MAX = 0.35


def rect_area(r):
    """矩形面积。不用 Rect.get_area()——PyMuPDF 1.26 已没有这个方法。"""
    return abs(r.width * r.height)


def rel(p):
    """相对 BASE 的显示路径；在 BASE 外就原样显示。不在这里抛异常——
    输出路径由命令行给，写到项目外是合法的，不该在收尾那一行崩掉。"""
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def text_blocks(page, min_size=18.0):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b["type"] == 1:
            continue
        txt = "".join(s["text"] for l in b.get("lines", []) for s in l["spans"]).strip()
        size = max([s["size"] for l in b.get("lines", []) for s in l["spans"]] or [0])
        if txt and size >= min_size:
            out.append((pymupdf.Rect(b["bbox"]), txt, size))
    return out


def alpha_mean(doc, xref):
    """图片 alpha 蒙版的平均不透明度（1.0 = 完全不透明）。"""
    try:
        d = doc.extract_image(xref)
        sm = d.get("smask") or 0
        if not sm:
            return 1.0
        sd = doc.extract_image(sm)
        im = Image.open(BytesIO(sd["image"])).convert("L")
        return float(np.asarray(im).mean()) / 255.0
    except Exception:
        return 1.0


def find_targets(doc, page):
    infos = page.get_image_info(xrefs=True)
    if not infos:
        return []
    bg = max(infos, key=lambda i: rect_area(pymupdf.Rect(i["bbox"])))
    bgr = pymupdf.Rect(bg["bbox"])
    hits = []
    for info in infos:
        r = pymupdf.Rect(info["bbox"])
        if info["xref"] == bg["xref"] or r == bgr:
            continue
        if rect_area(r) >= 0.5 * rect_area(bgr):
            continue
        best = None
        for tr, txt, size in text_blocks(page):
            inter = r & tr
            if inter.is_empty:
                continue
            v_cov = inter.height / max(min(r.height, tr.height), 1e-6)
            h_cov = inter.width / max(tr.width, 1e-6)
            h_ratio = r.height / max(tr.height, 1e-6)
            if v_cov > 0.7 and h_cov > 0.5 and 0.5 <= h_ratio <= 2.5:
                score = v_cov * h_cov
                if best is None or score > best[0]:
                    best = (score, txt, size)
        if not best:
            continue
        am = alpha_mean(doc, info["xref"])
        hits.append({
            "page": page.number + 1, "info": info, "bg": bg, "rect": r, "bgr": bgr,
            "text": best[1], "size": best[2], "alpha": round(am, 3),
            "ok": am <= ALPHA_MAX and info["xref"] != 0,
            "why": ("inline image" if info["xref"] == 0
                    else ("面板图(不透明)" if am > ALPHA_MAX else "")),
        })
    return hits


def patch_of(doc, bg, bgr, r):
    bimg = doc.extract_image(bg["xref"])
    pil = Image.open(BytesIO(bimg["image"])).convert("RGB")
    sx, sy = pil.width / bgr.width, pil.height / bgr.height
    box = (max(0, int((r.x0 - bgr.x0) * sx)), max(0, int((r.y0 - bgr.y0) * sy)),
           min(pil.width, int((r.x1 - bgr.x0) * sx)), min(pil.height, int((r.y1 - bgr.y0) * sy)))
    return pil.crop(box)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--report", default="",
                    help="把替换矩形清单写成 JSON（供视觉回归的允许变化区掩膜用）")
    a = ap.parse_args()
    src = Path(a.src) if Path(a.src).is_absolute() else BASE / a.src
    out = Path(a.out) if Path(a.out).is_absolute() else BASE / a.out

    doc = pymupdf.open(src)
    all_hits = []
    for pno in range(doc.page_count):
        all_hits += find_targets(doc, doc[pno])

    print(f"=== 候选 {len(all_hits)} 处 ===")
    for h in all_hits:
        flag = "OK " if h["ok"] else "跳过"
        print(f"  {flag} p{h['page']:>2} xref={h['info']['xref']:>4} alpha={h['alpha']:.3f} "
              f"bbox=({h['rect'].x0:.0f},{h['rect'].y0:.0f},{h['rect'].x1:.0f},{h['rect'].y1:.0f}) "
              f"{h['why']:<14} :: {h['text'][:26]!r}")

    todo = [h for h in all_hits if h["ok"]]
    print(f"\n将替换 {len(todo)} 处；跳过 {len(all_hits)-len(todo)} 处")

    if not a.apply:
        print("（预览模式，未改动文件；加 --apply 执行）")
        return

    n = 0
    for h in todo:
        page = doc[h["page"] - 1]
        buf = BytesIO()
        patch_of(doc, h["bg"], h["bgr"], h["rect"]).save(buf, format="PNG")
        page.replace_image(h["info"]["xref"], stream=buf.getvalue())
        n += 1
    if a.report:
        rp = Path(a.report) if Path(a.report).is_absolute() else BASE / a.report
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(
            [{"page": h["page"],
              "bbox": [round(v, 2) for v in h["rect"]],
              "xref": h["info"]["xref"],
              "text": h["text"][:80]}
             for h in todo], ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"替换矩形清单 -> {rel(rp)}（{len(todo)} 处）")
    out.parent.mkdir(parents=True, exist_ok=True)
    doc.subset_fonts()
    doc.save(out, garbage=4, clean=True, deflate=True, deflate_fonts=True)
    print(f"\n替换 {n} 处；saved -> {rel(out)}  {out.stat().st_size/1e6:.2f} MB")


if __name__ == "__main__":
    main()
