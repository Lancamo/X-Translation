#!/usr/bin/env python3
"""40_apply_overlay.py — 把图内文字原位替换为译文（铺背景色 + 写中文）。

原理：位图里的文字无法从 PDF 层面抹除，只能在原位置盖一层**采样自该处的背景色**，
再用中文字体写上译文。图形本体（柱子、坐标轴、网格、logo）不碰。

字号自适应（两步搜索，试算在临时文档里做，避免重复写入）：
  1. 从 `h * size_ratio` 起步，若溢出就按 0.94 逐步缩小，直到不溢出；
  2. 再不溢出的前提下向上试探（每步 +8%），直到贴近 `h * size_cap` 或开始溢出。
  这样中文既能"尽量占满原英文的宽度"，又不会超过原文行高的 1.15 倍。

输入 plan（JSON）：
{
  "source": "output/xxx.pdf", "output": "output/xxx_v1.1.pdf",
  "zoom": 4, "font": "NotoSerifSC-Regular.ttf",
  "defaults": {"pad": 0.7, "size_ratio": 0.78, "size_cap": 1.15, "scale_floor": 0.985},
  "items": [{"page":6, "bbox":[x0,y0,x1,y1], "zh":"译文", "size":null,
             "align":"left", "rotate":0, "color":null, "bg":null, "skip":false}]
}

用法：python 40_apply_overlay.py work/overlay_plan.json
"""
import html
import json
import re
import sys
from pathlib import Path

import numpy as np
import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
FONTS = BASE / "work" / "fonts"


def rel(p):
    """相对 BASE 的显示路径；在 BASE 外就原样显示。不在这里抛异常——
    输出路径来自计划文件，写到项目外是合法的，不该在收尾那一行崩掉。"""
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def render(doc, pno, zoom):
    pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)


def crop_of(arr, bbox, zoom, pad=0.0):
    x0 = max(0, int((bbox[0] - pad) * zoom)); y0 = max(0, int((bbox[1] - pad) * zoom))
    x1 = min(arr.shape[1], int((bbox[2] + pad) * zoom)); y1 = min(arr.shape[0], int((bbox[3] + pad) * zoom))
    if x1 <= x0 or y1 <= y0:
        return None
    return arr[y0:y1, x0:x1]


def sample_bg(arr, bbox, zoom):
    """背景色：bbox 内像素众数；众数占比过低（<0.25）时退化为「亮度上 40% 像素的众数」。"""
    c = crop_of(arr, bbox, zoom)
    if c is None or c.size == 0:
        return (255, 255, 255), 0.0
    q = (c // 4 * 4).reshape(-1, 3)
    vals, counts = np.unique(q, axis=0, return_counts=True)
    i = counts.argmax()
    ratio = counts[i] / len(q)
    if ratio >= 0.25:
        return tuple(int(v) for v in vals[i]), float(ratio)
    lum = c.mean(axis=2)
    sel = c[lum >= np.percentile(lum, 60)]
    q2 = (sel // 4 * 4).reshape(-1, 3)
    v2, c2 = np.unique(q2, axis=0, return_counts=True)
    return tuple(int(v) for v in v2[c2.argmax()]), float(ratio)


def sample_text_color(arr, bbox, zoom, bg):
    c = crop_of(arr, bbox, zoom)
    if c is None or c.size == 0:
        return (0, 0, 0)
    d = np.abs(c.astype(np.int16) - np.array(bg, dtype=np.int16)).max(axis=2)
    sel = c[d > 90].reshape(-1, 3)
    if sel.size == 0:
        return (34, 34, 34)
    q = sel // 8 * 8
    vals, counts = np.unique(q, axis=0, return_counts=True)
    return tuple(int(v) for v in vals[counts.argmax()])


def build_css(fontfile, fontfile_lat):
    return (f'@font-face {{ font-family:"zh"; src:url("{FONTS}/{fontfile}"); }}\n'
            f'@font-face {{ font-family:"lat"; src:url("{FONTS}/{fontfile_lat}"); }}\n'
            'body{margin:0;padding:0;} div{margin:0;padding:0;} '
            'p{margin:0;padding:0;} span{margin:0;padding:0;}')


LAT_RE = re.compile(r"[0-9A-Za-z]+")   # 数字+字母走拉丁字体；标点留在中文字体

# —— 中西文等大因子与间距（与 30_rebuild 同口径）——
LAT_SCALE_BODY = 1.06
LAT_SCALE_TITLE = 1.10
LAT_TITLE_MIN = 14.0
THIN = "\u2009"


def _is_wide(ch):
    return ord(ch) > 0x2E80


def _is_wide_punct(ch):
    """全角标点（自带侧向留白，邻接处不再插薄空格）。"""
    o = ord(ch)
    return (0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF
            or 0x2018 <= o <= 0x201D)


def _f_split(s, scale, size):
    """在每个 f 之后切开成多段 span：连字都在 f 处成形，f 收在段尾则跨段
    不成连字。实测 MuPDF HTML 引擎忽略 font-variant-ligatures:none，
    这是唯一被证明有效的连字抑制手段（Roboto Serif Italic 的 fl 字形
    0x039A 越出 ToUnicode → 抽取成 'Κ'，见 30_rebuild._f_split 同款注释）。"""
    segs, cur = [], ""
    for ch in s:
        cur += ch
        if ch == "f":
            segs.append(cur)
            cur = ""
    if cur:
        segs.append(cur)
    fs = f"font-size:{size * scale:.2f}pt;" if scale != 1.0 else ""
    return "".join(f'<span style="font-family:lat;{fs}">{html.escape(x)}</span>'
                   for x in segs)


def _lat_ratio(text):
    """非空白字符里拉丁字母的占比；过半的译文不乘等大因子（见 30_rebuild）。"""
    n = sum(1 for c in text if not c.isspace())
    if not n:
        return 0.0
    return sum(1 for c in text if c.isalpha() and ord(c) < 0x2E80) / n


def html_of(zh, size, align, color, weight):
    """数字/字母包进拉丁字体的 span（f 切段防连字 + 等大因子 + 汉字间薄空格）。

    为什么必须路由：insert_htmlbox 用单一 CJK 字体写 ASCII 时，数字的原始
    字形 id（0x7778+digit）落在该字体 ToUnicode 覆盖范围之外——渲染正常、
    OCR 读像素也正常，但复制/搜索/抽取/读屏回退成 chr(字形码)，数字 0-9
    连号变成「睸睹睺…」；a16z → a睹睾z 就是这么来的。拉丁字体里数字落在
    低位码，不越界。CSS font-family:"zh,lat" 无效（HarfBuzz 不会在同一 run
    内逐字形回退），必须显式切 span。
    """
    scale = 1.0
    if _lat_ratio(zh) <= 0.5:
        scale = LAT_SCALE_TITLE if size >= LAT_TITLE_MIN else LAT_SCALE_BODY
    parts, pos = [], 0
    for m in LAT_RE.finditer(zh):
        if m.start() > pos:
            parts.append(html.escape(zh[pos:m.start()]))
        run = m.group()
        pre = post = ""
        if any(c.isalpha() for c in run):
            if m.start() > 0:
                prev = zh[m.start() - 1]
                if _is_wide(prev) and not _is_wide_punct(prev) and not prev.isspace():
                    pre = THIN
            if m.end() < len(zh):
                nxt = zh[m.end()]
                if _is_wide(nxt) and not _is_wide_punct(nxt) and not nxt.isspace():
                    post = THIN
        parts.append(pre + _f_split(run, scale, size) + post)
        pos = m.end()
    if pos < len(zh):
        parts.append(html.escape(zh[pos:]))
    body = "".join(parts)
    return (f'<div style="font-family:zh; font-weight:{weight}; font-size:{size:.2f}pt; '
            f'color:rgb({color[0]},{color[1]},{color[2]}); text-align:{align}; '
            f'word-break:break-all; '
            f'line-height:1.18;">{body}</div>')


def char_w(ch, size):
    """单字符占宽估算：CJK/全角≈1.0em，拉丁≈0.56em。"""
    return size * (1.0 if ord(ch) > 0x2E80 else 0.56)


def n_lines(zh, size, box_w):
    """贪心装箱估算行数。"""
    lines, cur = 1, 0.0
    for ch in zh:
        cw = char_w(ch, size)
        if cur > 0 and cur + cw > box_w + 1e-6:
            lines += 1
            cur = cw
        else:
            cur += cw
    return lines


def fit_size_arith(box_w, box_h, zh, size_hint, size_cap, line_ratio=1.18):
    """二分搜索「不溢出且尽量大」的字号 —— 纯算术，不调用排版引擎。

    为什么不用 insert_htmlbox 试算：每试一次都会往临时文档里嵌入字体资源，
    1,277 条 × 十余次试算会把内存吃光（实测被 OOM Killer 杀在 EXIT=137）。
    插入时仍带 scale_low 兜底，估算偏差由引擎自动微调。
    """
    if box_w <= 0.5 or box_h <= 0.5 or not zh:
        return max(size_hint, 0.5)

    def ok(sz):
        # 留 6% 余量：估算贴边时排版引擎仍会自行缩字，反而更小
        return n_lines(zh, sz, box_w) * sz * line_ratio <= box_h * 0.94 + 0.4

    hi = max(min(size_cap, size_hint * 1.6), 0.5)
    if ok(hi):
        return hi
    lo = 0.5
    if not ok(lo):
        return lo
    for _ in range(30):
        mid = (lo + hi) / 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


def main():
    plan_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "work" / "overlay_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    d = plan.get("defaults", {})
    pad = d.get("pad", 0.7)
    size_ratio = d.get("size_ratio", 0.78)
    size_cap_ratio = d.get("size_cap", 1.15)
    scale_floor = d.get("scale_floor", 0.985)
    zoom = plan.get("zoom", 4)
    fontfile = plan.get("font", "NotoSerifSC-Regular.ttf")
    fontfile_lat = plan.get("font_lat", "RobotoSerif-Regular.ttf")
    css = build_css(fontfile, fontfile_lat)

    doc = pymupdf.open(BASE / plan["source"])

    by_page = {}
    for it in plan["items"]:
        if not it.get("skip"):
            by_page.setdefault(it["page"], []).append(it)

    stat = {"applied": 0, "tight": []}
    for pno in sorted(by_page):
        page = doc[pno - 1]
        arr = render(doc, pno, zoom)
        print(f"  p{pno} ({len(by_page[pno])} 处)", flush=True)
        prepared = []
        for it in by_page[pno]:
            bbox = pymupdf.Rect(it["bbox"])
            bg, bg_ratio = (tuple(it["bg"]), 1.0) if it.get("bg") else sample_bg(arr, it["bbox"], zoom)
            fg = tuple(it["color"]) if it.get("color") else sample_text_color(arr, it["bbox"], zoom, bg)
            hint = it.get("size") or bbox.height * size_ratio
            cap = it.get("size_cap") or bbox.height * size_cap_ratio
            prepared.append((it, bbox, bg, bg_ratio, fg, hint, cap))

        for it, bbox, bg, bg_ratio, fg, hint, cap in prepared:      # 先铺底
            page.draw_rect(bbox + (-pad, -pad, pad, pad), color=None,
                           fill=tuple(v / 255 for v in bg), width=0)
        for it, bbox, bg, bg_ratio, fg, hint, cap in prepared:      # 再写字
            vert = it.get("rotate", 0) in (90, 270)
            target = pymupdf.Rect(bbox)
            sz_hint = hint
            if not it.get("size"):
                n = max(len(it["zh"]), 1)
                if vert:
                    # 竖排：不旋转，改为让中文在窄框里「逐字换行」（等价竖排），
                    # 位置严格沿用原 bbox —— 旋转法会让文字跑偏出框
                    sz_hint = min(sz_hint, target.width * 1.15, target.height / n * 1.35)
                else:
                    # 中文比英文占宽，窄框里全靠自动缩小会缩到不可读；
                    # 先按「一行约能放几个字」估个上限，再交给二分微调
                    sz_hint = min(sz_hint, target.width / n * 1.25)
            sz = fit_size_arith(target.width, target.height, it["zh"], sz_hint, cap)
            spare, scale = page.insert_htmlbox(
                target, html_of(it["zh"], sz, it.get("align", "left"), fg,
                                it.get("weight", 400)),
                css=css, scale_low=0.3, overlay=True)
            stat["applied"] += 1
            if scale < scale_floor or bg_ratio < 0.25:
                stat["tight"].append({"page": pno, "scale": round(float(scale), 3),
                                      "bg_ratio": round(bg_ratio, 2),
                                      "size": round(sz, 2), "zh": it["zh"][:26]})

    out = BASE / plan["output"]
    doc.subset_fonts()
    doc.save(out, garbage=4, clean=True, deflate=True, deflate_fonts=True)
    doc.close()

    print(f"applied {stat['applied']} patches on {len(by_page)} pages")
    print(f"需要留意的（缩放或背景不稳）: {len(stat['tight'])}")
    for t in stat["tight"][:12]:
        print(f"   p{t['page']:>2} size={t['size']:>5} scale={t['scale']:.3f} bg_ratio={t['bg_ratio']} :: {t['zh']!r}")
    print(f"saved -> {rel(out)}  {out.stat().st_size/1e6:.2f} MB")


if __name__ == "__main__":
    main()
