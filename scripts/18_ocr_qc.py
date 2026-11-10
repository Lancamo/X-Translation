#!/usr/bin/env python3
"""18_ocr_qc.py — OCR 校对框架：逐块分级 + 双倍率复读校准 + GO/REVIEW/NO-GO 门。

为什么需要：OCR 误识会被**直接写进成品**（补丁管线没有第二道信息源），
而且「源 OCR 读错 → 译文跟着错」这类问题在 L4 验收里表现为假阳性/假阴性，
必须在翻译开始前就把不可靠的块挑出来处置掉。

三层校对：
  1. 逐块分级（ok / review / skip）
     - conf 阈值：< 0.35 skip（OCR 自己都没把握）、< 0.55 review；
     - 乱码启发：>=10 位数字连串（源侧噪声，与 64_ 同口径）、连续同字符、
       无元音长 token、成串问号；
     - 冲突重复读：同页两个框重叠 >=60% 但文本实质不同 → 双双 review
       （同位置两条读数必有一条是错的）。
  2. 复读校准：分层抽 N 页用更高倍率（默认 6x）重渲染重 OCR，
     按 bbox 配对算文本相似度——校准「4x 的读数到底能不能信」。
  3. 出门禁：GO / REVIEW / NO-GO + flags.json 供 30/31 --qc-flags 消费。

处置约定：skip 块在 30 里直接排除（保留原文）；review 块在工作单上标 `!QC`，
翻译时逐条人工裁决（可译可 `=`），不许闭眼照抄 OCR 原文。

用法：
  python 18_ocr_qc.py                       # 分级 + 复读校准 8 页
  python 18_ocr_qc.py --recheck 0           # 只分级，不复读
  python 18_ocr_qc.py --recheck 20          # 大部头加大抽样
"""
import argparse
import difflib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pymupdf

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SRC = Path(os.environ.get("XTRANS_SRC", BASE / "input.pdf"))
SCOPE = BASE / "work" / "ocr_scope4.json"
OUTDIR = BASE / "work" / "ocr_qc"
SCRIPTS = Path(__file__).resolve().parent
OCR_BIN = SCRIPTS / "ocr_vision"

NO_VOWEL = re.compile(r"\b[A-Za-z]{8,}\b")
NOISE_DIGITS = re.compile(r"\d{10,}")
CHAR_REPEAT = re.compile(r"(.)\1{5,}")
CJK = re.compile(r"[\u4e00-\u9fff]")


def norm_text(t):
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", t.lower())


def overlap_min(a, b):
    """交面积 / 较小者面积。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    sa = (a[2] - a[0]) * (a[3] - a[1])
    sb = (b[2] - b[0]) * (b[3] - b[1])
    return inter / max(min(sa, sb), 1e-6)


def garbage_reasons(t):
    """乱码启发。返回原因列表；纯数字/符号块不查（30 本来就会排除）。"""
    rs = []
    if NOISE_DIGITS.search(t):
        rs.append("noise_digits(>=10位数字连串,源侧噪声)")
    if CHAR_REPEAT.search(t):
        rs.append("char_repeat(同字符连写)")
    if t.count("?") >= 2:
        rs.append("junk_marks(成串问号)")
    if not CJK.search(t):
        for tok in NO_VOWEL.findall(t):
            if not re.search(r"[aeiouyAEIOUY]", tok):
                rs.append(f"no_vowel({tok})")
                break
    return rs


def classify_blocks(items, args):
    """逐块分级。verdict: ok < review < skip。"""
    out = []
    for x in items:
        conf = float(x.get("conf", 1.0))
        reasons = []
        if conf < args.conf_skip:
            reasons.append(f"low_conf({conf:.2f})")
        elif conf < args.conf_review:
            reasons.append(f"low_conf({conf:.2f})")
        reasons += garbage_reasons(x["text"])
        if not reasons:
            verdict = "ok"
        else:
            # skip 只留给「OCR 自己承认没把握」和确定性噪声；
            # 其余（低一些的 conf、乱码启发、重复读冲突）都是 review，交人工
            skip = (conf < args.conf_skip
                    or any("noise_digits" in r or "char_repeat" in r for r in reasons))
            verdict = "skip" if skip else "review"
        out.append([x, reasons, verdict])

    # 冲突重复读：同页重叠高但文本不同 → 双双 review
    by_page = defaultdict(list)
    for e in out:
        by_page[e[0]["page"]].append(e)
    conflicts = 0
    for pno, es in by_page.items():
        for i in range(len(es)):
            for j in range(i + 1, len(es)):
                a, b = es[i][0], es[j][0]
                na, nb = norm_text(a["text"]), norm_text(b["text"])
                if not na or not nb or na == nb:
                    continue
                if overlap_min(a["bbox"], b["bbox"]) >= 0.6 \
                        and difflib.SequenceMatcher(None, na, nb).ratio() < 0.85:
                    for e in (es[i], es[j]):
                        if e[2] == "ok":
                            e[2] = "review"
                        e[1].append("conflict_dup(同位异文重复读)")
                    conflicts += 1
    return out, conflicts


def run_ocr(paths):
    exe = OCR_BIN if OCR_BIN.exists() else Path(str(OCR_BIN) + ".bin")
    if not exe.exists():
        sys.exit("ocr_vision 不存在，先: swiftc -O scripts/ocr_vision.swift -o scripts/ocr_vision")
    r = subprocess.run([str(exe)] + [str(p) for p in paths], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"OCR 失败: {r.stderr[:300]}")
    return json.loads(r.stdout)


def recheck(items, args):
    """分层抽页，6x 复读，按 bbox 配对算相似度。返回 (页报告, 需降级的块索引集合)。"""
    doc = pymupdf.open(args.src)
    by_page = defaultdict(list)
    for i, x in enumerate(items):
        by_page[x["page"]].append(i)
    # 按页均置信度升序排，等距抽样——差页好页都要覆盖到
    page_conf = {p: sum(float(x["conf"]) for x in items if x["page"] == p)
                 / max(1, sum(1 for x in items if x["page"] == p)) for p in by_page}
    order = sorted(page_conf, key=page_conf.get)
    step = max(1, len(order) // args.recheck)
    sampled = order[::step][:args.recheck]

    zoom = args.recheck_zoom
    tmpdir = OUTDIR / "recheck"
    tmpdir.mkdir(parents=True, exist_ok=True)
    pages_report, mismatch_idx = [], set()
    sims_all = []

    for pno in sampled:
        pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        png = tmpdir / f"p{pno:03d}_x{int(zoom)}.png"
        pix.save(png)
        R = doc[pno - 1].rect
        cand = []
        for b in run_ocr([png]):
            t = str(b.get("text", "")).strip()
            if not t:
                continue
            cand.append({"text": t, "conf": b["conf"],
                         "bbox": [b["x"] * R.width, (1 - b["y"] - b["h"]) * R.height,
                                  (b["x"] + b["w"]) * R.width, (1 - b["y"]) * R.height]})
        sims, unmatched = [], 0
        for i in by_page[pno]:
            x = items[i]
            best, best_iou = None, 0.0
            for c in cand:
                iou = overlap_min(x["bbox"], c["bbox"])
                if iou > best_iou:
                    best, best_iou = c, iou
            if best is None or best_iou < 0.45:
                unmatched += 1
                continue
            sim = difflib.SequenceMatcher(None, norm_text(x["text"]),
                                          norm_text(best["text"])).ratio()
            sims.append(sim)
            sims_all.append(sim)
            if sim < 0.75:
                mismatch_idx.add(i)
        page_mean = sum(sims) / len(sims) if sims else 0.0
        pages_report.append({"page": pno, "mean_conf": round(page_conf[pno], 3),
                             "blocks": len(by_page[pno]), "matched": len(sims),
                             "unmatched": unmatched,
                             "mean_sim": round(page_mean, 3),
                             "mismatches": sum(1 for s in sims if s < 0.75)})
        flag = " <-- 低可信" if page_mean < 0.8 and sims else ""
        print(f"  p{pno}: 均conf {page_conf[pno]:.2f} / 配对 {len(sims)} / "
              f"均sim {page_mean:.2f} / 复读不一致 {sum(1 for s in sims if s < 0.75)}{flag}")
    doc.close()
    return pages_report, mismatch_idx, (sum(sims_all) / len(sims_all) if sims_all else None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default=str(SCOPE))
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--recheck", type=int, default=8, help="复读校准抽样页数，0=关")
    ap.add_argument("--recheck-zoom", type=float, default=6.0)
    ap.add_argument("--conf-review", type=float, default=0.55)
    ap.add_argument("--conf-skip", type=float, default=0.35)
    args = ap.parse_args()

    data = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    items = data["in_image"]
    if not items:
        sys.exit("scope 里没有图内文字块——扫描型不应出现这种情况，先检查 12 的输入")

    entries, conflicts = classify_blocks(items, args)
    n = len(entries)
    n_skip = sum(1 for e in entries if e[2] == "skip")
    n_review = sum(1 for e in entries if e[2] == "review")
    print(f"=== 逐块分级 ===\n总块 {n} / skip {n_skip} / review {n_review} / ok {n - n_skip - n_review}")
    print(f"冲突重复读对: {conflicts}")

    pages_report, mismatch_idx, agreement = None, set(), None
    if args.recheck > 0:
        print(f"\n=== 复读校准（抽 {min(args.recheck, len({x['page'] for x in items}))} 页 @ {args.recheck_zoom}x）===")
        pages_report, mismatch_idx, agreement = recheck(items, args)
        for i in mismatch_idx:
            if entries[i][2] == "ok":
                entries[i][2] = "review"
            entries[i][1].append("recheck_mismatch(6x复读不一致)")
        n_review = sum(1 for e in entries if e[2] == "review")
        print(f"复读不一致需人工复核: {len(mismatch_idx)} 块")

    # —— 门禁 ——
    skip_ratio, review_ratio = n_skip / n, n_review / n
    ok_chars = sum(len(x["text"]) for x, _, v in entries if v == "ok")
    review_chars = sum(len(x["text"]) for x, _, v in entries if v == "review")
    skip_chars = sum(len(x["text"]) for x, _, v in entries if v == "skip")
    if skip_ratio > 0.25 or (agreement is not None and agreement < 0.60):
        gate = "NO-GO"
    elif skip_ratio <= 0.05 and review_ratio <= 0.15 and (agreement is None or agreement >= 0.85):
        gate = "GO" if agreement is not None else "GO*"
    else:
        gate = "REVIEW"

    # —— 产物 ——
    OUTDIR.mkdir(parents=True, exist_ok=True)
    flags = {
        "gate": gate, "summary": {
            "blocks": n, "skip": n_skip, "review": n_review,
            "ok_chars": ok_chars, "review_chars": review_chars, "skip_chars": skip_chars,
            "skip_ratio": round(skip_ratio, 4), "review_ratio": round(review_ratio, 4),
            "conflict_pairs": conflicts, "recheck_agreement": None if agreement is None else round(agreement, 4),
        },
        "items": [{"page": x["page"], "bbox": x["bbox"], "text": x["text"],
                   "conf": x.get("conf"), "cls": x.get("cls"),
                   "verdict": v, "reasons": rs}
                  for x, rs, v in entries if v != "ok"],
    }
    (OUTDIR / "flags.json").write_text(json.dumps(flags, ensure_ascii=False, indent=1),
                                       encoding="utf-8")

    L = ["# OCR 校对报告（18_ocr_qc）", "",
         f"**门禁: {gate}**" + ("（未做复读校准，GO 带 *）" if gate == "GO*" else ""), "",
         "| 指标 | 值 |", "|---|---|",
         f"| 总块 | {n} |",
         f"| skip（30 自动排除，保留原文） | {n_skip} ({skip_ratio:.1%})，{skip_chars} 字符 |",
         f"| review（工作单标 `!QC`，人工裁决） | {n_review} ({review_ratio:.1%})，{review_chars} 字符 |",
         f"| ok（可直接进入翻译队列） | {n - n_skip - n_review}，{ok_chars} 字符 |",
         f"| 冲突重复读对 | {conflicts} |",
         f"| 复读一致率 | {'—' if agreement is None else f'{agreement:.1%}'} |", ""]
    if pages_report:
        L += ["## 复读校准页", "", "| 页 | 均conf | 配对 | 均sim | 不一致 |", "|---|---|---|---|---|"]
        L += [f"| p{p['page']} | {p['mean_conf']} | {p['matched']}/{p['blocks']}"
              f"（未配对 {p['unmatched']}） | {p['mean_sim']} | {p['mismatches']} |"
              for p in pages_report]
        L += [""]
    L += ["## review/skip 明细（前 60 条，全量见 flags.json）", ""]
    shown = [(x, rs, v) for x, rs, v in entries if v != "ok"][:60]
    for x, rs, v in shown:
        L.append(f"- p{x['page']} [{v}] conf={x.get('conf')} :: {x['text'][:60]!r} :: {'; '.join(rs)}")
    (OUTDIR / "report.md").write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"\n=== 门禁: {gate} ===")
    if gate == "NO-GO":
        print("OCR 质量不足以支撑覆盖式翻译。先试 --deskew-apply / --enhance 重跑 10b，")
        print("仍不行则建议拒单或与用户明确约定「只翻高置信区域」。")
    elif gate == "REVIEW":
        print(f"先人工过一遍 work/ocr_qc/flags.json 的 {n_review + n_skip} 条（重点 review），")
        print("裁决完再继续；或对差页用更高倍率重跑 10b。")
    elif gate == "GO*":
        print("分级通过，但没做复读校准——大部头建议 --recheck 20 补一次再开翻。")
    else:
        print("可以进入 30_build_l2_queue.py（记得带 --qc-flags）。")
    print(f"flags -> {OUTDIR / 'flags.json'}\nreport -> {OUTDIR / 'report.md'}")


if __name__ == "__main__":
    main()
