#!/usr/bin/env python3
"""30_build_l2_queue.py — 把「图内文字」整理成可翻译的队列，并分批导出工作单。

输入：work/ocr_scope4.json（4x OCR + 与文字层比对后的图内文字）
输出：
  work/l2_queue/index.json     全部待覆盖实例（含 bbox），供后续组装 overlay plan
  work/l2_queue/batch_NN.txt   去重后的批次工作单（`序号<TAB>元信息<TAB>原文`）
  work/l2_queue/skipped.json   被规则排除的条目及原因（装饰/数字/已有译文）

排除规则（按优先级）：
  1. skip_numeric          纯数字/符号（坐标轴刻度、年份、百分比）
  2. decoration            跨页高频的品牌装饰字（AI6ZGROWTH 一类，逐页重复出现）
  3. already_zh            已经是中文（OCR 偶发识别到中文层残留）

用法：
  python 30_build_l2_queue.py --batch-size 200 --min-h 5
"""
import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SCOPE = BASE / "work" / "ocr_scope4.json"
OUTDIR = BASE / "work" / "l2_queue"

# 品牌装饰字：逐页重复铺排的风格化 logo 字样，属图形元素，不翻译。
# ⚠️ 每个项目的 logo 字样不同，这条正则需要按项目实测值改（打包进 skill 时会被
#    改写成可用 XTRANS_DECOR 环境变量覆盖的形式）。本项目实测值就是下一行正则。
DECORATION = re.compile(os.environ.get("XTRANS_DECOR", r"^AI\s*[6OG&]\s*Z?\s*GROWTH$"), re.IGNORECASE)
CJK = re.compile(r"[\u4e00-\u9fff]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--min-h", type=float, default=5.0,
                    help="块高下限（pt）。低于此值视为微型字，OCR 不可靠且中文更不可读，跳过")
    ap.add_argument("--scope", default=str(SCOPE))
    args = ap.parse_args()

    data = json.loads(Path(args.scope).read_text(encoding="utf-8"))
    items = data["in_image"]

    # 先按文本聚合，判断哪些是跨页高频装饰
    agg = defaultdict(lambda: {"n": 0, "pages": set()})
    for x in items:
        t = x["text"].strip()
        agg[t]["n"] += 1
        agg[t]["pages"].add(x["page"])

    keep, skipped = [], []
    for x in items:
        t = x["text"].strip()
        if not t:
            continue
        reason = None
        if x["cls"] == "skip_numeric":
            reason = "skip_numeric"
        elif DECORATION.match(t):
            reason = "decoration"
        elif (x["bbox"][3] - x["bbox"][1]) < args.min_h:
            reason = "too_small"
        elif CJK.search(t) and not re.search(r"[A-Za-z]{3,}", t):
            reason = "already_zh"
        if reason:
            skipped.append({"page": x["page"], "bbox": x["bbox"], "text": t,
                            "cls": x["cls"], "reason": reason})
        else:
            keep.append(x)

    # 去重 -> 待译文本
    uniq = {}
    for x in keep:
        t = x["text"].strip()
        uniq.setdefault(t, {"n": 0, "pages": [], "h": [], "cls": x["cls"]})
        u = uniq[t]
        u["n"] += 1
        u["pages"].append(x["page"])
        u["h"].append(round(x["bbox"][3] - x["bbox"][1], 1))

    texts = sorted(uniq.items(), key=lambda kv: (-kv[1]["n"], kv[0]))

    OUTDIR.mkdir(parents=True, exist_ok=True)
    index = {
        "source_pdf": "output/product.pdf",
        "stats": {
            "in_image_blocks": len(items),
            "skipped": len(skipped),
            "keep_blocks": len(keep),
            "unique_texts": len(texts),
            "unique_chars": sum(len(t) for t, _ in texts),
        },
        "skip_reasons": dict(Counter(s["reason"] for s in skipped)),
        "texts": {t: {"n": v["n"], "pages": sorted(set(v["pages"])), "cls": v["cls"],
                      "h_avg": round(sum(v["h"]) / len(v["h"]), 1)} for t, v in texts},
    }
    (OUTDIR / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (OUTDIR / "skipped.json").write_text(json.dumps(skipped, ensure_ascii=False, indent=1),
                                         encoding="utf-8")

    # 分批工作单
    bs = args.batch_size
    n_batch = (len(texts) + bs - 1) // bs
    for b in range(n_batch):
        chunk = texts[b * bs:(b + 1) * bs]
        lines = [f"# batch {b+1}/{n_batch}  ({len(chunk)} 条)\n"]
        for i, (t, v) in enumerate(chunk):
            gid = b * bs + i + 1
            pg = ",".join(str(p) for p in sorted(set(v["pages"]))[:5])
            lines.append(f"{gid:04d}\t[{v['cls'][:4]} x{v['n']} p{pg} h{v['h'][0]:.0f}]\t{t}\n")
        (OUTDIR / f"batch_{b+1:02d}.txt").write_text("".join(lines), encoding="utf-8")

    s = index["stats"]
    print("=== L2 队列 ===")
    print(f"图内文字块      : {s['in_image_blocks']}")
    print(f"  排除          : {s['skipped']}  {index['skip_reasons']}")
    print(f"  待处理        : {s['keep_blocks']}")
    print(f"去重后待译      : {s['unique_texts']} 条, {s['unique_chars']} 字符")
    print(f"批次            : {n_batch} 个 (每批 {bs})")
    print(f"输出            : {OUTDIR}")


if __name__ == "__main__":
    main()
