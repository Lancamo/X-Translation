#!/usr/bin/env python3
"""
基线校准（精确版）：只针对本文档实际用到的 (字号, 行高比) 组合实测 K。

K = insert_htmlbox 文本框顶端 → 首行基线的距离。
回填时 box.y0 = 原文首行基线 - K，使译文与原文基线严格对齐。
"""
import json
import pymupdf
import os
from pathlib import Path

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = f"{BASE}/work"
FONTS = f"{WORK}/fonts"
FONT_FILE = {"Regular": "NotoSerifSC-Regular.ttf"}

css = (f'@font-face {{ font-family:"han"; src:url("{FONTS}/{FONT_FILE["Regular"]}"); }}'
       "\nbody{margin:0;padding:0;} div{margin:0;padding:0;} p{margin:0;padding:0;}")

units = json.load(open(f"{WORK}/units.json", encoding="utf-8"))


def lh_ratio(u):
    lh = u["lineheight"]
    if lh:
        return min(max(lh / u["size"], 1.0), 1.8)
    return 1.35 if u["size"] <= 14 else 1.22


combos = sorted({(u["size"], round(lh_ratio(u), 4))
                 for u in units if not u["passthrough"]})
print("combos to calibrate:", len(combos))
for c in combos:
    print("   size=%-6s lh=%s" % c)

CALIB_Y = 250.0
TXT = "基准对齐测试文本基准对齐"
results = {}
doc = pymupdf.open()
for size, lh in combos:
    page = doc.new_page(width=720, height=405)
    box = pymupdf.Rect(40, CALIB_Y, 680, CALIB_Y + 200)
    html = (f'<p style="font-family:\'han\';font-size:{size}pt;'
            f'line-height:{lh};color:#000">{TXT}</p>')
    page.insert_htmlbox(box, html, css=css, scale_low=0.1)
    got = None
    for b in page.get_text("dict")["blocks"]:
        if b["type"]:
            continue
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["text"].strip():
                    got = s["origin"][1]
                    break
            if got is not None:
                break
        if got is not None:
            break
    assert got is not None, (size, lh)
    results[f"{size}|{lh}"] = round(got - CALIB_Y, 4)
    doc.delete_page(-1)
doc.close()

json.dump(results, open(f"{WORK}/baseline_calib.json", "w"), indent=1)
print("\n-- K values --")
for k, v in results.items():
    print(f"   {k:<18} K={v}")
