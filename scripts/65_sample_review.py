#!/usr/bin/env python3
"""65_sample_review.py — T2 分层抽检：自动选页 + 上下对照图 + 人判清单。

为什么必须分层、且必须随机：均匀抽 10% 会踩中同一个坑——设计稿的问题集中在
特定页型上（封面漏翻、章节首页字号溢出、图表页微标签糊掉、密集图例页碰撞）。
按页型分层后每层都保证有样本，再在层内随机，才不会「抽了 10 页全是正文」。

核心判断：**哪几页抽检不能靠人拍脑袋**。让人指定页码，结果永远是「挑自己
有信心的那几页」。这里按页型自动分层、层内固定种子随机，保证可复现。

三件事：
  ① 按页型分层抽样（封面必抽；每层至少 1 页）
  ② 生成源↔成品上下对照图（一页一张，供人肉眼看）
  ③ 长句抽样（译文 ≥ 阈值字数）——长句是漏译从句 / 主谓颠倒的高发区，
     短标签反而很少出错

用法：
  python 65_sample_review.py --pdf output/xxx_zh-CN.pdf            # 默认抽 10%
  python 65_sample_review.py --pdf output/xxx.pdf --ratio 0.15 --seed 7
输出：work/verify/sample_plan.json + REVIEW.md + review/cmp_p*.png
"""
import argparse
import json
import math
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = BASE / "work"
OUTDIR = WORK / "verify"
IMGDIR = OUTDIR / "review"
CJK = re.compile(r"[\u3400-\u9fff]")
# 显式否定词（源语言=英文）。命中只是"往人工队列里倾斜"，不是自动 FAIL——
# 自动检测召回不足（弯引号、同义构词），误判难免，升级为 Critical 属项目约定。
NEG_RE = re.compile(r"\b(not|no|never|none|neither|nor|cannot|without|except|"
                    r"lack|fail(?:ed|s)? to)\b", re.I)
WORD_RE = re.compile(r"[A-Za-z0-9]+")


def rel(p):
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def page_features(base, doc):
    """每页的形态特征。全部来自已有产物，不重新跑 OCR。"""
    n = doc.page_count
    feat = [{"page": i + 1, "chars": 0, "imgs": 0, "img_area": 0.0,
             "l2": 0, "small": 0, "title_size": 0.0} for i in range(n)]

    for i in range(n):
        page = doc[i]
        t = page.get_text("text")
        feat[i]["chars"] = len(CJK.findall(t)) + len(t) // 2
        try:
            info = page.get_image_info()
        except Exception:
            info = []
        feat[i]["imgs"] = len(info)
        area = sum(abs(b["bbox"][2] - b["bbox"][0]) * abs(b["bbox"][3] - b["bbox"][1])
                   for b in info)
        feat[i]["img_area"] = round(area / max(1.0, page.rect.width * page.rect.height), 3)

    up = base / "work" / "units.json"
    if up.exists():
        for u in json.loads(up.read_text(encoding="utf-8")):
            if 0 <= u["page"] < n and len(u["text"].strip()) >= 2:
                feat[u["page"]]["title_size"] = max(feat[u["page"]]["title_size"],
                                                    u.get("size") or 0.0)

    sp = base / "work" / "ocr_scope4.json"
    if sp.exists():
        for x in json.loads(sp.read_text(encoding="utf-8"))["in_image"]:
            i = x["page"] - 1
            if not (0 <= i < n):
                continue
            feat[i]["l2"] += 1
            if (x["bbox"][3] - x["bbox"][1]) < 5.0:
                feat[i]["small"] += 1
    return feat


def quant(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(len(s) * p)))] if s else 0


def risk_scores(feat, pairs):
    """每页风险分（SYNTHESIS P0-3：错误非均匀分布，纯随机抽检系统性漏坏页）。

    成分全是规则可算的代理信号，各自按全文档最大值归一后加权：
      数字密度 ×1.0（数字错是 Critical）
      否定词命中 ×2.0（否定反转最危险且相似度指标全盲）
      长度比异常 ×2.0（超项目 p05–p95 的 译/源 长度比）
      图内块与微标签密度 ×0.5（小字最容易糊/碰撞）
    权重是默认值，不是标准——换项目按自身数据校准。风险分只决定
    「往哪倾斜」，不构成任何判定。
    """
    digits, negs, anom, ratios = Counter(), Counter(), Counter(), []
    for p in pairs:
        pg = p.get("page", -1) + 1
        s, t = p.get("src") or "", p.get("tgt") or ""
        digits[pg] += sum(ch.isdigit() for ch in s)
        negs[pg] += len(NEG_RE.findall(s))
        if p.get("status") in ("translated", "same") and len(s) >= 20 and len(t) >= 8:
            ratios.append(len(t) / len(s))
    q_lo, q_hi = (quant(ratios, 0.05), quant(ratios, 0.95)) if ratios else (0.0, 9.9)
    for p in pairs:
        s, t = p.get("src") or "", p.get("tgt") or ""
        if p.get("status") in ("translated", "same") and len(s) >= 20 and len(t) >= 8:
            if not (q_lo <= len(t) / len(s) <= q_hi):
                anom[p.get("page", -1) + 1] += 1
    maxd = max(digits.values(), default=0)
    maxn = max(negs.values(), default=0)
    maxa = max(anom.values(), default=0)
    maxl2 = max((f["l2"] for f in feat), default=0)
    maxsm = max((f["small"] for f in feat), default=0)
    for f in feat:
        pg = f["page"]
        f["risk"] = round(
            1.0 * digits.get(pg, 0) / max(1, maxd)
            + 2.0 * negs.get(pg, 0) / max(1, maxn)
            + 2.0 * anom.get(pg, 0) / max(1, maxa)
            + 0.5 * f["l2"] / max(1, maxl2)
            + 0.5 * f["small"] / max(1, maxsm), 4)
    return feat


def forced_pool(feat, plan, cap_ratio):
    """高风险强制池：按风险分降序取前 N（N = 页数 × cap_ratio），不占随机
    配额、不挤占随机样本（已在随机样本里的页不重复进池）。"""
    cap = math.ceil(len(feat) * cap_ratio)
    sampled = {p for v in plan.values() for p in v}
    cand = sorted((f for f in feat if f["page"] not in sampled),
                  key=lambda f: (-f["risk"], f["page"]))
    return [f["page"] for f in cand[:cap]]


def stratify(feat):
    """按页型归类。

    阈值一律取**本项目自身的分位数**，不写死绝对值——不同项目页数、图片密度、
    正文字号差得很远，写死阈值换个项目就把所有页归成一类（实测过）。
    页型顺序即优先级：封面永远独立成层。
    """
    chars = [f["chars"] for f in feat]
    l2 = [f["l2"] for f in feat]
    tsz = [f["title_size"] for f in feat if f["title_size"] > 0]
    q_ch_low, q_ch_hi = quant(chars, 0.25), quant(chars, 0.60)
    q_l2_lo, q_l2_hi = quant([x for x in l2 if x > 0] or [0], 0.25), quant(l2, 0.75)
    q_title = quant(tsz, 0.90)

    for f in feat:
        if f["page"] == 1:
            f["stratum"] = "cover"
        elif f["title_size"] >= q_title and q_title > 0:
            f["stratum"] = "chapter"
        elif f["l2"] >= q_l2_hi:
            f["stratum"] = "dense"
        elif f["chars"] <= q_ch_low and f["l2"] > 0:
            f["stratum"] = "chart"
        elif f["chars"] >= q_ch_hi and f["l2"] <= q_l2_lo:
            f["stratum"] = "text"
        else:
            f["stratum"] = "other"
    return feat


def sample(feat, ratio, seed, per_stratum_min=1):
    rnd = random.Random(seed)
    groups = defaultdict(list)
    for f in feat:
        groups[f["stratum"]].append(f)
    total = len(feat)
    want = max(1, round(total * ratio))
    plan = {}
    for s, g in groups.items():
        if s == "cover":
            plan[s] = [g[0]["page"]]
        else:
            n = max(per_stratum_min, round(want * len(g) / total))
            n = min(n, len(g))
            plan[s] = sorted(x["page"] for x in rnd.sample(g, n))
    # 超出目标就按层大小等比例裁减（封面的那 1 页不可裁）
    picked = sum(len(v) for v in plan.values())
    while picked > want + 2 and any(len(v) > 1 for s, v in plan.items() if s != "cover"):
        big = max((s for s in plan if s != "cover" and len(plan[s]) > 1),
                  key=lambda s: len(plan[s]), default=None)
        if big is None:
            break
        plan[big].pop()
        picked -= 1
    return plan


def render_pair(src, prd, pno, out_path, zoom=1.3):
    def one(doc):
        pix = doc[pno - 1].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    a, b = one(src), one(prd)
    w = 1080
    bar = 26
    def fit(im):
        k = w / im.width
        return im.resize((w, max(1, int(im.height * k))))
    a, b = fit(a), fit(b)
    canvas = Image.new("RGB", (w, a.height + b.height + bar * 2 + 8), "#FAFAFA")
    d = ImageDraw.Draw(canvas)
    d.rectangle([0, 0, w, bar], fill="#FCEBEB")
    d.text((8, 7), f"源  p{pno}", fill="#501313")
    canvas.paste(a, (0, bar))
    d.rectangle([0, bar + a.height, w, bar + a.height + bar], fill="#EAF3DE")
    d.text((8, bar + a.height + 7), f"成品  p{pno}", fill="#173404")
    canvas.paste(b, (0, bar + a.height + bar))
    canvas.save(out_path)
    return canvas.size


def load_pairs(base):
    p = base / "work" / "verify" / "pairs.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="output/product.pdf")
    ap.add_argument("--src", default="")
    ap.add_argument("--src-pdf", dest="src_pdf", default="", help=argparse.SUPPRESS)
    ap.add_argument("--ratio", type=float, default=0.10, help="抽检比例（按页数）")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--long", type=int, default=10, help="长句抽样条数")
    ap.add_argument("--long-min", type=int, default=60, help="多长算长句（目标语言字符）")
    ap.add_argument("--force-cap", type=float, default=0.20,
                    help="高风险强制池页数上限（占总页数比例，SYNTHESIS P0-3 ≤20%%）")
    ap.add_argument("--min-words", type=int, default=1000,
                    help="抽检样本词数下限（MQM 口径：低于判 Inconclusive）")
    ap.add_argument("--zoom", type=float, default=1.3)
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = BASE / pdf
    sp = Path(args.src or args.src_pdf) if (args.src or args.src_pdf) else BASE / "input.pdf"
    if not sp.is_absolute():
        sp = BASE / sp
    if not sp.exists():
        cands = sorted(p for p in BASE.glob("*.pdf") if p.is_file()
                       and p.resolve() != pdf.resolve())
        sp = cands[0] if cands else sp
    if not sp.exists():
        raise SystemExit("找不到源 PDF；用 --src 指定")

    src, prd = pymupdf.open(sp), pymupdf.open(pdf)
    if src.page_count != prd.page_count:
        print(f"[warn] 源 {src.page_count} 页 / 成品 {prd.page_count} 页，按较小的取")

    feat = stratify(page_features(BASE, prd))
    pairs = load_pairs(BASE)
    feat = risk_scores(feat, pairs)
    plan = sample(feat, args.ratio, args.seed)
    pool = forced_pool(feat, plan, args.force_cap)

    # 样本词数下限（MQM：样本 <1,000 词判 Inconclusive，结论不外推全体）。
    # 中文按字计、拉丁按词计，两者相加。
    sampled_pages = {p for v in plan.values() for p in v} | set(pool)
    n_words = 0
    for i in range(min(src.page_count, prd.page_count)):
        if (i + 1) in sampled_pages:
            t = prd[i].get_text("text")
            n_words += len(WORD_RE.findall(t)) + len(CJK.findall(t))
    inconclusive = n_words < args.min_words

    by_page = {f["page"]: f for f in feat}
    IMGDIR.mkdir(parents=True, exist_ok=True)
    for f in IMGDIR.glob("cmp_p*.png"):
        f.unlink()
    made = []
    for stratum, pages in sorted(plan.items()):
        for pno in pages:
            out = IMGDIR / f"cmp_p{pno:02d}.png"
            try:
                size = render_pair(src, prd, pno, out, args.zoom)
                made.append({"page": pno, "origin": "random", "stratum": stratum,
                             "img": str(rel(out)), "size": size})
            except Exception as e:
                print(f"[warn] p{pno} 渲染失败：{e}")

    # 高风险强制池：额外叠加，不占随机配额
    for pno in pool:
        out = IMGDIR / f"cmp_p{pno:02d}.png"
        if out.exists():
            made.append({"page": pno, "origin": "forced", "stratum": "forced",
                         "img": str(rel(out)), "size": None})
            continue
        try:
            size = render_pair(src, prd, pno, out, args.zoom)
            made.append({"page": pno, "origin": "forced", "stratum": "forced",
                         "img": str(rel(out)), "size": size})
        except Exception as e:
            print(f"[warn] p{pno} 渲染失败：{e}")

    # 长句抽样
    longs = [p for p in pairs
             if p.get("status") in ("translated", "same")
             and len(p.get("tgt", "")) >= args.long_min]
    rnd = random.Random(args.seed)
    long_sel = rnd.sample(longs, min(args.long, len(longs))) if longs else []
    long_sel.sort(key=lambda p: (p["page"], p["bbox"][1]))

    out = {"pdf": str(rel(pdf)), "src": str(rel(sp)),
           "ratio": args.ratio, "seed": args.seed,
           "page_count": prd.page_count,
           "forced_pool_cap": args.force_cap,
           "inconclusive": inconclusive,
           "sampled_words": n_words, "min_words": args.min_words,
           "risk_top10": sorted(({"page": f["page"], "risk": f["risk"],
                                  "stratum": f["stratum"]} for f in feat),
                                key=lambda x: -x["risk"])[:10],
           "strata": {s: len(g) for s, g in
                      sorted(((s, [f for f in feat if f["stratum"] == s])
                              for s in {f["stratum"] for f in feat}))},
           "plan": {s: v for s, v in sorted(plan.items())},
           "forced_pool": pool,
           "images": made,
           "long_sentences": [{"page": p["page"] + 1, "layer": p["layer"],
                               "len": len(p["tgt"]), "src": p["src"][:120],
                               "tgt": p["tgt"][:200]} for p in long_sel]}
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "sample_plan.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    write_review(OUTDIR / "REVIEW.md", out, by_page)

    print(f"页型分层      : " + " / ".join(f"{s} {n}" for s, n in out["strata"].items()))
    print(f"抽样          : {sum(len(v) for v in plan.values())} / {prd.page_count} 页"
          f"（比例 {args.ratio:.0%}，seed {args.seed}）")
    for s, v in sorted(plan.items()):
        print(f"    {s:<8} p{v}")
    print(f"高风险强制池  : {len(pool)} 页（上限 {args.force_cap:.0%}，不占随机配额）"
          f" p{pool}")
    if inconclusive:
        print(f"[Inconclusive] 样本词数 {n_words} < {args.min_words}，"
              f"本次抽检结论不得外推到全体（MQM 口径）")
    else:
        print(f"样本词数      : {n_words}（≥{args.min_words}，结论可外推）")
    print(f"对照图        : {len(made)} 张 -> {rel(IMGDIR)}/")
    print(f"长句抽样      : {len(long_sel)} 条（≥{args.long_min} 字，共 {len(longs)} 条候选）")
    print(f"输出 -> {rel(OUTDIR)}/sample_plan.json + REVIEW.md")


def write_review(path, out, by_page):
    L = ["# 人判清单 · 抽检", ""]
    if out.get("inconclusive"):
        L.append(f"⚠️ **Inconclusive**：样本词数 {out['sampled_words']} < "
                 f"{out['min_words']}，本次抽检结论不得外推到全体（MQM 口径）。")
        L.append("")
    L += [f"抽样 {sum(len(v) for v in out['plan'].values())} 页（随机 {out['ratio']:.0%}，"
          f"seed {out['seed']}）+ 高风险强制池 {len(out['forced_pool'])} 页"
          f"（不占随机配额），页面清单与对照图见 `review/`。", "",
          "## 一、逐页必看", ""]
    for s, pages in sorted(out["plan"].items()):
        L.append(f"### {s}")
        L.append("")
        for p in pages:
            f = by_page.get(p, {})
            L.append(f"- [ ] p{p:>3}  `review/cmp_p{p:02d}.png`  "
                     f"（风险分 {f.get('risk',0):.2f} · 文字层 {f.get('chars',0)} 字 · "
                     f"图内块 {f.get('l2',0)} · "
                     f"图片 {f.get('imgs',0)} · 最大字号 {f.get('title_size',0):.0f}pt）")
        L.append("")
    if out["forced_pool"]:
        L += ["### 高风险强制池（风险分降序，额外叠加）", ""]
        for p in out["forced_pool"]:
            f = by_page.get(p, {})
            L.append(f"- [ ] p{p:>3}  `review/cmp_p{p:02d}.png`  "
                     f"（风险分 {f.get('risk',0):.2f} · {f.get('stratum','')}）")
        L.append("")
    L += ["## 二、每页看什么（按页型）", "",
          "| 页型 | 重点看什么 |", "|---|---|",
          "| cover | 封面标题是否为目标语言；字重会不会比原文笨重 |",
          "| chapter | 章节标题有没有超出原框、字号有没有被引擎压小 |",
          "| text | 段落有没有串行；对齐（居中/靠左/靠右）有没有走样 |",
          "| chart | 图例、坐标轴标签、数据标注是否全译；有没有压字 |",
          "| dense | 微标签有没有糊成一团；相邻标签有没有互相碰撞 |",
          "| forced | 按该页自身构成（数字/否定词/图内块）判断重点 |",
          ""]
    if out["long_sentences"]:
        L += ["## 三、长句抽检（漏译从句 / 主谓颠倒的高发区）", ""]
        for p in out["long_sentences"]:
            L.append(f"- [ ] p{p['page']}  [{p['layer']}]  {p['len']} 字")
            L.append(f"    - 源：{p['src']}")
            L.append(f"    - 译：{p['tgt']}")
        L.append("")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
