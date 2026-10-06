#!/usr/bin/env python3
"""13_scan_vertical.py — 竖排（旋转）文字补扫。

问题：macOS Vision 在整页扫描模式下**基本读不出旋转 90° 的文字**（实测
'Indexed Market Cap' / 'Median Panelist AI Vendor Spend' 这类纵轴标题整块漏掉），
而它们在成品里是肉眼可见的英文残迹。

解法：把页面图顺时针旋转 90° 再 OCR —— 旋转文字就变成横排，Vision 正常识别；
再把坐标映射回页面坐标系，与主扫描结果合并去重。

坐标映射（归一化）：旋转图 (u',v') → 原图 (u,v) = (v', 1-u')
  即原 bbox = (v0', 1-u1', v1', 1-u0')

用法：
  python 13_scan_vertical.py probe --pages 12,33,54,36     # 只看这几页，核验映射
  python 13_scan_vertical.py all                           # 全量，产出 work/ocr_vertical.json
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pymupdf
from PIL import Image
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = BASE / "work"
OCR_BIN = Path(__file__).resolve().parent / "ocr_vision"   # OCR 二进制随 skill 走
ZOOM = 4.0

ASCII_WORD = re.compile(r"[A-Za-z]{2,}")
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def looks_english(t):
    t = t.strip()
    if len(t) < 4 or re.search(r"[^\x20-\x7E]", t):
        return False
    letters = sum(c.isalpha() for c in t)
    return letters >= 4 and letters / len(t) >= 0.7 and bool(ASCII_WORD.search(t))


def rot_cw(path_in, path_out):
    """顺时针旋转 90° 并保存。"""
    im = Image.open(path_in)
    arr = np.rot90(np.asarray(im), k=-1)
    Image.fromarray(np.ascontiguousarray(arr)).save(path_out)


def run_ocr(paths):
    exe = Path(str(OCR_BIN))
    if not exe.exists():
        exe = Path(str(OCR_BIN) + ".bin")
    out = subprocess.run([str(exe), *[str(p) for p in paths]],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["probe", "all"])
    ap.add_argument("--pdf", default="input.pdf")
    ap.add_argument("--pages", default="")
    args = ap.parse_args()

    doc = pymupdf.open(BASE / args.pdf)
    tmp = WORK / "rot_tmp"
    tmp.mkdir(parents=True, exist_ok=True)

    pages = ([int(p) for p in args.pages.split(",") if p.strip()]
             if args.pages else list(range(1, doc.page_count + 1)))
    pngs = []
    for p in pages:
        raw = tmp / f"p{p:02d}_raw.png"
        rot = tmp / f"p{p:02d}.png"
        doc[p - 1].get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), alpha=False).save(raw)
        rot_cw(raw, rot)
        pngs.append(rot)

    blocks = run_ocr(pngs)
    found = []
    for b in blocks:
        if not looks_english(b["text"]):
            continue
        pno = int(re.search(r"p(\d+)", b["file"]).group(1))
        r = doc[pno - 1].rect
        u0, v0 = b["x"], b["y"]
        u1, v1 = b["x"] + b["w"], b["y"] + b["h"]
        # 旋转图坐标 → 原图坐标
        ou0, ov0, ou1, ov1 = v0, 1 - u1, v1, 1 - u0
        w_pt = (ou1 - ou0) * r.width
        h_pt = (ov1 - ov0) * r.height
        # 只留「原图里确实是竖排」的块：窄而高。旋转后横排的正常文字会变成
        # 宽而扁，用这个判据一刀切掉，避免把整页正文重复检出。
        if not (w_pt < 34 and h_pt > 18 and h_pt > 1.4 * w_pt):
            continue
        found.append({
            "page": pno, "text": b["text"], "conf": round(float(b.get("conf", 0)), 2),
            "bbox": [round(ou0 * r.width, 1), round(ov0 * r.height, 1),
                     round(ou1 * r.width, 1), round(ov1 * r.height, 1)],
            "h": round(h_pt, 1), "w": round(w_pt, 1),
        })

    print(f"旋转 90° 后检出英文块: {len(found)}（{len(pages)} 页）")
    if args.mode == "probe":
        for f in sorted(found, key=lambda f: (f["page"], f["bbox"][1]))[:40]:
            print(f"  p{f['page']:<3} {f['w']:>6.1f}x{f['h']:<5.1f} conf={f['conf']:<4} "
                  f"{f['text'][:64]!r}")
        return

    # 与主扫描结果比对：标记新增
    scope = json.loads((WORK / "ocr_scope4.json").read_text(encoding="utf-8"))
    src = {}
    for x in scope["in_image"]:
        src.setdefault(x["page"], []).append(x)
    # 已进入计划的原文本
    plan = json.loads((WORK / "l2_queue" / "overlay_plan_l2.json").read_text(encoding="utf-8"))
    plan_txt = {(it["page"], re.sub(r"\s+", "", it.get("src", "")).lower())
                for it in plan["items"]}

    new = []
    for f in found:
        nt = re.sub(r"\s+", "", f["text"]).lower()
        dup = False
        for x in src.get(f["page"], []):
            nx = re.sub(r"\s+", "", x["text"]).lower()
            if len(nx) >= 5 and (nx in nt or nt in nx):
                dup = True
                break
        if not dup and (f["page"], nt) not in plan_txt:
            new.append(f)

    out = WORK / "ocr_vertical.json"
    out.write_text(json.dumps(found, ensure_ascii=False, indent=1), encoding="utf-8")
    (WORK / "ocr_vertical_new.json").write_text(
        json.dumps(new, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"其中主扫描未见过的（新增候选）: {len(new)} 处，涉及 "
          f"{len({f['page'] for f in new})} 页")
    print(f"-> {out.relative_to(BASE)} / {Path('work/ocr_vertical_new.json')}")
    for f in sorted(new, key=lambda f: -f["h"])[:30]:
        print(f"  p{f['page']:<3} {f['w']:>6.1f}x{f['h']:<5.1f} conf={f['conf']:<4} {f['text'][:60]!r}")


if __name__ == "__main__":
    main()
