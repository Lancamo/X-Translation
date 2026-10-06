#!/usr/bin/env python3
"""62_verify_residue.py — 残迹回源核验：源/成品同框裁图对比，判定真伪。

问题：成品 OCR 报出的残迹块里，混着两类东西 ——
  ① 真的英文残迹（源图上有英文，我们没译到或只译了一半）；
  ② OCR 误读（Vision 把**我们自己写的中文**读成了拉丁字母拼串）。
两者用文本本身分不开（都能 conf=1.0 被 is_english 放行）。

解法：对同一个矩形框，分别从**源 PDF** 和**成品 PDF** 裁图（几何完全一致），
各自 OCR，并排对比：
  · 源框读出英文、成品框读出同一英文  -> real    源图确有此英文
  · 成品框读出英文、源框读不出（空白/CJK 碎片） -> noise   成品 OCR 读的是我们写的中文
  · 成品框读出英文、源框读出**另一段**英文   -> lookup  框位串了邻位，需人工看

用法：
  python 62_verify_residue.py --cls unseen,filtered,covered --min-h 4.0
输出：work/residue/verify_pairs.json + unseen_verified.json + 对比图 work/residue/verify_pairs.png
"""
import argparse
import json
import re
import subprocess
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = BASE / "work"
OUT = WORK / "residue"
OCR_BIN = Path(__file__).resolve().parent / "ocr_vision"   # OCR 二进制随 skill 走
CROP_ZOOM = 6.0
MARGIN = 20

ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
STOP = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "has",
        "have", "not", "but", "all", "you", "our", "its", "out", "per", "see"}


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def content_words(s):
    return {w.lower() for w in ASCII_WORD.findall(s) if len(w) >= 3 and w.lower() not in STOP}


def run_ocr(paths):
    exe = Path(str(OCR_BIN))
    if not exe.exists():
        exe = Path(str(OCR_BIN) + ".bin")
    out = subprocess.run([str(exe), *[str(p) for p in paths]],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def crop(doc, pno, rect, path):
    pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(CROP_ZOOM, CROP_ZOOM),
                                  clip=rect, alpha=False)
    im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    canvas = Image.new("RGB", (pix.width + 2 * MARGIN, pix.height + 2 * MARGIN), "white")
    canvas.paste(im, (MARGIN, MARGIN))
    canvas.save(path)
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cls", default="unseen")
    ap.add_argument("--pad", type=float, default=2.0)
    ap.add_argument("--min-h", type=float, default=4.0)
    ap.add_argument("--pdf", default="output/product.pdf",
                    help="成品 PDF（被核验对象）")
    ap.add_argument("--src-pdf", default="input.pdf")
    ap.add_argument("--residue", default="work/residue/residue.json")
    args = ap.parse_args()

    src = pymupdf.open(BASE / args.src_pdf)
    rows = json.loads((BASE / args.residue).read_text(encoding="utf-8"))
    keep_cls = set(args.cls.split(","))
    targets = [r for r in rows if r["cls"] in keep_cls and r["h"] >= args.min_h]
    targets.sort(key=lambda r: -r["h"])

    prod_doc = pymupdf.open(BASE / args.pdf)

    tmp = WORK / "verify_unseen"
    tmp.mkdir(parents=True, exist_ok=True)
    for f in tmp.glob("*.png"):
        f.unlink()

    tasks = []
    for i, r in enumerate(targets):
        p = src[r["page"] - 1]
        x0, y0, x1, y1 = r["bbox"]
        pad = max(args.pad, 0.18 * max(x1 - x0, y1 - y0))
        clip = pymupdf.Rect(max(0, x0 - pad), max(0, y0 - pad),
                            min(p.rect.width, x1 + pad), min(p.rect.height, y1 + pad))
        if clip.width < 1 or clip.height < 1:
            continue
        fs = tmp / f"a{i:03d}_p{r['page']:02d}_src.png"
        # 成品同框：几何与源完全一致
        canvas_src = crop(src, r["page"], clip, fs)
        fp = tmp / f"b{i:03d}_p{r['page']:02d}_prod.png"
        canvas_prod = crop(prod_doc, r["page"], clip, fp)
        tasks.append({"i": i, "r": r, "src": fs, "prod": fp,
                      "canvas": (canvas_src, canvas_prod)})

    if not tasks:
        print("没有待核验项")
        return

    ocr = run_ocr([t["src"] for t in tasks] + [t["prod"] for t in tasks])
    by = {}
    for o in ocr:
        by.setdefault(o["file"], []).append(o["text"])
    rd = lambda p: " ".join(by.get(p.name, []))

    out = []
    for t in tasks:
        r, st, pt = t["r"], rd(t["src"]), rd(t["prod"])
        cs, cp = content_words(st), content_words(pt)
        if cp and cs & cp:
            verdict = "real"
        elif not cs and cp:
            verdict = "noise"
        elif cs and cp and not (cs & cp):
            verdict = "lookup"
        elif not cs and not cp:
            verdict = "noise"
        else:
            verdict = "lookup"
        out.append({**r, "src_text": st[:70], "prod_text": pt[:70], "verdict": verdict})

    # 对比图：每行 = 源 | 成品
    if out:
        thumbs = []
        for t in tasks:
            a, b = t["canvas"]
            h = 90
            aw = max(60, int(a.width * h / a.height))
            bw = max(60, int(b.width * h / b.height))
            thumbs.append((a.resize((aw, h)), b.resize((bw, h)), t["r"]))
        W = 1180
        row_h = 108
        img = Image.new("RGB", (W, row_h * len(thumbs) + 4), "#FAFAFA")
        d = ImageDraw.Draw(img)
        y = 0
        for a, b, r in thumbs:
            img.paste(a, (70, y + 8))
            img.paste(b, (430, y + 8))
            d.text((4, y + 40), f"p{r['page']}", fill="#111")
            d.text((760, y + 40), r["text"][:44], fill="#333")
            d.line([(0, y + row_h - 1), (W, y + row_h - 1)], fill="#DDD")
            y += row_h
        img.save(OUT / "verify_pairs.png")

    (OUT / "verify_pairs.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUT / "unseen_verified.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    from collections import Counter
    cnt = Counter(o["verdict"] for o in out)
    print(f"核验 {len(out)} 处：{dict(cnt)}")
    for v in ("real", "lookup", "noise"):
        grp = [o for o in out if o["verdict"] == v]
        if not grp:
            continue
        print(f"\n--- {v.upper()} ({len(grp)}) ---")
        for o in grp:
            print(f"  p{o['page']:<3} {o['w']:>6.1f}x{o['h']:<5.1f} "
                  f"成:{o['prod_text'][:30]!r:<34} 源:{o['src_text'][:34]!r}")


if __name__ == "__main__":
    main()
