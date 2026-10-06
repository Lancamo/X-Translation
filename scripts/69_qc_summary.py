#!/usr/bin/env python3
"""69_qc_summary.py — 验收总表：把 T0–T3 与残迹扫描的结果汇总成一张 QC_REPORT.md。

为什么还要一层汇总：T0–T3 各自输出 JSON，散在四处，加上原有的 rebuild_report、
覆盖计划、残迹核验，验收时要看的文件有十来个。汇总层做两件事——
  ① 把「四路覆盖率」算成一个数（文字层 / 图内 / 微标签 / 标题位图），
     现在这四个数字散在四个中间产物里，没有一处对得上；
  ② 把所有红项收成一份待处理清单，交付前照着划掉即可。

只读，不做任何判定——判定分散在 T0–T3 里，这一层不重复判断，只做汇总与呈现。

用法：
  python 69_qc_summary.py --pdf output/xxx_zh-CN.pdf
输出：work/verify/QC_REPORT.md
"""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
OUTDIR = BASE / "work" / "verify"


def rel(p):
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def load(p, default=None):
    p = Path(p)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def cell(v):
    """表格单元格净化：折叠换行与管道符。

    源/译文本里带换行是常态，直接塞进 Markdown 表格会把整张表打断
    （实测过，表格渲染成一片散行）。表格只是索引，完整原文在对应 JSON 里。
    """
    return re.sub(r"\s+", " ", str(v)).replace("|", "\\|").strip() or "—"


def table(rows, head):
    out = ["| " + " | ".join(head) + " |",
           "|" + "|".join("---" for _ in head) + "|"]
    for r in rows:
        out.append("| " + " | ".join(cell(x) for x in r) + " |")
    return out


def charset_broken(r):
    """良性/损坏的判据，与 64_audit_text.py 保持同一口径。

    硬证据优先：**字形码越出该字体 ToUnicode 的覆盖范围 → 抽取端只能 chr(code) 兜底 → 真损坏**；
    码在表内 → 连字或正常字形，只是码位不是我们写的那个 → 良性。
    取不到 trace 证据时才退回按连字码位表判。
    （刻意重抄而不是 import——skill 脚本会被改名，兄弟 import 会断。）
    """
    if r.get("outside_cmap"):
        return True
    if r.get("traced"):
        return False
    return not r.get("benign")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="output/product.pdf")
    args = ap.parse_args()

    V = OUTDIR
    structure = load(V / "structure.json")
    terms = load(V / "terms.json")
    numbers = load(V / "numbers.json", [])
    charset = load(V / "charset.json", [])
    pairs = load(V / "pairs.json", [])
    plan = load(V / "sample_plan.json")
    back = load(V / "backtrans.json", [])
    rebuild = load(BASE / "work" / "rebuild_report.json", [])
    residue = load(BASE / "work" / "residue" / "residue.json", [])
    verified = load(BASE / "work" / "residue" / "unseen_verified.json", [])
    l2plan = load(BASE / "work" / "l2_queue" / "overlay_plan_l2.json", {"items": []})
    microplan = load(BASE / "work" / "l2_queue" / "overlay_plan_micro.json", {"items": []})
    cover_art = BASE / "work" / "cover_art.png"

    L = ["# 验收总表", "",
         f"- 成品：`{args.pdf}`",
         f"- 目录：`{rel(BASE)}`", ""]

    todo = []

    # ---------- 交付结论 ----------
    L += ["## 一、交付结论", ""]
    verdict_rows = []

    # 源文件完整性排在最前——它是绝对红线，其余各项都在"源没被动过"这个前提之下
    # 才有意义。这里只读 67_source_guard.py --check 落下的结论，不自己算哈希。
    src_check = load(OUTDIR / "source_check.json")
    if src_check is None:
        verdict_rows.append(["源文件完整性", "未核验",
                             "未见 source_check.json——收工前须跑 67_source_guard.py --check"])
        todo.append("★ 源文件完整性未核验：跑 `67_source_guard.py --check`（绝对红线要求）")
    else:
        ok = src_check.get("status") == "pass"
        warn_n = src_check.get("warn", 0)
        verdict_rows.append(["源文件完整性", "未变" if ok else "★ 内容已变",
                             f"{len(src_check.get('files', []))} 个文件，"
                             f"核验于 {src_check.get('checked_at', '—')}"
                             + (f"；另有 {warn_n} 项时间戳提示" if warn_n else "")])
        if not ok:
            todo.append("★★ 源文件内容已变（绝对红线）——**停止交付**，"
                        "先查清是谁改的、能否恢复，不要做补救性覆盖")

    t0_status = structure["verdict"] if structure else "未跑（缺 structure.json）"
    if structure and structure["counts"]["FAIL"]:
        todo.append(f"T0 结构断言 {structure['counts']['FAIL']} 项 FAIL："
                    + "；".join(f"{c['name']}（{c['detail']}）"
                                for c in structure["checks"] if c["status"] == "FAIL"))
    verdict_rows.append(["T0 结构断言", t0_status,
                         f"PASS {structure['counts']['PASS']} / FAIL {structure['counts']['FAIL']} "
                         f"/ SKIP {structure['counts']['SKIP']}" if structure else "—"])

    t1_red = {"术语·重复源串不一致": len(terms["repeat_conflicts"]) if terms else 0,
              "术语·专名策略不一致": sum(1 for r in terms["propernouns"] if r["verdict"] == "policy-mixed")
              if terms else 0,
              "数字丢失": sum(1 for r in numbers if r["kind"] == "check"),
              "字符映射损坏": sum(1 for r in charset if charset_broken(r))}
    t1_total = sum(t1_red.values())
    verdict_rows.append(["T1 文本完整性", "通过" if t1_total == 0 else f"{t1_total} 项红项",
                         " / ".join(f"{k} {v}" for k, v in t1_red.items() if v) or "全部通过"])
    for k, v in t1_red.items():
        if v:
            todo.append(f"T1 {k}：{v} 项，见 `work/verify/`")

    t2_pages = sum(len(v) for v in plan["plan"].values()) if plan else 0
    t2_pool = len(plan.get("forced_pool", [])) if plan else 0
    verdict_rows.append(["T2 分层抽检",
                         f"{t2_pages} 页随机 + {t2_pool} 页强制池，待人判" if plan else "未跑",
                         ("分层 " + " / ".join(f"{k} {v}" for k, v in plan["strata"].items())
                          + (f"；风险倾斜强制池 {t2_pool} 页（≤20%）" if t2_pool else "")
                          + (f"；样本词数 {plan.get('sampled_words','—')}"
                             + ("，Inconclusive" if plan.get("inconclusive") else "")) if plan else "—")])
    if plan:
        todo.append(f"T2 逐页人判：{t2_pages + t2_pool} 页（含高风险强制池），"
                    f"清单见 `work/verify/REVIEW.md`")

    t3 = Counter(r["verdict"] for r in back) if back else Counter()
    verdict_rows.append(["T3 回译核验",
                         f"flag {t3.get('flag',0)} / watch {t3.get('watch',0)}" if back else "未跑（可选）",
                         " / ".join(f"{k} {v}" for k, v in t3.items()) if back else "—"])
    if t3.get("flag"):
        todo.append(f"T3 回译 flag {t3['flag']} 条，见 `work/verify/backtrans.json`")

    real = [r for r in verified if r.get("verdict") == "real"] if verified else []
    if real:
        todo.append(f"残迹核验判定为真的 {len(real)} 处（多为品牌名/代码，确认后可放行）")

    # 视觉回归（96b，并表不并分：独立维度列，不并入 T0–T3 的判定）
    vr = load(V / "visual_regress.json")
    if vr:
        fl = vr.get("flags", {})
        n_flag = len(set(fl.get("ssim", [])) | set(fl.get("ae", [])) | set(fl.get("iou", [])))
        verdict_rows.append(["视觉回归（SSIM/AE/IoU）",
                             f"{n_flag} 页 flagged" if n_flag else "通过",
                             f"SSIM flagged {len(fl.get('ssim', []))} / "
                             f"AE flagged {len(fl.get('ae', []))} / "
                             f"IoU flagged {len(fl.get('iou', []))} / "
                             f"AE warn {len(fl.get('warn_ae', []))}；"
                             f"热力图 `work/verify/visual/`"])
        if n_flag:
            todo.append(f"视觉回归 flagged {n_flag} 页（SSIM/AE/IoU 并表不并分），"
                        f"页清单见 `work/verify/visual_regress.json`，优先进 T2 人审")

    L += table(verdict_rows, ["层", "结论", "明细"])
    L.append("")

    # ---------- T0 ----------
    if structure:
        L += ["## 二、T0 结构断言", ""]
        L += table([[c["status"], c["name"], c["detail"]] for c in structure["checks"]],
                   ["状态", "检查项", "结果"])
        if structure.get("untranslated_titles"):
            L += ["", "未译标题：", ""]
            for t in structure["untranslated_titles"][:30]:
                L.append(f"- p{t['page']+1}  {t['size']:.1f}pt  `{t['text']}`")
        L.append("")

    # ---------- T1 ----------
    if terms:
        L += ["## 三、T1 文本完整性", ""]
        if terms["repeat_conflicts"]:
            L += [f"### 重复源串译文不一致（{len(terms['repeat_conflicts'])} 项，硬红项）", ""]
            for r in terms["repeat_conflicts"]:
                L.append(f"- `{cell(r['src'])}`  出现 {r['count']} 次，{len(r['variants'])} 种译法")
                for v in r["variants"]:
                    L.append(f"    - ×{v['n']}  {cell(v['tgt'])}   p{v['pages']}")
            L.append("")
        mixed = [r for r in terms["propernouns"] if r["verdict"] == "policy-mixed"]
        if mixed:
            L += [f"### 专名保留策略不一致（{len(mixed)} 项）", ""]
            for r in mixed:
                L.append(f"- `{r['term']}` ×{r['count']} 保留率 {r['keep_rate']:.0%}")
            L.append("")
        else:
            L += [f"专名保留策略：一致（{len(terms['propernouns'])} 个候选，无分歧）", ""]
        if terms.get("glossary"):
            L += [f"冻结术语表：{terms['glossary']['detail']}", ""]

    if numbers:
        lost = [r for r in numbers if r["kind"] == "check"]
        L += [f"### 数字校验", ""]
        cnt = Counter(r["kind"] for r in numbers)
        L += [f"- 检出 {len(numbers)} 处："
              + " / ".join(f"{k} {v}" for k, v in cnt.items())
              + "（仅 `check` 为红项，其余为换算/日期重排/源侧噪声）", ""]
        if lost:
            L += table([[f"p{r['page']}", r["layer"], r["missing"], r["src"][:56], r["tgt"][:56]]
                        for r in lost],
                       ["页", "层", "缺", "源", "译"])
            L.append("")

    if charset:
        broken = [r for r in charset if charset_broken(r)]
        L += [f"### 字符集核验", ""]
        if broken:
            codes = sorted(ord(r["char"]) for r in broken)
            L += [f"成品里出现 {len(broken)} 个「我们从未写过」的字符：", ""]
            L += table([[r["char"], r["code"], r["count"], r["examples"][0]["page"],
                         r["examples"][0]["text"][:44]] for r in broken],
                       ["字符", "码位", "次数", "首见页", "上下文"])
            L.append("")
            if (max(codes) - min(codes) + 1) == len(codes) and len(codes) > 3:
                L += [f"> ⚠ 码位为连续区段 `U+{codes[0]:04X}`–`U+{codes[-1]:04X}`，"
                      f"是**字形码越出该字体 ToUnicode 覆盖范围**的典型特征："
                      f"**字形渲染正确，但复制/搜索/文本抽取得到的全是错码**。修法见 pitfalls。", ""]
            hint = next((r for r in broken if r.get("fonts")), None)
            if hint:
                L += [f"- 取证：`{hint['char']}` 出现在 `{hint['fonts'][0]}`，"
                      f"字形码 {', '.join('0x%04X' % g for g in hint['glyph_codes'])}"
                      f"（越界 {hint['outside_cmap']} 次）。可用 `page.get_texttrace()` 复核。", ""]
        benign = [r for r in charset if not charset_broken(r)]
        if benign:
            L += ["合字 / 表内字形（不影响观感，仅影响按原串搜索）："
                  + "、".join(f"{r['char']} ×{r['count']}" for r in benign), ""]

    # ---------- 覆盖率 ----------
    if pairs:
        st = Counter(p["status"] for p in pairs)
        L += ["## 四、覆盖率", ""]
        rows = []
        for layer, label in (("text", "文字层"), ("l2", "图内 / 微标签")):
            g = [p for p in pairs if p["layer"] == layer]
            c = Counter(p["status"] for p in g)
            duty = c["translated"] + c["unchanged"]
            rate = (100.0 * c["translated"] / duty) if duty else 100.0
            rows.append([label, len(g), c["translated"], c["kept"], c["unchanged"], c["empty"],
                         f"{rate:.1f}%"])
        L += table(rows, ["层", "源块数", "已译", "有意保留", "未变（残迹）", "取不到文字", "覆盖率"])
        L += ["", f"- 图内覆盖计划：L2 {len(l2plan.get('items', []))} 条"
                  f" + 微标签 {len(microplan.get('items', []))} 条", ""]
        if st["unchanged"]:
            L += [f"- ⚠ {st['unchanged']} 条源块在成品里与原文字面相同，"
                  f"疑似残迹：见 `pairs.json` 里 status=unchanged", ""]

    if rebuild:
        floor = 0.985
        shrunk = [r for r in rebuild if r.get("scale", 1) < floor]
        L += ["## 五、排版指标", "",
              f"- 回填单元 {len(rebuild)} 条，其中缩放率 < {floor} 的 **{len(shrunk)}** 条"
              + ("（需逐条确认是否可读）" if shrunk else "（无异常缩放）"), ""]
        if shrunk:
            L += table([[f"p{r['page']+1}", r["id"], f"{r['scale']:.3f}",
                         f"{r.get('size','—')}",
                         f"{r.get('src_len','—')} → {r.get('zh_len','—')} 字"]
                        for r in shrunk[:20]],
                       ["页", "id", "缩放率", "字号", "源/译字数"])
            L.append("")

    if residue:
        rc = Counter(r["cls"] for r in residue)
        L += ["## 六、残留外文（反向扫描）", "",
              "- 分类：" + " / ".join(f"{k} {v}" for k, v in rc.most_common()), ""]
        if verified:
            vc = Counter(r["verdict"] for r in verified)
            L += ["- 回源核验：" + " / ".join(f"{k} {v}" for k, v in vc.most_common()),
                  "  （`real` 才是真残迹，`noise` 是 OCR 把中文读成了外文）", ""]

    # ---------- 待处理 ----------
    L += ["## 七、待处理清单", ""]
    if todo:
        for i, t in enumerate(todo, 1):
            L.append(f"{i}. [ ] {t}")
    else:
        L.append("无需处理项。")
    L.append("")

    out = V / "QC_REPORT.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(L) + "\n", encoding="utf-8")

    print(f"T0 结构断言   : {t0_status}")
    print(f"T1 红项       : {t1_total} 项  " + " / ".join(f"{k} {v}" for k, v in t1_red.items() if v))
    print(f"T2 抽检       : {t2_pages} 页待人判")
    print(f"T3 回译       : " + (" / ".join(f"{k} {v}" for k, v in t3.items()) if t3 else "未跑"))
    print(f"待处理项      : {len(todo)} 条")
    print(f"\n输出 -> {rel(out)}")


if __name__ == "__main__":
    main()
