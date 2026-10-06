#!/usr/bin/env python3
"""64_audit_text.py — T1 文本完整性：术语 / 数字 / 专名 的源↔成品比对。

和「翻自己的处理清单找漏项」不同，这一层全部拿**成品实际渲染出来的文本**说话：
对每个源侧文本块，在成品同一页、同一矩形里取它渲染出来的文字，得到一张
(src, tgt) 对照表，然后在这张表上做三类检查。

检查项与判定强度（诚实标注，不假装能自动判的都能自动判）：

  ① 术语 · 重复源串一致（硬判定，零歧义）
     同一源串在不同位置出现，译文必须一致。不一致 = 红项。
  ② 术语 · 专名保留策略一致（硬判定）
     同一专名在所有出现处应被同等对待（都保留原文 / 都译成中文）。
     有的保留有的译掉 = 策略不一致 = 红项。
     （§二第 6 条要求专有名词保留原文，全部译掉算「一致但需确认」，单列。）
  ③ 术语 · 括号术语证据（不做自动判定，输出人工抽检表）
     译文里的 X（English）是作者亲手写下的术语对照，是判断「同一术语是否
     全篇统一」最直接的证据。从相邻中文里精确切出译名不可靠，所以只抽取证据、
     不自动判，交给人扫一眼。
  ④ 数字 · 单元内守恒（硬判定）
     源块里的数字必须原样出现在同一块的译文里。金额 / 百分比 / 年份被改动
     是后果最严重的一类错，其余检查都抓不到。
  ⑤ 专名 · glossary 冻结核验（硬判定，需先冻结术语表）

先跑本脚本拿到 `glossary_proposed.json` → 人工裁决 → 存成 `work/glossary.json`
并设 `frozen: true` → 再跑本脚本即启用 ⑤ 的逐条硬核验。

用法：
  python 64_audit_text.py --pdf output/xxx_zh-CN.pdf
  python 64_audit_text.py --pdf output/xxx.pdf --sample-terms 40
输出：work/verify/{pairs.json,terms.json,numbers.json,glossary_proposed.json,terms_sample.md}
"""
import argparse
import json
import re
import string
from collections import Counter, defaultdict
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
WORK = BASE / "work"
OUTDIR = WORK / "verify"

CJK = re.compile(r"[\u3400-\u9fff]")
# 纯数字/符号：不参与术语与数字核验（页码、刻度、目录编号）
NUMERIC = re.compile(r"^[\s0-9$%¢€£¥.,+\-–—/×x:*()\[\]<>|~='\"#&°]+$")
# 数字 token。四个约束缺一不可，否则会把英文单词、型号、复数标记当数字：
#   ① 必须以**真数字**开头（否则 "S1O0"、"lls"、"o&G" 会被形近字映射成 "5100"、"115"）
#   ② 左边不能紧邻字母数字（否则 "Q2"、"H100" 的尾巴会被切出来当独立数字）
#   ③ 形近字 o/O/l/I 只在数字串内部才当数字（OCR 把 2026 读成 2o26）
#   ④ 尾部单独的 s 是复数标记，先剥掉（"1990s" -> "1990"，不是 "19905"）
NUMTOK = re.compile(r"(?<![A-Za-z0-9])[0-9][0-9oOlI,.\u00a0]{0,18}")
LOOKALIKE = str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1"})

# 专名候选：全大写代码 / 型号 / 域名 / 公司全称
CODE = re.compile(r"\b[A-Z][A-Z0-9]{1,5}\b")
MODEL = re.compile(r"\b[A-Z]{1,4}-?\d{1,4}[A-Za-z]{0,3}\b")
DOMAIN = re.compile(r"\b[a-z0-9][a-z0-9.\-]*\.(?:com|org|net|ai|io|gov|edu)\b")
COMPANY = re.compile(r"\b[A-Z][A-Za-z.&]*(?:\s+[A-Z][A-Za-z.&]*){0,3}\s+"
                     r"(?:Inc|Corp|Corporation|Ltd|LLC|Co|Group|Capital|Partners|Technologies|Research)\b")
# 这些全大写词只是「全大写排版」的产物，不是术语
STOP_CAPS = {"THE", "AND", "BUT", "FOR", "NOT", "ALL", "NEW", "OUR", "YOU", "WERE",
             "THIS", "THAT", "WITH", "FROM", "ARE", "HAS", "ITS", "OUT", "PER"}


def rel(p):
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def dedup_lines(t):
    """去掉连续重复行。

    同一源块在成品里的裁剪常把紧邻的同一行也带进来（页脚尤其明显），
    不去重会把「同一条译文」误判成「两种译法」。
    """
    out, prev = [], None
    for ln in (x.strip() for x in t.splitlines()):
        if ln and ln != prev:
            out.append(ln)
        prev = ln
    return "\n".join(out)


def norm(s):
    """比较用归一化：小写 + 去空白。中文内部没有空格，英文空格位置不该影响判定。"""
    return re.sub(r"\s+", "", s).lower()


def num_runs(s, single="currency"):
    """抽数字并归一化成 digit-run 集合。

    复合 token 要拆开："9.24.26" / "1,300" / "96-103" 各拆成若干段，
    否则 "Aug. 24, 2026 -> 2026.8.24" 这种格式重排会被误报成数字丢失。

    single 控制一位数怎么处理：
      · 源侧用 "currency"——只收紧邻货币/百分号的一位数字，
        否则 "Q1"、"第 2 名" 会淹没报告；
      · 目标侧用 "all"——源侧因货币上下文被收进来的数字，在译文里
        可能脱离了货币符号（"$3.5T" -> "3.5 万亿"），两侧规则必须对称才不误报。
    """
    out = set()
    for m in NUMTOK.finditer(s):
        tok = re.sub(r"(?<=[0-9oOlI])s(?![A-Za-z])", "", m.group(0))   # 剥掉复数 s
        tok = tok.translate(LOOKALIKE)
        near = s[max(0, m.start() - 1):m.end() + 1]
        for run in re.split(r"[^\d]+", tok):
            if not run:
                continue
            run = run.lstrip("0") or "0"
            if len(run) >= 2 or single == "all" or re.search(r"[%$€£¥]", near):
                out.add(run)
    return out


def currency_runs(s):
    """紧邻货币符号的数字段——这些在中文里常换算成「亿/万亿」，值会变，不算丢失。"""
    out = set()
    for m in NUMTOK.finditer(s):
        if not re.search(r"[$€£¥]", s[max(0, m.start() - 1):m.end() + 1]):
            continue
        tok = re.sub(r"(?<=[0-9oOlI])s(?![A-Za-z])", "", m.group(0)).translate(LOOKALIKE)
        for run in re.split(r"[^\d]+", tok):
            if run:
                out.add(run.lstrip("0") or "0")
    return out


MONTH = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\b",
                   re.IGNORECASE)


def classify_number(miss, src, tgt):
    """把数字不符分四档，避免把「正常换算」「日期重排」「OCR 噪声」混进真问题里。

    unit  货币/量级换算——$97.4B 写成「974 亿美元」、30 million 写成「3000 万」
          是中文惯例，数字本来就会变，单列待确认。
    date  日期重排——"Aug 11, 2020" 写成「2020 年 8 月 11 日」、坐标轴
          的 "Jan 12" 缩成 "1/12"，月份名换成数字，逐字比对必然不符。
    noise 源侧本身就是 OCR 噪声——含 ? 或出现 10 位以上的「数字串」。
    check 其余，需人工核。
    """
    if re.search(r"[亿萬万]", tgt) and (miss <= currency_runs(src) or MAGNITUDE.search(src)):
        return "unit"
    if len(MONTH.findall(src)) >= 3 or (MONTH.search(src) and re.search(r"\d", tgt)):
        return "date"
    if NOISY_SRC.search(src) or any(len(m) > 10 for m in miss):
        return "noise"
    return "check"


def num_match(t, tgt_runs):
    """源侧数字 t 是否在译文中出现。容忍年份的两位/四位写法互换。"""
    if t in tgt_runs:
        return True
    if len(t) == 2 and ("20" + t) in tgt_runs:
        return True
    if len(t) == 4 and t[2:] in tgt_runs:
        return True
    return False


def load_keep(base):
    """L2 工作单里被标 `=` 保留原文的源串集合（有意不译）。"""
    keep = set()
    q = base / "work" / "l2_queue"
    units = {}
    row = re.compile(r"^(\d{4})\t\[[^\]]*\]\t(.*)$")
    for f in sorted(q.glob("batch_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            m = row.match(line)
            if m:
                units[int(m.group(1))] = m.group(2).strip()
    for f in sorted(q.glob("trans_*.txt")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if "\t" not in line:
                continue
            g, v = line.split("\t", 1)
            if g.strip().isdigit() and v.strip() == "=" and int(g) in units:
                keep.add(norm(units[int(g)]))
    return keep


def source_items(base):
    """源侧文本块：文字层单元 + 图内 OCR 块。页码统一成 0 基。"""
    items = []
    up = base / "work" / "units.json"
    if up.exists():
        for u in json.loads(up.read_text(encoding="utf-8")):
            items.append({"page": u["page"], "bbox": u["bbox"], "src": u["text"],
                          "layer": "text", "id": u["id"],
                          "passthrough": bool(u.get("passthrough"))})
    sp = base / "work" / "ocr_scope4.json"
    if sp.exists():
        for i, x in enumerate(json.loads(sp.read_text(encoding="utf-8"))["in_image"]):
            items.append({"page": x["page"] - 1, "bbox": x["bbox"], "src": x["text"],
                          "layer": "l2", "id": f"l2:{x['page']}:{i}",
                          "cls": x.get("cls", ""), "conf": x.get("conf", 0)})
    return items


def build_pairs(base, doc, keep):
    """对每个源块，取成品同框渲染出来的文字，得到 (src, tgt) 对照表。"""
    items = source_items(base)
    by_page = defaultdict(list)
    for i, it in enumerate(items):
        by_page[it["page"]].append(i)
    tgt = [""] * len(items)

    for pno0, idxs in by_page.items():
        if not (0 <= pno0 < doc.page_count):
            continue
        page = doc[pno0]
        rects = []
        for i in idxs:
            b = items[i]["bbox"]
            r = pymupdf.Rect(b[0], b[1], b[2], b[3])
            rects.append(r if r.width > 0.6 and r.height > 0.6 else None)
        for i, r in zip(idxs, rects):
            if r is None:
                continue
            try:
                tgt[i] = dedup_lines(page.get_text("text", clip=r))
            except Exception:
                tgt[i] = ""

    pairs = []
    for it, t in zip(items, tgt):
        s = it["src"].strip()
        ns, nt = norm(s), norm(t)
        if not s:
            continue
        if it.get("passthrough") or ns in keep or NUMERIC.match(s):
            status = "kept"
        elif not t:
            status = "empty"
        elif not CJK.search(t) and ns == nt:
            status = "unchanged"
        elif ns == nt:
            status = "same"
        else:
            status = "translated"
        pairs.append({**it, "tgt": t, "status": status})
    return pairs


def check_repeat(pairs, min_occ=2, min_len=8):
    """① 同一源串在不同位置的译文必须一致。

    判定前先做一次「包含即同」的合并：裁剪常把邻行一并带进来，
    于是同一处会出现「译文」与「译文+邻行」两种形态，它们不是分歧。
    只有互不包含的才是真分歧——比如同一个 "TTM Revenue Growth %"
    一处译「TTM 收入增长 %」、另一处译「TTM 营收增速 %」。
    """
    groups = defaultdict(list)
    for p in pairs:
        if p["status"] in ("translated", "same") and len(p["src"]) >= min_len:
            groups[norm(p["src"])].append(p)
    out = []
    for key, g in groups.items():
        if len(g) < min_occ:
            continue
        variants = defaultdict(list)
        for p in g:
            variants[norm(p["tgt"])].append(p["page"])
        if len(variants) == 1:
            continue
        reps = []
        for k in sorted(variants, key=lambda x: -len(x)):
            host = next((r for r in reps if k in r["key"] or r["key"] in k), None)
            if host:
                if len(k) > len(host["key"]):
                    host["key"] = k
                host["n"] += len(variants[k])
                host["pages"] = sorted(set(host["pages"]) | set(variants[k]))
            else:
                reps.append({"key": k, "n": len(variants[k]),
                             "pages": sorted(set(variants[k]))})
        if len(reps) < 2:
            continue
        reps.sort(key=lambda r: -r["n"])
        out.append({"src": g[0]["src"][:90], "count": len(g),
                    "variants": [{"tgt": next(x["tgt"] for x in g if norm(x["tgt"]) == r["key"]),
                                  "n": r["n"], "pages": [q + 1 for q in r["pages"]][:8]}
                                 for r in reps]})
    out.sort(key=lambda r: (-r["count"], -len(r["variants"])))
    return out


def propernoun_candidates(pairs, min_occ=3):
    """从源侧抽高频专名候选。

    只收「值得要求一致性」的：出现 ≥min_occ 个不同源块，且不是期数标记。
    两个刻意排除，否则报告会被噪声淹没：
      · 2~3 个字母的纯字母代码（US / VC / EV）——中文惯例本就常译，保留率天然低
      · 单字母 + 单个数字的期数标记（Q1 / H1 / Q2）——中文写成「第一季度」，不是漏保留
    """
    cand = defaultdict(list)
    for p in pairs:
        if p["status"] not in ("translated", "same", "unchanged", "kept"):
            continue
        s = p["src"]
        found = set()
        for rx, minlen in ((CODE, 4), (MODEL, 0), (DOMAIN, 0), (COMPANY, 0)):
            for m in rx.finditer(s):
                t = m.group(0).strip()
                if not t:
                    continue
                if rx is CODE and (t in STOP_CAPS or len(t) < minlen):
                    continue
                if rx is MODEL:
                    letters = re.match(r"[A-Z]+", t)
                    digits = re.search(r"\d+", t)
                    if letters and digits and len(letters.group(0)) < 2 and len(digits.group(0)) < 2:
                        continue
                found.add(t)
        for t in found:
            cand[t].append(p)
    cand = {t: v for t, v in cand.items() if len(v) >= min_occ}
    # 裁剪取不到文字的条目（tgt 为空）无法判断保留与否，剔出分母，
    # 否则会被算成「没保留」，把保留率压低成假红项。
    return {t: [p for p in v if p["tgt"]] for t, v in cand.items()
            if len([p for p in v if p["tgt"]]) >= min_occ}


def check_propernouns(pairs, min_occ=3):
    """② 同一专名的保留/翻译策略必须一致。"""
    cand = propernoun_candidates(pairs, min_occ)
    rows = []
    for term, occ in cand.items():
        tl = term.lower()
        kept = [p for p in occ if tl in p["tgt"].lower()]
        dropped = [p for p in occ if tl not in p["tgt"].lower()]
        rate = len(kept) / len(occ)
        if dropped and kept:
            verdict = "policy-mixed"
        elif kept:
            verdict = "kept"
        else:
            verdict = "translated"
        rows.append({"term": term, "count": len(occ), "keep_rate": round(rate, 3),
                     "verdict": verdict,
                     "kept_pages": sorted({p["page"] + 1 for p in kept})[:8],
                     "dropped": [{"page": p["page"] + 1, "src": p["src"][:70],
                                  "tgt": p["tgt"][:70]} for p in dropped[:3]]})
    order = {"policy-mixed": 0, "translated": 1, "kept": 2}
    rows.sort(key=lambda r: (order[r["verdict"]], -r["count"]))
    return rows


def intended_charset(base):
    """我们「本应写出来」的字符集合：源文本 + 我们自己的译文/计划文本 + 基本拉丁与常用标点。

    成品里出现的、不在这个集合里的字符，就是**凭空冒出来的**——
    要么是我们写错了，要么（更常见）是字体 ToUnicode 映射损坏，
    把字形写对了但把码位写成了别的值。人眼看不出来，只能靠这一条抓。
    """
    chars = set(string.printable)
    chars |= set("　、。，．：；！？「」『』（）〈〉《》—–…·％＃＆＊＋－／＜＝＞＠［］｛｝｜～＄￥€£¥°±×÷§¶†‡•‰′″⁄")
    for f, key in (("work/units.json", "text"), ("work/ocr_scope4.json", "in_image")):
        p = base / f
        if not p.exists():
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        items = d if isinstance(d, list) else d.get(key, [])
        for it in items:
            chars |= set(it.get("text", ""))
    tp = base / "work" / "translations.json"
    if tp.exists():
        for v in json.loads(tp.read_text(encoding="utf-8")).values():
            chars |= set(str(v))
    for f in ("work/l2_queue/overlay_plan_l2.json", "work/l2_queue/overlay_plan_micro.json"):
        p = base / f
        if p.exists():
            for it in json.loads(p.read_text(encoding="utf-8")).get("items", []):
                chars |= set(it.get("zh", ""))
    return chars


# 这不是损坏，是字体把相邻字符合并成了一个连字字形。
# 语义等价、视觉一致，只是让「按原串搜索」失效（搜 "——" 找不到 "⸺"），归为提示。
BENIGN_CHARS = {chr(c): f"{chr(c)} 是标准连字码位，字体把相邻字母合并成了一个字形"
                for c in range(0xFB00, 0xFB07)}
BENIGN_CHARS["\u2e3a"] = "两个 —— 合并成 two-em dash"
BENIGN_CHARS["\u2e3b"] = "三个 —— 合并成 three-em dash"
BENIGN_CHARS["\u2009"] = "U+2009 THIN SPACE 是排版层按 clreq 主动插入的中西文间距（SYNTHESIS P1-7），非损坏"


def _cmap_spans(cm):
    """把一份 ToUnicode 解析成「已覆盖的码区间」列表。

    ⚠ 必须按 beginbfrange / beginbfchar **分块**再匹配三元组。全局一把梭的正则会把
    「上一行的 <码> <Unicode>」和「下一行的 <码>」串成假三元组，凭空造出大量互相
    矛盾的重叠区间（实测踩过，白排查一轮）。
    另外只存区间、不展开成码表——Noto Serif SC 一份 CMap 就有 3 万个码，展开会吃 GB 级内存。
    """
    spans = []
    for blk in re.findall(r"beginbfrange(.*?)endbfrange", cm, re.S):
        for lo, hi in re.findall(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*<[0-9A-Fa-f]+>", blk):
            spans.append((int(lo, 16), int(hi, 16)))
    for blk in re.findall(r"beginbfchar(.*?)endbfchar", cm, re.S):
        for src in re.findall(r"<([0-9A-Fa-f]+)>\s*<[0-9A-Fa-f]+>", blk):
            c = int(src, 16)
            spans.append((c, c))
    return spans


def charset_evidence(doc):
    """把每个「凭空出现」的字符追到 (字体, 字形码)，并判该码有没有被 ToUnicode 收录。

    **这是判良性/损坏的正确口径。** 本 skill 里的损坏根因是：`insert_htmlbox` 有时把
    字符的**码**写成字体原始字形的 id，越过该字体 ToUnicode 的覆盖上限 → 抽取端回退成
    `chr(code)`。连字（`⸺`/`ﬃ`）则相反——**表里有条目**，只是指向连字码位，属良性。

    所以「码在 CMap 内 = 良性提示；码越界 = 真损坏」，比照着一张写死的连字表判更可靠。
    """
    names = {}          # 字体显示名 -> 覆盖区间
    xref_cache = {}     # font xref -> 显示名
    ev = defaultdict(list)
    for pno in range(doc.page_count):
        page = doc[pno]
        for f in page.get_fonts(full=True):
            xref, basefont = f[0], f[3]
            if xref in xref_cache:
                continue
            name = basefont.split("+", 1)[1] if "+" in basefont else basefont
            xref_cache[xref] = name
            obj = doc.xref_object(xref, compressed=True)
            m = re.search(r"/ToUnicode\s+(\d+)", obj)
            names[name] = _cmap_spans(doc.xref_stream(int(m.group(1))).decode("latin-1")) if m else []
        try:
            trace = page.get_texttrace()
        except Exception:                      # 少数版本/页面取不到，跳过而非崩
            continue
        for sp in trace:
            spans = names.get(sp.get("font", ""), [])
            for ch in sp.get("chars", []):
                if len(ch) < 2:
                    continue
                raw, gid = ch[0], ch[1]
                # ToUnicode 里没条目时，trace 给的是 0xFFFD，而 get_text() 给的是
                # `chr(字形码)` —— 正是我们要抓的那批。**不能把它们过滤掉**，
                # 否则证据全部落空（踩过：看起来"取证为空"，其实是自己滤掉了）。
                c = chr(gid) if raw in (0xFFFD, 0, -1) else chr(raw)
                covered = any(lo <= gid <= hi for lo, hi in spans)
                ev[c].append((pno + 1, sp.get("font", ""), gid, covered))
    return ev


def check_charset(base, doc, intended):
    """⑤ 字符集核验：成品文字层里有没有「我们从未写过」的字符。

    这是抓字体映射损坏最有效的一招——损坏的特征就是「字形对、码位错」，
    成品里会冒出一批我们根本没写过的字符。人眼和对照图都发现不了。
    """
    seen = defaultdict(list)
    for pno in range(doc.page_count):
        for b in doc[pno].get_text("dict")["blocks"]:
            if b["type"] == 1:
                continue
            for ln in b.get("lines", []):
                t = "".join(sp["text"] for sp in ln["spans"])
                for c in t:
                    if c not in intended and not c.isspace():
                        seen[c].append((pno + 1, t.strip()[:70]))
    evidence = charset_evidence(doc)
    rows = []
    for c, v in sorted(seen.items(), key=lambda kv: -len(kv[1])):
        # 取该字符的一个真实字形码作证据；越界即真损坏，在表内即良性连字
        fonts, codes, outside = set(), set(), 0
        for _, fnt, gid, covered in evidence.get(c, []):
            fonts.add(fnt)
            codes.add(gid)
            if not covered:
                outside += 1
        row = {"char": c, "code": f"U+{ord(c):04X}", "count": len(v),
               "benign": BENIGN_CHARS.get(c, ""),
               "pages": sorted({p for p, _ in v})[:10],
               "examples": [{"page": p, "text": t} for p, t in v[:2]],
               "fonts": sorted(fonts), "glyph_codes": sorted(codes)[:6],
               "outside_cmap": outside, "traced": len(evidence.get(c, []))}
        rows.append(row)
    return rows


def charset_verdict(row):
    """良性还是真损坏 —— 优先用「字形码有没有越出 ToUnicode 覆盖范围」这个硬证据。

    越界 → 抽取端只能 `chr(code)` 兜底 → 真损坏；
    在表内 → 是连字或正常字形，只是码位不是我们写的那一个 → 良性提示。
    取不到 trace 证据时，退回按连字码位表判。
    """
    if row["outside_cmap"]:
        return "broken"
    if row["traced"]:
        return "benign"
    return "benign" if row["benign"] else "broken"


MAGNITUDE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:million|billion|trillion|thousand)\b|[$\d]\s*[KMBT]\b",
                       re.IGNORECASE)
NOISY_SRC = re.compile(r"\d\s*[?？]|[?？]\s*\d")


def check_numbers(pairs, skip_chars=()):
    """④ 单元内数字守恒。tgt 里含损坏字符的条目交给 ⑥，不在这里重复报。"""
    bad, skipped = [], 0
    for p in pairs:
        if p["status"] not in ("translated", "same", "unchanged"):
            continue
        if skip_chars and any(c in p["tgt"] for c in skip_chars):
            skipped += 1
            continue
        sn = num_runs(p["src"], single="currency")
        if not sn:
            continue
        tn = num_runs(p["tgt"], single="all")
        miss = {t for t in sn if not num_match(t, tn)}
        if not miss:
            continue
        bad.append({"page": p["page"] + 1, "layer": p["layer"],
                    "kind": classify_number(miss, p["src"], p["tgt"]),
                    "missing": sorted(miss), "src": p["src"][:90], "tgt": p["tgt"][:90]})
    order = {"check": 0, "date": 1, "unit": 2, "noise": 3}
    bad.sort(key=lambda r: (order[r["kind"]], -len(r["missing"])))
    return bad, skipped


def paren_evidence(pairs, cap=400):
    """③ 括号术语证据：X（English）是作者亲手写下的术语对照，供人工抽检。"""
    ev = defaultdict(list)
    rx = re.compile(r"[\u4e00-\u9fff]{0,12}[（(]\s*([A-Za-z][A-Za-z0-9 .\-&+/']{1,28}?)\s*[)）]")
    rx2 = re.compile(r"([A-Za-z][A-Za-z0-9 .\-&+/']{1,28}?)\s*[（(]([\u4e00-\u9fff]{1,14})[)）]")
    for p in pairs:
        for en in rx.findall(p["tgt"]):
            ev[en.lower()].append(p)
        for en, _zh in rx2.findall(p["tgt"]):
            ev[en.lower()].append(p)
    rows = []
    for en, occ in sorted(ev.items(), key=lambda kv: -len(kv[1])):
        rows.append({"en": en, "count": len(occ),
                     "samples": [{"page": p["page"] + 1, "tgt": p["tgt"][:100]} for p in occ[:4]]})
    return rows[:cap]


def check_glossary(base, pairs):
    """⑤ 冻结术语表逐条核验。需要 work/glossary.json。"""
    gp = base / "work" / "glossary.json"
    if not gp.exists():
        return {"frozen": False, "violations": [], "detail": "未找到 work/glossary.json，跳过"}
    g = json.loads(gp.read_text(encoding="utf-8"))
    terms = g.get("terms", g if isinstance(g, list) else [])
    viol = []
    for t in terms:
        en, zh = t.get("en", ""), t.get("zh", "")
        if not en or not zh:
            continue
        rx = re.compile(r"\b" + re.escape(en) + r"\b", re.IGNORECASE)
        for p in pairs:
            if p["status"] not in ("translated", "same"):
                continue
            if not rx.search(p["src"]):
                continue
            if zh not in p["tgt"] and en.lower() not in p["tgt"].lower():
                viol.append({"en": en, "zh": zh, "page": p["page"] + 1,
                             "src": p["src"][:80], "tgt": p["tgt"][:80]})
    return {"frozen": bool(g.get("frozen")), "term_count": len(terms),
            "violations": viol,
            "detail": f"{len(terms)} 条术语，{len(viol)} 处不符"}


def check_negation(pairs):
    """⑦ 否定词守恒（SYNTHESIS P0-7）——提示项，**不自动 FAIL**。

    否定反转（"禁止靠近"→"靠近"）最危险且所有相似度指标都抓不到（实测：
    否定词被删 chrF 仍 86.6）。但自动检测召回不足（弯引号 n't、同义构词、
    隐式否定），误判难免，所以只把「源有显式否定、译文零否定痕迹」的条目
    提示出来强制进人工队列；是否升级 Critical 按内容风险定，属项目约定。
    隐式否定（except/without/fail to）单列，只提示不计数。
    """
    neg_re = re.compile(r"\b(not|no|never|none|neither|nor|cannot|n[o']t)\b", re.I)
    impl_re = re.compile(r"\b(except|without|excluding|fail(?:ed|s)? to|lack|absent|free of)\b", re.I)
    zh_neg = "不没无未非别勿莫免禁止不得"
    hints, impl = [], []
    for p in pairs:
        if p.get("status") not in ("translated",):
            continue
        s, t = p.get("src", ""), p.get("tgt", "")
        n_src = len(neg_re.findall(s))
        if n_src and not any(c in t for c in zh_neg):
            hints.append({"page": p["page"] + 1, "layer": p["layer"],
                          "src": s[:90], "tgt": t[:90], "n_src_neg": n_src})
        if impl_re.search(s) and not any(c in t for c in zh_neg + "除外除"):
            impl.append({"page": p["page"] + 1, "layer": p["layer"],
                         "src": s[:90], "tgt": t[:90]})
    return {"explicit": hints, "implicit": impl,
            "note": "提示项：强制进人工队列，不自动 FAIL（MQM 严重度不绑定错误类型）"}


def proposal(rows_pn, rows_paren, repeat):
    """候选术语表：把三类证据合成一份待裁决清单。人工改完存成 glossary.json。"""
    terms = []
    for r in rows_pn:
        if r["verdict"] in ("policy-mixed", "translated"):
            ev = [{"page": d["page"], "src": d["src"], "tgt": d["tgt"]} for d in r["dropped"][:2]]
            terms.append({"en": r["term"], "zh": "", "keep_rate": r["keep_rate"],
                          "count": r["count"], "verdict": r["verdict"], "evidence": ev})
    for r in rows_paren:
        if not any(t["en"].lower() == r["en"] for t in terms):
            terms.append({"en": r["en"], "zh": "", "keep_rate": None, "count": r["count"],
                          "verdict": "paren", "evidence": r["samples"][:2]})
    return terms


def write_sample_md(path, rows_pn, rows_paren, repeat, n_terms):
    L = ["# 术语一致性 · 人工抽检表", "",
         "用法：对每个术语扫一眼下面几处出现，判断「是否全篇统一」。",
         "发现有分歧的，写进 `work/glossary.json` 冻结下来，之后 96 会自动硬核验。", ""]
    if repeat:
        L += [f"## 一、同一源串译文不一致（{len(repeat)} 项，硬红项）", ""]
        for r in repeat[:25]:
            L.append(f"- **{r['src'][:70]}**  出现 {r['count']} 次，{len(r['variants'])} 种译法")
            for v in r["variants"]:
                L.append(f"    - ×{v['n']}  {v['tgt'][:70]}   p{v['pages']}")
        L.append("")
    L += [f"## 二、专名保留策略", ""]
    for v, label in (("policy-mixed", "策略不一致（红项）"), ("translated", "全译成中文（需确认）"),
                     ("kept", "全部保留原文（通过）")):
        grp = [r for r in rows_pn if r["verdict"] == v]
        if not grp:
            continue
        L.append(f"### {label} — {len(grp)} 项")
        L.append("")
        for r in grp[:n_terms]:
            L.append(f"- `{r['term']}` ×{r['count']}  保留率 {r['keep_rate']:.0%}")
            for d in r["dropped"][:2]:
                L.append(f"    - p{d['page']} 源：{d['src'][:60]}")
                L.append(f"      p{d['page']} 译：{d['tgt'][:60]}")
        L.append("")
    L += [f"## 三、括号术语证据（{len(rows_paren)} 项）", ""]
    for r in rows_paren[:n_terms]:
        L.append(f"- `{r['en']}` ×{r['count']}")
        for s in r["samples"][:3]:
            L.append(f"    - p{s['page']}  {s['tgt'][:80]}")
    Path(path).write_text("\n".join(L) + "\n", encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="output/product.pdf")
    ap.add_argument("--min-occ", type=int, default=3, help="专名候选的最低出现块数")
    ap.add_argument("--sample-terms", type=int, default=30, help="抽检表里每类最多列几条")
    args = ap.parse_args()

    pdf = Path(args.pdf)
    if not pdf.is_absolute():
        pdf = BASE / pdf
    doc = pymupdf.open(pdf)
    keep = load_keep(BASE)

    pairs = build_pairs(BASE, doc, keep)
    intended = intended_charset(BASE)
    charset = check_charset(BASE, doc, intended)
    bad_chars = {r["char"] for r in charset if charset_verdict(r) == "broken"}
    # 映射损坏的条目里，「译文」本身就是乱码，拿去比术语/专名只会产生连带的假阳性。
    # 统一交给 ⑥ 报一次，其余检查只看干净条目。
    clean = [p for p in pairs if not any(c in p["tgt"] for c in bad_chars)]
    repeat = check_repeat(clean)
    rows_pn = check_propernouns(clean, args.min_occ)
    rows_paren = paren_evidence(clean)
    numbers, num_skipped = check_numbers(pairs, skip_chars=bad_chars)
    gloss = check_glossary(BASE, pairs)
    negation = check_negation(pairs)

    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "pairs.json").write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
    (OUTDIR / "terms.json").write_text(json.dumps(
        {"repeat_conflicts": repeat, "propernouns": rows_pn, "paren": rows_paren,
         "glossary": gloss}, ensure_ascii=False, indent=1), encoding="utf-8")
    (OUTDIR / "negation.json").write_text(json.dumps(negation, ensure_ascii=False, indent=1),
                                          encoding="utf-8")
    (OUTDIR / "numbers.json").write_text(json.dumps(numbers, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    (OUTDIR / "charset.json").write_text(json.dumps(charset, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    (OUTDIR / "glossary_proposed.json").write_text(json.dumps(
        {"note": "候选术语表：人工填 zh 后另存为 work/glossary.json 并设 frozen=true，"
                 "之后 64_audit_text.py 会逐条硬核验",
         "terms": proposal(rows_pn, rows_paren, repeat)},
        ensure_ascii=False, indent=1), encoding="utf-8")
    write_sample_md(OUTDIR / "terms_sample.md", rows_pn, rows_paren, repeat, args.sample_terms)

    st = Counter(p["status"] for p in pairs)
    mixed = [r for r in rows_pn if r["verdict"] == "policy-mixed"]
    print(f"对照表        : {len(pairs)} 条源块（文字层 {sum(1 for p in pairs if p['layer']=='text')} / "
          f"图内 {sum(1 for p in pairs if p['layer']=='l2')}）")
    print(f"  状态分布    : {dict(st)}")
    print(f"\n① 重复源串不一致 : {len(repeat)} 项  {'红项' if repeat else '通过'}")
    print(f"② 专名策略不一致 : {len(mixed)} 项  {'红项' if mixed else '通过'}"
          f"（共 {len(rows_pn)} 个专名候选）")
    print(f"③ 括号术语证据   : {len(rows_paren)} 项（人工抽检，见 terms_sample.md）")
    lost = [r for r in numbers if r["kind"] == "check"]
    unit = [r for r in numbers if r["kind"] == "unit"]
    date = [r for r in numbers if r["kind"] == "date"]
    noise = [r for r in numbers if r["kind"] == "noise"]
    print(f"④ 数字丢失       : {len(lost)} 处  {'红项' if lost else '通过'}"
          + (f"；{len(unit)} 处货币/量级换算" if unit else "")
          + (f"；{len(date)} 处日期重排" if date else "")
          + (f"；{len(noise)} 处源侧 OCR 噪声" if noise else "")
          + (f"；{num_skipped} 条字符映射损坏归入 ⑤" if num_skipped else ""))
    broken = [r for r in charset if charset_verdict(r) == "broken"]
    soft = [r for r in charset if charset_verdict(r) != "broken"]
    if broken:
        codes = sorted(ord(r["char"]) for r in broken)
        run = (max(codes) - min(codes) + 1) == len(codes)
        tol = "；".join(f"{r['char']}({r['code']}×{r['count']})" for r in broken[:10])
        if len(broken) > 10:
            tol += f" …… 共 {len(broken)} 个"
        print(f"⑤ 字符集核验     : {len(broken)} 个「凭空出现」的字符  红项")
        print(f"     {tol}")
        if run:
            print(f"     ⚠ 码位是连续区段 U+{codes[0]:04X}–U+{codes[-1]:04X}"
                  f" → 字形码超出该字体 ToUnicode 覆盖范围，抽取端回退成 chr(code)")
        # 取证：按字体去重，最多举两例（数字与连字常落在不同字体上）
        shown = set()
        for r in broken:
            if not r["fonts"] or r["fonts"][0] in shown:
                continue
            shown.add(r["fonts"][0])
            print(f"     取证：{r['char']} 出现在 {r['fonts'][0]}，字形码 "
                  f"{', '.join('0x%04X' % g for g in r['glyph_codes'][:3])}"
                  f"，不在该字体 ToUnicode 内 {r['outside_cmap']} 次")
            if len(shown) >= 2:
                break
    else:
        print("⑤ 字符集核验     : 通过")
    for r in soft:
        why = r["benign"] or ("字形码在 ToUnicode 覆盖范围内，属正常字形"
                              f"（字形码 {', '.join('0x%04X' % g for g in r['glyph_codes'][:3])}）"
                              if r["traced"] else "未取到字形证据，按连字码位表判为良性")
        print(f"     提示：{r['char']}（{r['code']}×{r['count']}）= {why}，不影响观感，"
              f"但按原串搜索会失效")
    print(f"⑥ 冻结术语表     : {gloss['detail']}")
    print(f"⑦ 否定词守恒     : 显式 {len(negation['explicit'])} 处 / "
          f"隐式 {len(negation['implicit'])} 处待人工核"
          f"（提示项，不自动 FAIL；见 negation.json）")
    for r in repeat[:5]:
        print(f"    · {r['src'][:60]!r} ×{r['count']} -> {len(r['variants'])} 种译法")
    for r in mixed[:5]:
        print(f"    · 专名 {r['term']} ×{r['count']} 保留率 {r['keep_rate']:.0%}")
    for r in lost[:5]:
        print(f"    · p{r['page']} 缺 {r['missing']}  源 {r['src'][:52]!r}")
    for r in unit[:3]:
        print(f"    · [换算] p{r['page']} {r['missing']}  源 {r['src'][:46]!r}")
    print(f"\n输出 -> {rel(OUTDIR)}/{{pairs,terms,numbers,charset,glossary_proposed}}.json "
          f"+ terms_sample.md")


if __name__ == "__main__":
    main()
