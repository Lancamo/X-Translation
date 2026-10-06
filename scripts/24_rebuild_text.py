#!/usr/bin/env python3
"""
重建 PDF：抹掉原文本 → 原位回填译文。

用法
----
  python 24_rebuild_text.py <translations.json> <输出.pdf> [--pages 1,2,3]

要点
----
* 基线锚定：box.y0 = 原文首行基线 - K，保证译文与原文基线重合。
* 中西文分段字体：中文用思源宋体，拉丁片段用原版同源字体
  （正文 Roboto Serif、大标题 Playfair Display）。既更贴近原版观感，
  也规避 MuPDF 在「字母后紧跟数字」时的字形映射错误。
* 横向严格沿用原文本框，不做扩张，避免改变版面。
"""
import argparse
import html
import json
import os
import re

import pymupdf
from pathlib import Path

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))
WORK = f"{BASE}/work"
FONTS = f"{WORK}/fonts"

# ---------------- 字体族 ----------------
ZH_FILES = {
    "Regular": "NotoSerifSC-Regular.ttf",
    "Medium": "NotoSerifSC-Medium.ttf",
    "Bold": "NotoSerifSC-Bold.ttf",
    "Black": "NotoSerifSC-Black.ttf",
}
LAT_FILES = {
    "Regular": "RobotoSerif-Regular.ttf",
    "Medium": "RobotoSerif-Medium.ttf",
    "Bold": "RobotoSerif-Bold.ttf",
    "Italic": "RobotoSerif-Italic.ttf",
}
DISP_FILES = {
    "Regular": "PlayfairDisplay-Regular.ttf",
    "Bold": "PlayfairDisplay-Bold.ttf",
}

# 原字体 → 中文字重
ZH_WEIGHT = {
    "PlayfairDisplay-Regular": None,       # 按字号决定
    "PlayfairDisplay-Bold": "Bold",
    "PlayfairDisplay-Italic": "Medium",
    "RobotoSerif-Regular": "Regular",
    "RobotoSerif-Medium": "Medium",
    "RobotoSerif-Bold": "Bold",
    "RobotoSerif-Italic": "Regular",
    "RobotoSerif-BoldItalic": "Bold",
    "ArialMT": "Regular",
    "Arial-BoldMT": "Bold",
}

# 原字体 → 拉丁片段所用字族/字重
LAT_STYLE = {
    "PlayfairDisplay-Regular": ("disp", "Regular"),
    "PlayfairDisplay-Bold": ("disp", "Bold"),
    "PlayfairDisplay-Italic": ("disp", "Regular"),
    "RobotoSerif-Regular": ("lat", "Regular"),
    "RobotoSerif-Medium": ("lat", "Medium"),
    "RobotoSerif-Bold": ("lat", "Bold"),
    "RobotoSerif-Italic": ("lat", "Italic"),
    "RobotoSerif-BoldItalic": ("lat", "Bold"),
    "ArialMT": ("lat", "Regular"),
    "Arial-BoldMT": ("lat", "Bold"),
}

LAT_RE = re.compile(r"[ -~]+")     # ASCII 可打印区间 = 拉丁片段

# —— 中西文等大因子（SYNTHESIS P1-7）——
# 拉丁字形在设计上比同字号汉字显小、重心飘，是"不像原文"的最肉眼可见来源。
# 数值在设计区间（正文 1.05-1.10 / 标题 1.12）内取保守值；放大后若某单元
# 触发排版引擎缩字（rebuild 报告 scale<0.985），优先下调因子而不是硬扛。
LAT_SCALE_BODY = 1.06       # 正文拉丁片段
LAT_SCALE_TITLE = 1.10      # 标题拉丁片段
LAT_TITLE_MIN = 14.0        # ≥此字号按标题算

THIN = "\u2009"             # U+2009 THIN SPACE，汉字↔拉丁间按 clreq §3.2.2
# 纯数字段不插空格（SYNTHESIS：数字/单位/专名须白名单），只有含字母的
# 拉丁段才插；且相邻已是空白、或拉丁段贴着文本首/尾时不插。


def _is_wide(ch):
    return ord(ch) > 0x2E80      # 与 char_w 的 CJK/拉丁分界一致


def _is_wide_punct(ch):
    """全角标点（自带侧向留白，邻接处不再插薄空格，否则视觉上双倍间距）。"""
    o = ord(ch)
    return (0x3000 <= o <= 0x303F or 0xFF00 <= o <= 0xFFEF
            or 0x2018 <= o <= 0x201D)


def pick_zh_weight(font: str, size: float) -> str:
    w = ZH_WEIGHT.get(font)
    if w is None:                   # PlayfairDisplay-Regular
        if size >= 40:
            return "Black"
        if size >= 20:
            return "Bold"
        return "Medium"
    return w


def css_block() -> str:
    # 注意：MuPDF 的 CSS 解析器不支持 `*` 通配选择器，必须显式列出元素。
    parts = ["body{margin:0;padding:0;}", "div{margin:0;padding:0;}",
             "p{margin:0;padding:0;}", "span{margin:0;padding:0;}"]
    for fam, files in (("zh", ZH_FILES), ("lat", LAT_FILES), ("disp", DISP_FILES)):
        for w, fn in files.items():
            parts.append(f'@font-face {{ font-family:"{fam}-{w}"; '
                         f'src:url("{FONTS}/{fn}"); }}')
    return "\n".join(parts)


def color_hex(c: int) -> str:
    return f"#{c & 0xFFFFFF:06x}"


def _f_split(s: str, lat_family: str, size: float = 0.0,
             scale: float = 1.0) -> str:
    """在每个 f 之后切开成多段：连字（ff/fi/fl/ffi/ffl）都在 f 处成形，
    f 永远收在段尾，跨段不成连字，且不引入任何额外字符。

    实测（2026-10-06，work/liga_test.pdf）：MuPDF 的 HTML 引擎**忽略**
    font-variant-ligatures:none 与 font-variant:no-common-ligatures，
    连字照做；Roboto Serif Italic 的 fl 连字字形 id 0x039A 落在该字体
    ToUnicode 覆盖之外 → 复制/抽取回退成 chr(0x039A)='Κ'，SnowΚake。
    唯一被证明有效的抑制手段就是切 span。
    """
    segs, cur = [], ""
    for ch in s:
        cur += ch
        if ch == "f":
            segs.append(cur)
            cur = ""
    if cur:
        segs.append(cur)
    fs = f"font-size:{size * scale:.2f}pt;" if scale != 1.0 else ""
    return "".join(f'<span style="font-family:\'{lat_family}\';{fs}">'
                   f'{html.escape(x)}</span>'
                   for x in segs)


def wrap_runs(text: str, lat_family: str, size: float = 0.0,
              lat_scale: float = 1.0) -> str:
    """把 ASCII 片段包进拉丁字体的 span，其余交给中文字体。

    拉丁片段内部再按 _f_split 切开防连字；span 字号乘等大因子；
    含字母的拉丁段与汉字之间插 U+2009（数字段不插、已有空白不插、
    贴文本首尾不插——行首行尾的禁插由"文本首尾不插"近似覆盖）。
    """
    out, pos = [], 0
    for m in LAT_RE.finditer(text):
        if m.start() > pos:
            out.append(html.escape(text[pos:m.start()]))
        run = m.group()
        pre = post = ""
        if any(c.isalpha() for c in run):
            if m.start() > 0:
                prev = text[m.start() - 1]
                if _is_wide(prev) and not _is_wide_punct(prev) and not prev.isspace():
                    pre = THIN
            if m.end() < len(text):
                nxt = text[m.end()]
                if _is_wide(nxt) and not _is_wide_punct(nxt) and not nxt.isspace():
                    post = THIN
        out.append(pre + _f_split(run, lat_family, size, lat_scale) + post)
        pos = m.end()
    if pos < len(text):
        out.append(html.escape(text[pos:]))
    return "".join(out)


def _lat_ratio(text):
    """非空白字符里拉丁字母的占比。占比过半的译文（如 'SaaSpocalypse?'）
    本来就通篇拉丁、与原文字形观感一致，乘等大因子只会撑爆文本框
    （v1.5 实测：纯拉丁标题被引擎缩到 0.909），故不乘。"""
    n = sum(1 for c in text if not c.isspace())
    if not n:
        return 0.0
    return sum(1 for c in text if c.isalpha() and ord(c) < 0x2E80) / n


def build_html(text, zh_weight, lat_family, size, lh_ratio, align, color):
    lat_scale = 1.0
    if _lat_ratio(text) <= 0.5:
        lat_scale = LAT_SCALE_TITLE if size >= LAT_TITLE_MIN else LAT_SCALE_BODY
    style = (f"font-family:'zh-{zh_weight}';font-size:{size:.2f}pt;"
             f"line-height:{lh_ratio:.3f};color:{color_hex(color)};"
             f"text-align:{align};white-space:normal;")
    return f'<p style="{style}">{wrap_runs(text, lat_family, size, lat_scale)}</p>'


def lh_ratio_of(u):
    lh = u["lineheight"]
    r = (lh / u["size"]) if lh else (1.35 if u["size"] <= 14 else 1.22)
    return min(max(r, 1.0), 1.8)


def baseline_k(size, lh_ratio, calib):
    k = calib.get(f"{size}|{round(lh_ratio, 4)}")
    return k if k is not None else size * (0.3 + 0.5 * lh_ratio)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("translations")
    ap.add_argument("out")
    ap.add_argument("--pages", default="")
    ap.add_argument("--report", default="")
    args = ap.parse_args()

    units = json.load(open(f"{WORK}/units.json", encoding="utf-8"))
    tr = json.load(open(args.translations, encoding="utf-8"))
    try:
        calib = json.load(open(f"{WORK}/baseline_calib.json", encoding="utf-8"))
    except FileNotFoundError:
        calib = {}

    only = {int(x) for x in args.pages.split(",") if x.strip()} if args.pages else set()

    doc = pymupdf.open(SRC)
    css = css_block()
    report, n_replaced = [], 0

    by_page = {}
    for u in units:
        by_page.setdefault(u["page"], []).append(u)

    # 每页的「障碍物」：其他文本单元 + 图片，用于判断文本框能向下延伸多少。
    # 文本框顶部对齐，多余的底部空间不可见，因此向下延伸是安全的，
    # 可以避免译文因行数略多而被无谓地缩小字号。
    obstacles = {}
    for pno in by_page:
        obs = [u["bbox"] for u in by_page[pno]]
        for b in doc[pno].get_text("dict")["blocks"]:
            if b["type"] == 1:
                obs.append(list(b["bbox"]))
        obstacles[pno] = obs

    def room_below(pno, bbox, max_extra):
        """返回文本框底部最多可延伸到的 y。"""
        x0, y1, x1 = bbox[0], bbox[3], bbox[2]
        limit = y1 + max_extra
        for ox0, oy0, ox1, oy1 in obstacles[pno]:
            if ox1 <= x0 + 2 or ox0 >= x1 - 2:      # 水平不相交
                continue
            if oy0 < y1 - 1:                        # 不在下方
                continue
            limit = min(limit, oy0 - 1.5)
        return limit

    for pno in sorted(by_page):
        if only and pno not in only:
            continue
        page = doc[pno]
        todo = []
        for u in by_page[pno]:
            if u["passthrough"]:
                continue
            zh = tr.get(u["id"])
            if zh:
                todo.append((u, zh))
        if not todo:
            continue

        # 1) 抹除原文本（保留图片与矢量线）
        for u, _ in todo:
            for lb in u["lines"]:
                page.add_redact_annot(
                    pymupdf.Rect(lb[0] - 0.6, lb[1] - 0.6, lb[2] + 0.6, lb[3] + 0.6))
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_REMOVE)

        # 2) 回填译文
        for u, zh in todo:
            size = u["size"]
            lh_ratio = lh_ratio_of(u)
            zh_w = pick_zh_weight(u["font"], size)
            lat_fam, lat_w = LAT_STYLE.get(u["font"], ("lat", "Regular"))

            x0, y0, x1, y1 = u["bbox"]
            k = baseline_k(size, lh_ratio, calib)
            delta = (u["baseline"] - k) - y0
            # 向下延伸到下一个障碍物，最多多容纳 3 行
            bottom = room_below(pno, u["bbox"], 3.0 * lh_ratio * size)
            box = pymupdf.Rect(x0, y0 + delta, x1,
                               min(bottom + delta, page.rect.height - 2))

            # 单行单元：行高的多余部分是纯行距空白。若下方紧贴障碍物导致文本框
            # 装不下一个行高，排版引擎会把整行缩字（如 p20u03 被缩到 0.926）。
            # 基线锚定在 box.y0 + k，向下补足只增加空白，字形位置不变。
            # 仅在缺口不超过 0.3em 时补，避免真的压到下一段文字。
            if len(u["lines"]) == 1:
                need = lh_ratio * size
                deficit = need - box.height
                if 0 < deficit <= 0.3 * size:
                    box.y1 = min(box.y0 + need, page.rect.height - 2)

            htm = build_html(zh, zh_w, f"{lat_fam}-{lat_w}", size,
                             lh_ratio, u["align"], u["color"])
            spare, scale = page.insert_htmlbox(box, htm, css=css, scale_low=0.7)
            n_replaced += 1
            report.append({"id": u["id"], "page": pno, "scale": round(scale, 3),
                           "spare": round(spare, 1), "size": size,
                           "src_len": len(u["text"]), "zh_len": len(zh)})

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    # insert_htmlbox 会在每页各嵌入一份完整字体（思源宋体每字重 ~14MB），
    # 不处理会让 90 页的文件膨胀到 GB 级。subset_fonts() 把字体裁到实际用到的
    # 字形并合并重复对象，garbage=4/clean 再做一次全局去重。
    try:
        doc.subset_fonts()
    except Exception as e:                      # noqa: BLE001
        print("subset_fonts 跳过:", e)
    doc.save(args.out, garbage=4, clean=True, deflate=True,
             deflate_images=True, deflate_fonts=True)
    doc.close()

    print(f"replaced units: {n_replaced} -> {args.out}")
    shrunk = [r for r in report if r["scale"] < 0.985]
    print(f"auto-shrunk (scale<0.985): {len(shrunk)}")
    for r in sorted(shrunk, key=lambda r: r["scale"])[:20]:
        print(f"   {r['id']} scale={r['scale']} size={r['size']} "
              f"src={r['src_len']} zh={r['zh_len']}")
    if args.report:
        json.dump(report, open(args.report, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
