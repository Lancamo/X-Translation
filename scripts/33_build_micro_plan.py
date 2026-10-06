#!/usr/bin/env python3
"""33_build_micro_plan.py — L2b：<5pt 微标签补译（生成待译清单 / 生成补丁计划）。

背景：L2 主计划用 `--min-h 5.0` 过滤，源图里 1.8–5pt 的图表微标签（图例、轴标题、
供应链小字）整批被丢弃，是成品里最大的一类英文残迹。

为什么不能"原字号直译"：中文笔画密度远高于拉丁字母，同样 2.1pt 下英文尚可辨认、
中文会糊成一团（实测）。所以微标签必须**放大到 ≥3.6pt** 才值得译。放大后可能与
邻行相撞 —— 用「同页相邻块间距」做碰撞护栏，挤不下就跳过该条。

两种模式：
  --mode queue   产出 work/l2_queue/micro_queue.txt（待译，制表符分隔 gid/文本）
  --mode plan    读 work/l2_queue/micro_trans.txt（gid/中文，`=` 表示跳过）
                 产出可交给 40_apply_overlay.py 的补丁计划
"""
import argparse
import json
import re
from pathlib import Path
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
SCOPE = BASE / "work" / "ocr_scope4.json"
MAIN_PLAN = BASE / "work" / "l2_queue" / "overlay_plan_l2.json"
QUEUE = BASE / "work" / "l2_queue"
GID0 = 9500          # 微标签 gid 段，避开 L2 主工作单（0001+）与补扫（9001+）

VOWEL = re.compile(r"[aeiouyAEIOUY]")
CITE = re.compile(r"^\s*sources?\b", re.IGNORECASE)
TICKER = re.compile(r"^[A-Z0-9&.\-]{2,6}$")
DECOR = re.compile(os.environ.get("XTRANS_DECOR", r"^AI\s*[6OG&]\s*Z?\s*GROWTH$"), re.IGNORECASE)


def ov(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    return inter / max(min((a[2] - a[0]) * (a[3] - a[1]),
                           (b[2] - b[0]) * (b[3] - b[1])), 1e-6)


def plausible(t):
    """粗筛 OCR 乱码：要求至少一个含元音、长度 ≥3 的词。"""
    words = re.findall(r"[A-Za-z]{3,}", t)
    return any(VOWEL.search(w) for w in words)


def micro_size(h):
    """微标签目标字号：≥3.6pt 才有可读性，按原高 1.6 倍但不超 4.6pt。"""
    return round(min(4.6, max(3.6, h * 1.6)), 2)


def load_candidates():
    scope = json.loads(SCOPE.read_text(encoding="utf-8"))["in_image"]
    plan = json.loads(MAIN_PLAN.read_text(encoding="utf-8"))
    plan_by = {}
    for it in plan["items"]:
        plan_by.setdefault(it["page"], []).append(it["bbox"])

    page_boxes = {}
    for s in scope:
        page_boxes.setdefault(s["page"], []).append(s["bbox"])

    out = []
    for s in scope:
        if s["cls"] == "skip_numeric":
            continue
        h = s["bbox"][3] - s["bbox"][1]
        if not (1.8 <= h < 5.0):
            continue
        t = s["text"].strip()
        if not re.search(r"[A-Za-z]{3,}", t) or DECOR.match(t):
            continue
        if CITE.match(t) or TICKER.match(t) or not plausible(t):
            continue
        if max((ov(s["bbox"], pb) for pb in plan_by.get(s["page"], [])), default=0.0) > 0.4:
            continue
        out.append(s)
    return out, page_boxes


def collide(s, size, page_boxes, others):
    """放大后的目标框是否撞上同页其它文字块（排除自身与同一批微标签）。"""
    x0, y0, x1, y1 = s["bbox"]
    cy = (y0 + y1) / 2
    half = size * 0.72
    box = [x0 - 1, cy - half, x1 + 1, cy + half]
    for b in page_boxes.get(s["page"], []):
        if b is s["bbox"]:
            continue
        # 只关心垂直方向贴近、水平方向有交叠的邻行
        if b[3] <= box[1] + 0.4 or b[1] >= box[3] - 0.4:
            continue
        if b[2] <= box[0] or b[0] >= box[2]:
            continue
        if b in others:
            continue
        return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["queue", "plan"], default="queue")
    ap.add_argument("--pages", default="")
    ap.add_argument("--grow-w", type=float, default=0.35)
    ap.add_argument("--source", default="output/product.pdf")
    ap.add_argument("--target", default="output/product.pdf")
    ap.add_argument("--out", default="work/l2_queue/overlay_plan_micro.json")
    args = ap.parse_args()

    pages = {int(p) for p in args.pages.split(",") if p.strip()}
    cands, page_boxes = load_candidates()
    if pages:
        cands = [s for s in cands if s["page"] in pages]
    cands.sort(key=lambda s: (s["page"], s["bbox"][1], s["bbox"][0]))

    if args.mode == "queue":
        rows, gid = [], GID0
        for t in sorted({s["text"].strip() for s in cands}, key=str.lower):
            rows.append(f"{gid}\t{t}")
            gid += 1
        (QUEUE / "micro_queue.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")
        print(f"待译唯一文本 {len(rows)} 条 / 出现 {len(cands)} 处 -> "
              f"{(QUEUE / 'micro_queue.txt').relative_to(BASE)}")
        return

    trans = {}
    tf = QUEUE / "micro_trans.txt"
    for line in tf.read_text(encoding="utf-8").splitlines():
        if "\t" not in line or line.lstrip().startswith("#"):
            continue
        g, v = line.split("\t", 1)
        if g.strip().isdigit():
            trans[int(g.strip())] = v.strip()

    text2gid = {}
    for line in (QUEUE / "micro_queue.txt").read_text(encoding="utf-8").splitlines():
        if "\t" in line and line.split("\t", 1)[0].strip().isdigit():
            g, t = line.split("\t", 1)
            text2gid[t.strip()] = int(g.strip())

    items, skipped_collide, skipped_notrans = [], 0, set()
    for s in cands:
        t = s["text"].strip()
        g = text2gid.get(t)
        zh = trans.get(g) if g else None
        if g is None or not zh:
            skipped_notrans.add(t)
            continue
        if zh == "=":
            continue
        size = micro_size(s["bbox"][3] - s["bbox"][1])
        if collide(s, size, page_boxes, []):
            skipped_collide += 1
            continue
        x0, y0, x1, y1 = s["bbox"]
        cy = (y0 + y1) / 2
        half = size * 0.72
        items.append({"page": s["page"],
                      "bbox": [x0, cy - half, x0 + (x1 - x0) * (1 + args.grow_w), cy + half],
                      "zh": zh, "size": size, "size_cap": size,
                      "align": "left", "rotate": 0})

    doc = {"source": args.source, "output": args.target, "zoom": 4,
           "font": "NotoSerifSC-Regular.ttf",
           "defaults": {"pad": 0.6, "size_ratio": 0.78, "size_cap": 1.15,
                        "scale_floor": 0.985},
           "items": items}
    out = BASE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"微标签补丁 {len(items)} 条 / 涉及 {len({i['page'] for i in items})} 页 -> "
          f"{out.relative_to(BASE)}")
    print(f"  因撞邻行跳过 : {skipped_collide}")
    print(f"  无译文跳过   : {len(skipped_notrans)} 种")


if __name__ == "__main__":
    main()
