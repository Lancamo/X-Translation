#!/usr/bin/env python3
"""64b_visual_regress.py — 三维视觉回归：逐页 SSIM + 掩膜外 AE + 图层矩形 IoU。

解决什么问题：T2 只人看 10% 抽检页，其余 90% 没有任何自动报警。本脚本把
「源 vs 成品」逐页光栅化比对，自动发现"某块被改坏/重排/漂移"，作为 T2 的
强制池来源之一（与 65_sample_review.py 联动）。

三维（并表不并分——各自独立出数，不合成总分）：
  1. SSIM   ：整页结构相似度（排序信号；对反锯齿敏感，光栅化参数必须固定）。
  2. AE     ：**允许变化区掩膜之外**的绝对像素差。掩膜 = 源文字 span +
              L2/L2b 补丁矩形 + L3 标题替换矩形 + 封面标题矩形。
              ⚠️ 没有掩膜这个断言必然满屏假警报：L3 封面/标题位图是**故意**
              改的，clean/deflate 也会带来非零差异。
  3. IoU    ：源↔成品逐页图像放置矩形的平均交并比（原位管线应 ≈1.0；
              下降 = 版面漂移）。文本几何由"往原始 bbox 里回填"构造保证，
              由 T1/T2 覆盖，这里不重复。

阈值口径：**一律取本项目自身分位数**（本脚本自动按全文档分布取底部 5% /
顶部 5% 作 flagged），不写死绝对值。AE 均值 > 0.5/255 或差异像素占比
> 0.1% 的页另立 `ae_warn` 列，供人工判断是掩膜没盖全还是真漂移。

用法：
  python3 64b_visual_regress.py --src input.pdf --pdf output/product.pdf \
      --plans work/stage/plan_l2.json work/stage/plan_micro.json \
      --drop-report work/l4/drop_title_report.json \
      --cover-rect 212.9 139.4 507.1 265.6
输出：work/verify/visual_regress.json + visual/heat_p*.png（最差 5 页热力图）
"""
import argparse
import json
import os
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
OUTDIR = BASE / "work" / "verify"
IMGDIR = OUTDIR / "visual"
AE_TOL = 0.5 / 255          # 掩膜外平均 AE 的 WARN 线（≈ 半个灰阶）
AE_PIX_RATIO = 0.001        # 掩膜外差异像素占比的 WARN 线
HEAT_TOP = 5                # 出热力图的页数（按 SSIM 升序）
PAD_TEXT = 2.5              # 源文字 span 的外扩（pt），盖住反锯齿溢出
PAD_RECT = 1.0              # 显式矩形的外扩（pt）


def rel(p):
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def raster(doc, pno, zoom):
    pix = doc[pno].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    return arr.copy()          # frombuffer 是只读视图，SSIM/减法需要可写


def rect_to_px(r, zoom, h, w):
    x0 = max(0, int(r[0] * zoom)); y0 = max(0, int(r[1] * zoom))
    x1 = min(w, int(np.ceil(r[2] * zoom))); y1 = min(h, int(np.ceil(r[3] * zoom)))
    return x0, y0, x1, y1


def build_mask(shape, zoom, rects):
    """把允许变化区矩形列表画成 bool 掩膜。rects: [(page已定, [x0,y0,x1,y1])]"""
    h, w = shape
    m = np.zeros((h, w), dtype=bool)
    for r in rects:
        x0, y0, x1, y1 = rect_to_px(r, zoom, h, w)
        if x1 > x0 and y1 > y0:
            m[y0:y1, x0:x1] = True
    return m


def page_text_rects(page):
    """源页全部文本**块**的 bbox（允许变化：这里本来就要换字）。

    用块级而不是行级 span：回填后段落会重排，中文落进源行之间的缝隙，
    行级掩膜必然漏（v1.5 实测 18 页假 warn）。块框内的任何重排都算允许。
    """
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        if any(sp.get("text", "").strip() for ln in b["lines"] for sp in ln["spans"]):
            out.append(b["bbox"])
    return out


def page_image_rects(page):
    return [pymupdf.Rect(i["bbox"]) for i in page.get_image_info(xrefs=True)]


def mean_iou(src_rects, prd_rects):
    """矩形集平均 IoU：按面积降序贪心配对，数量不等按短板计。"""
    if not src_rects and not prd_rects:
        return 1.0
    if not src_rects or not prd_rects:
        return 0.0
    a = sorted(src_rects, key=lambda r: abs(r), reverse=True)
    b = list(prd_rects)
    ious = []
    for ra in a:
        best, bi = 0.0, -1
        for i, rb in enumerate(b):
            inter = (ra & rb)
            if inter.is_empty:
                continue
            ia = abs(inter)
            iou = ia / (abs(ra) + abs(rb) - ia)
            if iou > best:
                best, bi = iou, i
        ious.append(best)
        if bi >= 0:
            b.pop(bi)
    return float(np.mean(ious)) if ious else 1.0


def heatmap(a, b, mask, out_path):
    d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
    vis = np.clip(d * 8, 0, 255).astype(np.uint8)      # ×8 放大才看得见小差异
    rgb = np.stack([vis, vis, vis], axis=2)
    rgb[mask, 0] = vis[mask] // 4            # 掩膜区染绿=允许变化
    rgb[mask, 1] = 255 - vis[mask] // 4
    rgb[mask, 2] = vis[mask] // 4
    Image.fromarray(rgb).save(out_path)


def quant(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(len(s) * p)))] if s else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--zoom", type=float, default=2.0,
                    help="光栅化倍率；**必须跨次固定**，否则 SSIM 不可比")
    ap.add_argument("--plans", nargs="*", default=[],
                    help="L2/L2b overlay plan JSON（其 bbox 是故意改动区）")
    ap.add_argument("--drop-report", default="",
                    help="78_drop_title_images --report 的 JSON（L3 标题替换矩形）")
    ap.add_argument("--cover-rect", nargs=4, type=float, default=None,
                    metavar=("X0", "Y0", "X1", "Y1"),
                    help="封面标题矩形（94_cover_title --title-rect，页 1）")
    ap.add_argument("--report", default="work/verify/visual_regress.json")
    args = ap.parse_args()

    src_path = Path(args.src) if Path(args.src).is_absolute() else BASE / args.src
    prd_path = Path(args.pdf) if Path(args.pdf).is_absolute() else BASE / args.pdf
    src, prd = pymupdf.open(src_path), pymupdf.open(prd_path)
    if src.page_count != prd.page_count:
        raise SystemExit(f"页数不一致：源 {src.page_count} / 成品 {prd.page_count}（T0 应已拦）")

    # —— 收集允许变化区 ——
    plans_rects = {}                     # page(1基) -> [rect]
    for pl in args.plans:
        p = Path(pl) if Path(pl).is_absolute() else BASE / pl
        if not p.exists():
            print(f"[warn] plan 不存在，跳过：{rel(p)}")
            continue
        for it in json.loads(p.read_text(encoding="utf-8")).get("items", []):
            if it.get("skip"):
                continue
            x0, y0, x1, y1 = it["bbox"]
            plans_rects.setdefault(it["page"], []).append(
                (x0 - PAD_RECT, y0 - PAD_RECT, x1 + PAD_RECT, y1 + PAD_RECT))
    drop_rects = {}
    if args.drop_report:
        p = Path(args.drop_report) if Path(args.drop_report).is_absolute() else BASE / args.drop_report
        if p.exists():
            for h in json.loads(p.read_text(encoding="utf-8")):
                x0, y0, x1, y1 = h["bbox"]
                drop_rects.setdefault(h["page"], []).append(
                    (x0 - PAD_RECT, y0 - PAD_RECT, x1 + PAD_RECT, y1 + PAD_RECT))
        else:
            print(f"[warn] drop-report 不存在：{rel(p)}")

    pages, worst = [], []
    n = src.page_count
    for i in range(n):
        pno = i + 1
        a = raster(src, i, args.zoom)
        b = raster(prd, i, args.zoom)
        rects = list(plans_rects.get(pno, [])) + list(drop_rects.get(pno, []))
        rects += [(x0 - PAD_TEXT, y0 - PAD_TEXT, x1 + PAD_TEXT, y1 + PAD_TEXT)
                  for x0, y0, x1, y1 in page_text_rects(src[i])]
        if pno == 1 and args.cover_rect:
            x0, y0, x1, y1 = args.cover_rect
            rects.append((x0 - PAD_RECT, y0 - PAD_RECT, x1 + PAD_RECT, y1 + PAD_RECT))
        mask = build_mask(a.shape[:2], args.zoom, rects)

        # SSIM（灰度足够做页级排序信号，省 3/4 内存与时间）
        ga = a.mean(axis=2); gb = b.mean(axis=2)
        ssim = float(_ssim(ga, gb))

        # 掩膜外 AE
        d = np.abs(a.astype(np.int16) - b.astype(np.int16)).max(axis=2)
        outside = d[~mask]
        ae_mean = float(outside.mean()) / 255.0 if outside.size else 0.0
        ae_pix = float((outside > 8).mean()) if outside.size else 0.0   # >8/255 算差异像素

        iou = mean_iou(page_image_rects(src[i]), page_image_rects(prd[i]))

        pages.append({"page": pno, "ssim": round(ssim, 5),
                      "ae_mean": round(ae_mean, 6), "ae_pix_ratio": round(ae_pix, 6),
                      "iou": round(iou, 5),
                      "mask_px_ratio": round(float(mask.mean()), 4)})
        worst.append((ssim, pno))

    # 分位数口径的 flagged：不写死绝对值
    ssims = [p["ssim"] for p in pages]
    ssim_floor = quant(ssims, 0.05)                    # 底部 5%
    aes = [p["ae_mean"] for p in pages]
    ae_floor = quant(aes, 0.95)                        # 顶部 5%
    for p in pages:
        p["flag_ssim"] = p["ssim"] <= ssim_floor
        p["flag_ae"] = p["ae_mean"] >= ae_floor and p["ae_mean"] > 0
        p["warn_ae"] = p["ae_mean"] > AE_TOL or p["ae_pix_ratio"] > AE_PIX_RATIO
        p["flag_iou"] = p["iou"] < 0.995

    worst.sort()
    IMGDIR.mkdir(parents=True, exist_ok=True)
    for f in IMGDIR.glob("heat_p*.png"):
        f.unlink()
    heat = []
    for ss, pno in worst[:HEAT_TOP]:
        a = raster(src, pno - 1, args.zoom)
        b = raster(prd, pno - 1, args.zoom)
        rects = list(plans_rects.get(pno, [])) + list(drop_rects.get(pno, []))
        rects += [(x0 - PAD_TEXT, y0 - PAD_TEXT, x1 + PAD_TEXT, y1 + PAD_TEXT)
                  for x0, y0, x1, y1 in page_text_rects(src[pno - 1])]
        if pno == 1 and args.cover_rect:
            x0, y0, x1, y1 = args.cover_rect
            rects.append((x0 - PAD_RECT, y0 - PAD_RECT, x1 + PAD_RECT, y1 + PAD_RECT))
        mask = build_mask(a.shape[:2], args.zoom, rects)
        out = IMGDIR / f"heat_p{pno:02d}.png"
        heatmap(a, b, mask, out)
        heat.append({"page": pno, "ssim": round(ss, 5), "img": str(rel(out))})

    rep = {"src": str(rel(src_path)), "pdf": str(rel(prd_path)),
           "zoom": args.zoom,
           "thresholds": {"ssim_floor_q05": round(ssim_floor, 5),
                          "ae_ceiling_q95": round(ae_floor, 6),
                          "ae_tol_abs": AE_TOL, "ae_pix_ratio": AE_PIX_RATIO,
                          "note": "阈值取本项目分位数，跨项目不得照抄绝对值"},
           "pages": pages,
           "flags": {"ssim": [p["page"] for p in pages if p["flag_ssim"]],
                     "ae": [p["page"] for p in pages if p["flag_ae"]],
                     "iou": [p["page"] for p in pages if p["flag_iou"]],
                     "warn_ae": [p["page"] for p in pages if p["warn_ae"]]},
           "heatmaps": heat}
    OUTDIR.mkdir(parents=True, exist_ok=True)
    rp = Path(args.report) if Path(args.report).is_absolute() else BASE / args.report
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")

    n_flag = sum(p["flag_ssim"] or p["flag_ae"] or p["flag_iou"] for p in pages)
    print(f"逐页比对 {n} 页（zoom={args.zoom}）  flagged {n_flag} 页")
    print(f"SSIM  中位 {quant(ssims, 0.5):.4f} / 底部5%线 {ssim_floor:.4f}")
    print(f"AE掩膜外 中位 {quant(aes, 0.5):.6f} / 顶部5%线 {ae_floor:.6f}  "
          f"warn {len(rep['flags']['warn_ae'])} 页")
    ious = [p["iou"] for p in pages]
    print(f"IoU   中位 {quant(ious, 0.5):.4f} / <0.995 的 {len(rep['flags']['iou'])} 页")
    if rep["flags"]["warn_ae"]:
        print(f"[warn] 掩膜外 AE 超容忍线的页：{rep['flags']['warn_ae']}")
        print("       → 先查是不是掩膜没盖全（新改动区没进掩膜），再判是不是真漂移")
    print(f"热力图（SSIM 最差 {len(heat)} 页）-> {rel(IMGDIR)}/")
    print(f"报告 -> {rel(rp)}")


def _ssim(ga, gb):
    """SSIM（灰度）。独立小实现，避免依赖 skimage 也在没有它时报不了。
    高斯加权太重，这里用 8×8 均匀窗步进采样——页级排序信号足够，
    精度要求由"同参光栅化 + 分位数阈值"保障，不追求论文级复现。"""
    from numpy.lib.stride_tricks import sliding_window_view
    C1, C2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    wa = sliding_window_view(ga, (8, 8))[::8, ::8]
    wb = sliding_window_view(gb, (8, 8))[::8, ::8]
    ma, mb = wa.mean(axis=(2, 3)), wb.mean(axis=(2, 3))
    va, vb = wa.var(axis=(2, 3)), wb.var(axis=(2, 3))
    cov = (wa * wb).mean(axis=(2, 3)) - ma * mb
    s = ((2 * ma * mb + C1) * (2 * cov + C2)) / ((ma ** 2 + mb ** 2 + C1) * (va + vb + C2))
    return float(s.mean())


if __name__ == "__main__":
    main()
