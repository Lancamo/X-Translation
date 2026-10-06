#!/usr/bin/env python3
"""66_backtranslate.py — T3 回译核验：把译文再翻回源语言，与原文比对抓语义漂移。

为什么值这一步：残迹扫描只能发现「还有外文」，术语检查只能发现「用词不统一」，
数字检查只能发现「数变了」。**「句子整体翻错意思」这一类问题，上面三个都抓不到。**
回译是唯一能碰到它的机械手段：把成品的中文重新译成英文，与原文并排看——
译反了、漏了从句、主谓颠倒了，两侧内容词会对不上。

成本等于再跑一遍翻译，所以**只对 T2 抽出的那几页做**，不做全量。

两段式（和 skill 里其他翻译步骤同构）：
  1) --emit   导出待回译工作单 work/verify/backtrans_queue.txt
  2)          （用模型把每行译文回译成源语言，写进 backtrans_done.txt，格式 gid<TAB>回译）
  3) --check  逐条比对原文与回译的内容词，输出漂移清单

判定：内容词（去停用词、去数字）的 Jaccard 重合率。
      ≥0.5 视为一致；0.25~0.5 需人工看；<0.25 高度可疑。
      数字单独看——回译里数字对不上原文，说明译文里的数字本身就错了。

用法：
  python 66_backtranslate.py --emit
  python 66_backtranslate.py --check
"""
import argparse
import json
import re
from pathlib import Path

import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
OUTDIR = BASE / "work" / "verify"
QUEUE = OUTDIR / "backtrans_queue.txt"
DONE = OUTDIR / "backtrans_done.txt"

WORD = re.compile(r"[A-Za-z]{2,}")
STOP = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "were", "has",
        "have", "had", "not", "but", "all", "you", "our", "its", "out", "per", "see",
        "can", "will", "would", "could", "should", "been", "into", "than", "then",
        "more", "most", "less", "least", "also", "just", "only", "over", "under",
        "about", "which", "while", "when", "where", "what", "who", "how", "why",
        "their", "there", "these", "those", "they", "them", "his", "her", "hers",
        "itself", "very", "much", "many", "some", "any", "each", "other", "such"}
# 否定词极性。两个坑都踩过：
#   ① 报告里用的是**弯引号**，aren’t 不是 aren't，只认直引号会全部漏掉
#   ② non-normal / not-normal 是同义的两种构词，不认 non 会误判成「译反」
NEG = re.compile(r"\b(?:not|non|no|never|none|nothing|neither|nor|without|cannot)\b|"
                 r"[A-Za-z]+n['\u2019]t\b|不|没|无|未|非|别|勿|莫", re.IGNORECASE)
# 回译里数字常被写成英文数词（"~3 years" 回译成 "three years"），
# 不映射就会误报「数字丢失」。数字归一化与 64_audit_text.py 保持一致，
# 刻意重复十几行而不做跨文件 import——技能里的脚本会被改名，
# 兄弟模块 import 一改名就断，独立可跑比去掉重复更重要。
NUMWORD = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
           "seven": "7", "eight": "8", "nine": "9", "ten": "10", "eleven": "11",
           "twelve": "12", "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50",
           "sixty": "60", "seventy": "70", "eighty": "80", "ninety": "90",
           "hundred": "100", "thousand": "1000", "million": "1000000"}
NUMLIKE = re.compile(r"(?<![A-Za-z0-9])[0-9][0-9oOlI,.\u00a0]{0,18}")
LOOKALIKE = str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1"})


def nums(s):
    out = set()
    for m in NUMLIKE.finditer(s):
        tok = re.sub(r"(?<=[0-9oOlI])s(?![A-Za-z])", "", m.group(0)).translate(LOOKALIKE)
        for run in re.split(r"[^\d]+", tok):
            if run:
                out.add(run.lstrip("0") or "0")
    for w in WORD.findall(s.lower()):
        if w in NUMWORD:
            out.add(NUMWORD[w])
    return out


def num_missing(src, back):
    """源侧数字在回译里找不到的。容忍两位/四位年份互换（9.4.26 <-> 2026.9.4）。"""
    miss = []
    for t in nums(src):
        if t in nums(back) or (len(t) == 2 and "20" + t in nums(back)) \
                or (len(t) == 4 and t[2:] in nums(back)):
            continue
        miss.append(t)
    return sorted(set(miss))


def rel(p):
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def content_words(s):
    return {w.lower() for w in WORD.findall(s) if w.lower() not in STOP}


def read_pairs():
    p = OUTDIR / "pairs.json"
    if not p.exists():
        raise SystemExit(f"缺 {rel(p)}；先跑 64_audit_text.py")
    return json.loads(p.read_text(encoding="utf-8"))


def read_rows(path):
    rows = {}
    if not Path(path).exists():
        return rows
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "\t" not in line:
            continue
        g, v = line.split("\t", 1)
        rows[g.strip()] = v.strip()
    return rows


def emit(args):
    plan_path = OUTDIR / "sample_plan.json"
    pages = set()
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for v in plan["plan"].values():
            pages |= set(v)
    pairs = read_pairs()
    sel = []
    for i, p in enumerate(pairs):
        if p.get("status") not in ("translated", "same"):
            continue
        if pages and (p["page"] + 1) not in pages:
            continue
        if len(p.get("tgt", "")) < args.min_len:
            continue
        sel.append((i, p))
    if len(sel) > args.max:
        sel = sel[:args.max]

    OUTDIR.mkdir(parents=True, exist_ok=True)
    lines = [f"# 回译工作单  —  共 {len(sel)} 条（范围：{'抽样页 ' + str(sorted(pages)) if pages else '全篇'}）",
             "# 格式：gid<TAB>译文回译成源语言（英文）。力求还原原意，不要逐字直译，不要补全原文没有的信息。",
             "# 品牌名/代码/型号原样保留。"]
    for i, p in sel:
        tgt = " ".join(p["tgt"].split())
        lines.append(f"b{i:04d}\t{tgt}")
    QUEUE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"回译工作单 : {len(sel)} 条 -> {rel(QUEUE)}")
    print(f"下一步     : 把每行译文回译成英文，写进 {rel(DONE)}（格式 gid<TAB>回译）")
    print(f"然后       : python 66_backtranslate.py --check")


def check(args):
    pairs = read_pairs()
    que = read_rows(QUEUE)
    don = read_rows(DONE)
    if not que:
        raise SystemExit(f"没有工作单，先跑 --emit（{rel(QUEUE)}）")
    if not don:
        raise SystemExit(f"没有回译结果，把回译写进 {rel(DONE)} 再跑 --check")

    rows, miss = [], []
    for gid, tgt in que.items():
        if gid not in don:
            miss.append(gid)
            continue
        i = int(gid[1:])
        p = pairs[i]
        src, back = p["src"], don[gid]
        sw, bw = content_words(src), content_words(back)
        if not sw or not bw:
            continue
        jac = len(sw & bw) / len(sw | bw)

        # 硬信号：可靠、可复核、不依赖措辞。措辞相近度只用来排序。
        hard, hint = [], []
        nm = num_missing(src, back)
        if nm:
            hard.append(f"数字不符 {nm[:5]}")
        ratio = len(back) / max(1, len(src))
        if not (0.45 <= ratio <= 2.2):
            hard.append(f"长度比失衡 {ratio:.2f}")
        # 否定极性只作提示、不作判据：构词法太容易骗到它
        # （already/unmatched 不含否定、but non-normal 又确实是否定）。
        if bool(NEG.search(src)) != bool(NEG.search(back)):
            hint.append("否定词两侧不一致，值得看一眼")

        if hard or jac < 0.15:
            verdict = "flag"
        elif jac < 0.35 or hint:
            verdict = "watch"
        else:
            verdict = "ok"
        rows.append({"gid": gid, "page": p["page"] + 1, "layer": p["layer"],
                     "overlap": round(jac, 2), "verdict": verdict,
                     "hard": hard, "hint": hint,
                     "src": src[:110], "tgt": tgt[:110], "back": back[:110]})
    rows.sort(key=lambda r: (r["verdict"] != "flag", r["overlap"]))
    (OUTDIR / "backtrans.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                           encoding="utf-8")

    n = {v: sum(1 for r in rows if r["verdict"] == v) for v in ("ok", "watch", "flag")}
    print(f"回译核验 : {len(rows)} 条   ok {n['ok']} / watch {n['watch']} / flag {n['flag']}")
    print("  口径：回译是**排序工具**不是判定工具。措辞相近度只能把最不像的排到前面，")
    print("        硬判据只有两条——数字不符、长度比失衡；否定词不一致只作提示，")
    print("        因为 already/unmatched 这类构词法太容易被它误判。")
    if miss:
        print(f"  [warn] {len(miss)} 条没回译：{miss[:6]}")
    for r in rows:
        if r["verdict"] == "ok":
            continue
        print(f"\n  [{r['verdict']}] p{r['page']} 措辞重合 {r['overlap']:.0%}"
              + ("  ⚠ " + "；".join(r["hard"]) if r["hard"] else "")
              + ("  · " + "；".join(r["hint"]) if r["hint"] else ""))
        print(f"    源　{r['src']}")
        print(f"    译　{r['tgt']}")
        print(f"    回译{r['back']}")
    print(f"\n输出 -> {rel(OUTDIR)}/backtrans.json")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--emit", action="store_true", help="导出待回译工作单")
    g.add_argument("--check", action="store_true", help="比对回译结果，出漂移清单")
    ap.add_argument("--min-len", type=int, default=24, help="译文多短就不值得回译")
    ap.add_argument("--max", type=int, default=120, help="工作单最多条数（控制模型工作量）")
    args = ap.parse_args()
    emit(args) if args.emit else check(args)


if __name__ == "__main__":
    main()
