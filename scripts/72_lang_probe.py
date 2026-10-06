#!/usr/bin/env python3
"""72_lang_probe.py — 多语言能力实测：把同一句话用多种语言渲染出来，检查字形与排版。

目的：回答「X-Translation 能否从英译中拓展到其他语言」。
关注三件事：① 字形是否齐（缺字会变豆腐块）；② 换行/字距是否正常；③ RTL 语言是否可用。

输出：work/proto/lang_matrix.png
"""
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
FONTS = BASE / "work" / "fonts"
OUT = BASE / "work" / "proto" / "lang_matrix.png"

SAMPLES = [
    ("中文 zh-CN", "NotoSerifSC-Regular.ttf", "ltr",
     "科技公司占全球百强约 35%（按数量）——这一比例仍在上升"),
    ("日文 ja-JP", "NotoSerifJP-VF.ttf", "ltr",
     "テック企業は世界トップ100の約35％を占める（社数ベース）——この比率は上昇中"),
    ("韩文 ko-KR", "NotoSerifKR-VF.ttf", "ltr",
     "테크 기업은 세계 100대 기업의 약 35%를 차지합니다 — 이 비율은 계속 상승 중"),
    ("俄文 ru-RU", "RobotoSerif-Regular.ttf", "ltr",
     "Технологические компании составляют около 35% из 100 крупнейших — доля растёт"),
    ("希腊文 el-GR", "RobotoSerif-Regular.ttf", "ltr",
     "Οι εταιρείες τεχνολογίας αποτελούν περίπου το 35% των 100 μεγαλύτερων"),
    ("阿拉伯文 ar-SA（RTL）", "NotoSerifSC-Regular.ttf", "rtl",
     "تشكل شركات التقنية حوالي 35% من أكبر 100 شركة"),
    ("泰文 th-TH", "NotoSerifSC-Regular.ttf", "ltr",
     "บริษัทเทคโนโลยีคิดเป็นประมาณ 35% ของ 100 บริษัทที่ใหญ่ที่สุด"),
]


def main():
    doc = pymupdf.open()
    W, H = 480, 46
    page = doc.new_page(width=W * 2, height=H * len(SAMPLES) + 20)
    y = 10
    for label, font, direction, text in SAMPLES:
        css = (f'@font-face {{ font-family:"t"; src:url("{FONTS}/{font}"); }}\n'
               'body{margin:0;padding:0;} div{margin:0;padding:0;} '
               'p{margin:0;padding:0;} span{margin:0;padding:0;}')
        # 标签
        page.insert_htmlbox(pymupdf.Rect(8, y, 12 + 150, y + 12),
                            f'<div style="font-family:t; font-size:7pt; color:#888;">{label}</div>',
                            css=css)
        rect = pymupdf.Rect(170, y, W * 2 - 10, y + H - 6)
        spare, scale = page.insert_htmlbox(
            rect,
            f'<div dir="{direction}" style="font-family:t; font-size:11pt; color:#111; '
            f'text-align:{"right" if direction=="rtl" else "left"};">{text}</div>',
            css=css, scale_low=0.2)
        page.draw_rect(rect, color=(0.85, 0.85, 0.85), width=0.3)
        y += H
    OUT.parent.mkdir(parents=True, exist_ok=True)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(2.2, 2.2), alpha=False)
    pix.save(OUT)
    print(f"saved -> {OUT.relative_to(BASE)}  {pix.width}x{pix.height}")


if __name__ == "__main__":
    main()
