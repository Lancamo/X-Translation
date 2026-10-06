#!/usr/bin/env python3
"""12_scan_image_text.py — 用 OCR 结果量化「图内文字」规模。

思路：OCR 覆盖整页（文字层 + 位图内容）。凡是与文字层文本块位置重合的 OCR 块，
就是文字层渲染出来的文字（已翻译过）；剩下的才是**真正烧在位图里的文字**。

输出：work/ocr_scope.json + 控制台摘要（去重规模、分类、token 估算）
"""
import json
import re
from collections import Counter
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))
OCR = BASE / "work" / "ocr_raw4.json"
OUT = BASE / "work" / "ocr_scope4.json"
OCR_ZOOM = 4.0          # 与 ocr_raw4.json 的渲染倍率一致

NUMERIC = re.compile(r"^[\s\d%.,()'’\-–—+/x×$:#°]*$")
WORDS = re.compile(r"[A-Za-z]{2,}")


def rect_area(r):
    """矩形面积。不用 Rect.get_area()——PyMuPDF 1.26 已没有这个方法。"""
    return abs(r.width * r.height)


def is_numeric(t):
    return bool(NUMERIC.match(t)) or not WORDS.search(t)


def classify(t):
    if is_numeric(t):
        return "skip_numeric"
    n_words = len(WORDS.findall(t))
    if len(t) <= 24 and n_words <= 3 and not re.search(r"[.!?]$", t):
        return "label"
    return "sentence"


def main():
    src = pymupdf.open(SRC)
    ocr = json.loads(OCR.read_text(encoding="utf-8"))

    # 文字层块（页面 pt 坐标）
    text_blocks = []
    for pno in range(src.page_count):
        pg = src[pno]
        for b in pg.get_text("dict")["blocks"]:
            if b["type"] == 1:
                continue
            txt = "".join(s["text"] for l in b.get("lines", []) for s in l["spans"]).strip()
            if txt:
                text_blocks.append((pno + 1, pymupdf.Rect(b["bbox"]), txt))
    src.close()

    per_page = Counter()
    in_image = []          # 图内文字（未与文字层重合的 OCR 块）
    on_text_layer = 0

    for o in ocr:
        pno = int(re.search(r"p(\d+)", o["file"]).group(1))
        r = pymupdf.Rect(
            o["x"] * 720, (1 - o["y"] - o["h"]) * 405,
            (o["x"] + o["w"]) * 720, (1 - o["y"]) * 405,
        )
        area = rect_area(r)
        if area < 1:
            continue
        matched = False
        for tp, tr, tt in text_blocks:
            if tp != pno:
                continue
            inter = rect_area(r & tr)
            if inter / max(area, 1) > 0.55:
                matched = True
                break
        if matched:
            on_text_layer += 1
            continue
        t = o["text"].strip()
        if not t:
            continue
        in_image.append({
            "page": pno, "text": t, "conf": o["conf"],
            "bbox": [round(v, 1) for v in (r.x0, r.y0, r.x1, r.y1)],
            "cls": classify(t),
        })
        per_page[pno] += 1

    uniq = Counter(x["text"] for x in in_image)
    by_cls = Counter(x["cls"] for x in in_image)
    uniq_cls = {c: sum(1 for t, n in uniq.items()
                       if classify(t) == c) for c in ("skip_numeric", "label", "sentence")}
    uniq_chars = {c: sum(len(t) for t in uniq if classify(t) == c)
                  for c in ("skip_numeric", "label", "sentence")}

    data = {
        "summary": {
            "ocr_blocks_total": len(ocr),
            "on_text_layer": on_text_layer,
            "in_image_blocks": len(in_image),
            "in_image_chars": sum(len(x["text"]) for x in in_image),
            "unique_strings": len(uniq),
            "by_class_blocks": dict(by_cls),
            "by_class_unique": uniq_cls,
            "by_class_unique_chars": uniq_chars,
            "pages_with_in_image_text": len(per_page),
            "top_pages": per_page.most_common(12),
        },
        "in_image": in_image,
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    s = data["summary"]
    print("=== OCR 总览 ===")
    print(f"OCR 块总数              : {s['ocr_blocks_total']}")
    print(f"  落在文字层上(已翻译)   : {s['on_text_layer']}")
    print(f"  落在位图里(图内文字)   : {s['in_image_blocks']}  字符 {s['in_image_chars']}")
    print()
    print("=== 图内文字按类型（块 / 去重后条数 / 去重字符数）===")
    for c in ("skip_numeric", "label", "sentence"):
        print(f"  {c:<13}: {s['by_class_blocks'].get(c,0):>5} / {s['by_class_unique'].get(c,0):>4} / {s['by_class_unique_chars'].get(c,0):>6}")
    print()
    print(f"去重后唯一字符串总数: {s['unique_strings']}")
    print(f"含图内文字的页数    : {s['pages_with_in_image_text']} / 90")
    print(f"图内文字最多的页    : {s['top_pages']}")
    print()
    print("=== 图内文字示例（每页最多 6 条，前 12 页）===")
    shown_pages = 0
    cur = None
    cnt = 0
    for x in in_image:
        if x["page"] != cur:
            cur = x["page"]
            cnt = 0
            shown_pages += 1
            if shown_pages > 12:
                break
            print(f"  -- p{cur}")
        if cnt >= 6:
            continue
        cnt += 1
        print(f"     [{x['cls']:<12}] {x['text'][:78]}")
    print(f"\nJSON -> {OUT.relative_to(BASE)}")


if __name__ == "__main__":
    main()
