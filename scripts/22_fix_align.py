#!/usr/bin/env python3
"""
修复单元对齐（align）：区分左 / 中 / 右，保证译文不改变原版面。

⚠️ 这是一步**必做**的收尾，不是可选优化
--------------------------------------
20_extract_units.py 给出的 `align` 只是粗判，回填前必须经这里精判：

1. 多行单元：抽取时用「行坐标极差 < 1.5pt」判对齐。末行尾随空格会把 bbox 撑宽、
   把中心推偏约 1.7pt，于是恰好超过 1.5 → 居中被误判成左。
   实测某项目 p82u01(1.75) / p77u01(1.86) / p84u01(1.84) / p27u10(1.55) 全部漏判。
2. 单行单元：抽取时一律写死 left。封面日期、居中标题、图表面板标题全被当成左对齐。
   译文比原文短，左边缘原地不动 → 视觉中心整体左移。

判据
----
多行：对每行取 x0 / x1 / 中心 cx 三个轴，**取极差最小的那条轴**作为对齐轴
      （左对齐必然 x0 最紧、右对齐 x1 最紧、居中 cx 最紧），并要求该极差 <= tol。
      不用固定优先级——2 行单元里 x1 极差常恰好过关，会把居中误判成右对齐。
单行：找同页**水平重叠**且纵向最近的**多行单元**作锚点，沿用它的对齐轴；
      无锚点时退回页面中心 / 右边界判据。

纯数字单行（页码）跳过：框就是它自身 bbox，左/右对齐渲染位置完全一致，改了没有意义。

为什么单独一个脚本、而不是改抽取脚本
------------------------------------
抽取产出的 `units.json` 被 `translations.json` 用同样的 id 索引。重新抽取会打乱 id，
译文全部错位。所以抽取保持不动，对齐单独修 —— 这也符合"可增量重跑"。

用法
----
  python3 22_fix_align.py --report                 # 只列出将要变更的单元
  python3 22_fix_align.py --apply                  # 写回 units.json（先备份）
  python3 22_fix_align.py --apply --log work/align_changes.json

源 PDF 用于取页面宽度（判断"页面居中/靠右"）。默认自动在项目根目录找唯一的 *.pdf，
也可 `--source` 指定；实在没有就跳过该兜底并在日志里说明。
"""
import argparse
import collections
import json
import re
import shutil
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
TOL_ML_MIN = 1.5     # 多行：各轴极差下限
TOL_ML_RATIO = 0.15  # 多行：极差随字号缩放
DECISIVE = 0.6       # 多行：最紧轴必须 <= 次紧轴的该比例，才认定有效
TOL_SL = 0.15        # 单行：容差 = max(TOL_SL_MIN, TOL_SL * 字号)
TOL_SL_MIN = 2.0
NUMERIC = re.compile(r"^[\s\d.,:%–—/-]+$")   # 纯数字/符号（页码）→ 跳过


def robust_range(vals):
    """极差；>=3 个点时允许剔掉一个最偏点（末行尾随空格）。"""
    v = sorted(vals)
    if len(v) < 2:
        return 0.0
    if len(v) >= 3:
        return min(v[-1] - v[0], v[-1] - v[1], v[-2] - v[0])
    return v[-1] - v[0]


def multi_align(lines, size):
    """左边界已对齐 → left（左对齐是常态，默认）；否则在 right/center 里取决定性胜者。

    容差随字号缩放：6pt 的窄栏里，行宽差异天然只有 1~2pt，
    用固定 2pt 会把「居中」和「左对齐」混为一谈。
    """
    tol = max(TOL_ML_MIN, TOL_ML_RATIO * size)
    xs0 = [l[0] for l in lines]
    xs1 = [l[2] for l in lines]
    cx = [(l[0] + l[2]) / 2 for l in lines]
    sp = {"left": robust_range(xs0),
          "right": robust_range(xs1),
          "center": robust_range(cx)}
    if sp["left"] <= tol:                   # 左边界整齐 → 左对齐
        return "left"
    order = sorted(sp.items(), key=lambda kv: kv[1])
    (best, bs), (_, second) = order[0], order[1]
    if bs > tol:                            # 没有一条轴够整齐 → 保守
        return "left"
    if second > 0 and bs > DECISIVE * second:
        return "left"
    return best


def axis_of(u):
    """返回单元的对齐轴：('left', x0) / ('right', x1) / ('center', cx)。"""
    ls = u["lines"]
    if u["align"] == "left":
        return "left", min(l[0] for l in ls)
    if u["align"] == "right":
        return "right", max(l[2] for l in ls)
    cx = sorted((l[0] + l[2]) / 2 for l in ls)
    return "center", cx[len(cx) // 2]


def x_overlap_ratio(a, b):
    lo = max(a[0], b[0])
    hi = min(a[2], b[2])
    if hi <= lo:
        return 0.0
    return (hi - lo) / max(1e-6, min(a[2] - a[0], b[2] - b[0]))


def vgap(a, b):
    if a[3] <= b[1]:
        return b[1] - a[3]
    if b[3] <= a[1]:
        return a[1] - b[3]
    return 0.0


def single_align(u, anchors, page_units, page_w):
    """单行单元：锚点 → 同行中点 → 同行继承 → 页面兜底 → 默认 left。"""
    ur = u["bbox"]
    ucx = (ur[0] + ur[2]) / 2
    tol = max(TOL_SL_MIN, TOL_SL * u["size"])

    def hit(kind, val):
        return (abs(ur[0] - val) <= tol if kind == "left" else
                abs(ur[2] - val) <= tol if kind == "right" else
                abs(ucx - val) <= tol)

    # 1) 锚点：最近的多行单元（沿用它的对齐轴）。要遍历全部候选，
    #    最近的锚点不匹配不代表其它锚点也不匹配（p88 的两个大标题即如此）。
    cands = [v for v in anchors if x_overlap_ratio(ur, v["bbox"]) >= 0.5]
    for v in sorted(cands, key=lambda v: vgap(ur, v["bbox"])):
        kind, val = axis_of(v)
        if hit(kind, val):
            return kind, f"anchor {v['id']}({kind}@{val:.1f})"

    # 2) 同行中点：左右最近邻夹出的格子里居中（p88 的 Robotics/Autonomy）
    def same_row(v):
        return min(ur[3], v["bbox"][3]) - max(ur[1], v["bbox"][1]) > 0
    left = [v for v in page_units if v["id"] != u["id"] and same_row(v)
            and v["bbox"][2] <= ur[0] + 1]
    right = [v for v in page_units if v["id"] != u["id"] and same_row(v)
             and v["bbox"][0] >= ur[2] - 1]
    if left and right:
        cell = (max(v["bbox"][2] for v in left), min(v["bbox"][0] for v in right))
        if cell[1] > cell[0] and abs(ucx - (cell[0] + cell[1]) / 2) <= tol:
            return "center", f"row-mid@{ (cell[0]+cell[1])/2:.1f}"

    # 3) 同行继承：同一行的兄弟已判定居中且字号一致（p88 的 AI x Bio）
    for v in page_units:
        if v["id"] == u["id"] or not same_row(v):
            continue
        if abs(v["size"] - u["size"]) < 0.5 and v.get("_new", v["align"]) == "center":
            return "center", f"row-inherit {v['id']}"

    # 4) 页面兜底：仅当本页没有多行单元（封面这类整页设计稿）才用
    if not anchors and page_w:
        if abs(ucx - page_w / 2) <= tol and ur[0] > tol:
            return "center", f"page-center@{page_w/2:.1f}"
        if ur[2] >= page_w - 12:
            return "right", f"page-right@{page_w:.1f}"
    return "left", "default"


def find_source(base):
    """项目根目录下唯一的 *.pdf 就是源文件（output/ 之类子目录不参与）。"""
    cands = sorted(p for p in base.glob("*.pdf") if p.is_file())
    return cands[0] if len(cands) == 1 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--units", default=str(BASE / "work" / "units.json"))
    ap.add_argument("--source", default="", help="源 PDF（默认自动找根目录唯一的 *.pdf）")
    ap.add_argument("--log", default="", help="把变更清单写到这个 json")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    units = json.load(open(args.units, encoding="utf-8"))
    src = Path(args.source) if args.source else find_source(BASE)
    if src and Path(src).exists():
        doc = pymupdf.open(str(src))
        print(f"源 PDF: {src}")
    else:
        doc = None
        print(f"[warn] 未找到源 PDF（{src}）：跳过「页面居中/靠右」兜底，"
              f"封面这类无多行单元的页面可能漏判")

    by_page = collections.defaultdict(list)
    for u in units:
        by_page[u["page"]].append(u)

    changes = []
    for pno, us in sorted(by_page.items()):
        page_w = doc[pno].rect.width if doc and pno < doc.page_count else None
        anchors = []
        for u in us:
            if u["nlines"] >= 2:
                a = multi_align(u["lines"], u["size"])
                if a != u["align"]:
                    changes.append({"id": u["id"], "page": pno, "old": u["align"],
                                    "new": a, "size": u["size"], "nlines": u["nlines"],
                                    "how": "multi", "text": u["text"][:60],
                                    "bbox": u["bbox"]})
                    u["_new"] = a
                anchors.append(dict(u, align=u.get("_new", u["align"])))
        for u in us:
            if u["nlines"] > 1:
                continue
            if NUMERIC.match(u["text"]):
                continue                    # 页码：改与不改渲染一致
            a, how = single_align(u, anchors, us, page_w)
            if a != u["align"]:
                changes.append({"id": u["id"], "page": pno, "old": u["align"],
                                "new": a, "size": u["size"], "nlines": 1,
                                "how": how, "text": u["text"][:60],
                                "bbox": u["bbox"]})
                u["_new"] = a

    cnt = collections.Counter(c["old"] + " -> " + c["new"] for c in changes)
    print(f"拟变更 {len(changes)} 条 / 共 {len(units)} 单元")
    for k, v in sorted(cnt.items(), key=lambda kv: -kv[1]):
        print(f"   {k:16s} {v}")
    print()
    for c in sorted(changes, key=lambda c: (c["page"], c["id"])):
        print(f"  p{c['page']:02d} {c['id']} {c['old']:6}->{c['new']:6} sz={c['size']:4.1f} "
              f"n={c['nlines']} [{c['how']}] | {c['text']}")

    if args.log:
        payload = []
        for c in sorted(changes, key=lambda c: (c["page"], c["id"])):
            u = next(x for x in units if x["id"] == c["id"])
            xs0 = [l[0] for l in u["lines"]]
            xs1 = [l[2] for l in u["lines"]]
            cx = [(l[0] + l[2]) / 2 for l in u["lines"]]
            payload.append(dict(c, spread={"x0": round(max(xs0) - min(xs0), 2),
                                           "x1": round(max(xs1) - min(xs1), 2),
                                           "cx": round(max(cx) - min(cx), 2)}))
        Path(args.log).parent.mkdir(parents=True, exist_ok=True)
        json.dump(payload, open(args.log, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"变更清单 -> {args.log}")

    if args.apply:
        bak = Path(args.units).with_suffix(".json.bak")
        shutil.copy(args.units, bak)
        for u in units:
            if "_new" in u:
                u["align"] = u.pop("_new")
        json.dump(units, open(args.units, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        print(f"\n已写回 {args.units}（备份 {bak.name}）")


if __name__ == "__main__":
    main()
