#!/usr/bin/env python3
"""侦察 PDF：页数、字体、文本/图片分布。"""
import sys, collections
import pymupdf
import os
from pathlib import Path
BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()

SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))

doc = pymupdf.open(SRC)
print("pages:", doc.page_count)
print("metadata:", doc.metadata.get("title"), "|", doc.metadata.get("producer"))

fonts = collections.Counter()
font_sizes = collections.Counter()
total_chars = 0
img_bytes = 0
img_count = 0

for pno in range(doc.page_count):
    page = doc[pno]
    d = page.get_text("dict")
    for b in d["blocks"]:
        if b["type"] == 1:
            img_count += 1
            img_bytes += len(b.get("image", b""))
            continue
        for l in b.get("lines", []):
            for s in l["spans"]:
                fonts[s["font"]] += len(s["text"])
                font_sizes[round(s["size"], 1)] += len(s["text"])
                total_chars += len(s["text"])

print("total_chars:", total_chars)
print("images:", img_count, "bytes:", img_bytes)
print("\n-- fonts (by char count) --")
for f, c in fonts.most_common(40):
    print(f"  {c:>8}  {f}")
print("\n-- sizes --")
for s, c in font_sizes.most_common(25):
    print(f"  {c:>8}  {s}")

# 页面尺寸样本
for pno in [0, 1, 5, 20]:
    if pno < doc.page_count:
        p = doc[pno]
        print(f"page {pno} rect:", p.rect, "rotation:", p.rotation)

# 每页文本量
print("\n-- chars per page (first 60) --")
counts = []
for pno in range(doc.page_count):
    t = doc[pno].get_text("text")
    counts.append(len(t))
print(counts[:60])
print("...")
print("pages with <50 chars:", [i for i, c in enumerate(counts) if c < 50])
doc.close()
