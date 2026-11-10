#!/usr/bin/env python3
"""10_probe_pdf.py — PDF 类型侦察与 OCR 路由判定。

默认输出人类可读报告；--json 输出给编排脚本消费的单个 JSON 对象。
路由只回答「要不要先走 OCR」，不替代人工对图像质量和版式的判断。
"""
import argparse
import collections
import json
import os
from pathlib import Path

import pymupdf

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))


def probe(src):
    doc = pymupdf.open(src)
    fonts = collections.Counter()
    font_sizes = collections.Counter()
    page_chars = []
    full_page_images = 0
    img_count = 0
    img_bytes = 0
    page_sizes = []

    for page in doc:
        page_sizes.append([round(page.rect.width, 2), round(page.rect.height, 2)])
        d = page.get_text("dict")
        chars = 0
        page_has_full_image = False
        for b in d["blocks"]:
            if b["type"] == 1:
                img_count += 1
                img_bytes += len(b.get("image", b""))
                r = pymupdf.Rect(b["bbox"])
                page_area = max(page.rect.width * page.rect.height, 1)
                if r.width * r.height / page_area >= 0.80:
                    page_has_full_image = True
                continue
            for line in b.get("lines", []):
                for s in line["spans"]:
                    text = s["text"]
                    fonts[s["font"]] += len(text)
                    font_sizes[round(s["size"], 1)] += len(text)
                    chars += len(text)
        page_chars.append(chars)
        if page_has_full_image:
            full_page_images += 1
    doc.close()

    pages = len(page_chars)
    avg_chars = sum(page_chars) / max(pages, 1)
    has_text_layer = sum(page_chars) > 0
    if avg_chars < 50:
        pdf_type = "scan"
        route = "ocr"
    elif img_count:
        pdf_type = "hybrid_or_image_text"
        route = "ocr_plus_text_layer"
    else:
        pdf_type = "editable_or_vector"
        route = "text_layer"

    return {
        "source": str(src), "pages": pages,
        "total_chars": sum(page_chars), "avg_chars_per_page": round(avg_chars, 2),
        "page_chars": page_chars, "has_text_layer": has_text_layer,
        "full_page_images": full_page_images, "images": img_count,
        "image_bytes": img_bytes, "page_sizes": page_sizes,
        "pdf_type": pdf_type, "route": route, "ocr_required": route != "text_layer",
        "fonts": dict(fonts.most_common(40)),
        "font_sizes": {str(k): v for k, v in font_sizes.most_common(25)},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src", nargs="?", default=str(SRC))
    ap.add_argument("--json", action="store_true", help="只输出机器可读 JSON")
    ap.add_argument("--out", default=None, help="同时保存 probe.json")
    args = ap.parse_args()
    data = probe(Path(args.src))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
        return

    print("pages:", data["pages"])
    print("source:", data["source"])
    print("total_chars:", data["total_chars"])
    print("avg_chars_per_page:", data["avg_chars_per_page"])
    print("images:", data["images"], "bytes:", data["image_bytes"])
    print("full_page_images:", data["full_page_images"])
    print("pdf_type:", data["pdf_type"])
    print("route:", data["route"])
    print("ocr_required:", data["ocr_required"])
    print("page_sizes (first 5):", data["page_sizes"][:5])
    print("\n-- fonts (by char count) --")
    for f, c in data["fonts"].items():
        print(f"  {c:>8}  {f}")
    print("\n-- chars per page (first 60) --")
    print(data["page_chars"][:60])
    print("pages with <50 chars:", [i + 1 for i, c in enumerate(data["page_chars"]) if c < 50])

    if data["route"] == "ocr":
        print("\n下一步（扫描型）：")
        print("  python3 scripts/10b_render_ocr.py --pages 1-5 --deskew")
        print("  python3 scripts/18_ocr_qc.py --recheck 5")
    elif data["route"] == "ocr_plus_text_layer":
        print("\n下一步（混合型）：保留 L1 文字层，同时运行 10b OCR 侦察图像文字。")


if __name__ == "__main__":
    main()
