#!/usr/bin/env python3
"""24b_freeze_glossary.py — 从 96 的 glossary_proposed.json 生成可冻结的术语表。

流程（SYNTHESIS P0-5）：96 审计产出候选表 → 本脚本自动填充**一致**译法 →
分歧项留空标 needs_ruling（人工裁决后重跑本脚本或手改）→ frozen=true 后
96 的 ⑥ 逐条硬核验「写了=生效了」。

自动填充规则：某术语在成品里的译法若**完全一致**（出现 ≥1 次且唯一），
直接采纳；有分歧的**绝不自动裁决**（那 4 项重复源串分歧是用户的待办）。
保留原文类（verdict=kept 语义，即成品里仍是英文）→ zh 留空、keep=true。

用法：python3 24b_freeze_glossary.py            # 生成/更新 work/glossary.json
      python3 24b_freeze_glossary.py --freeze   # 全部无 needs_ruling 后才允许
"""
import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--freeze", action="store_true",
                    help="置 frozen=true（须先清零 needs_ruling）")
    args = ap.parse_args()

    prop = json.loads((BASE / "work" / "verify" / "glossary_proposed.json").read_text(encoding="utf-8"))
    cands = prop.get("terms", prop if isinstance(prop, list) else [])
    pairs = json.loads((BASE / "work" / "verify" / "pairs.json").read_text(encoding="utf-8"))

    terms, n_auto, n_ruling = [], 0, 0
    for t in cands:
        en = t.get("en", "").strip()
        if not en:
            continue
        rx = re.compile(r"\b" + re.escape(en) + r"\b", re.IGNORECASE)
        tgts = [p["tgt"] for p in pairs
                if p.get("status") in ("translated", "same") and rx.search(p.get("src", ""))]
        if not tgts:
            continue
        # 术语的实际译法：tgt 里去掉英文后的中文片段——太脆弱；
        # 改用「tgt 是否一致」判定：一致则整条 tgt 作为 evidence，zh 只在
        # 能唯一提取时填。提取不到就标 needs_ruling。
        uniq = set(tgts)
        zh = (t.get("zh") or "").strip()
        keep = t.get("verdict") in ("kept",)
        needs = bool(t.get("needs_ruling"))
        if not zh and len(uniq) == 1 and t.get("verdict") == "paren":
            # 括号术语类：形如「中文（English）」，取括号外的中文
            m = re.match(r"\s*([^（(]+)", tgts[0])
            if m and m.group(1).strip() and m.group(1).strip() != en:
                zh = m.group(1).strip()
        if not zh and all(en.lower() in x.lower() for x in tgts):
            # 全部出现都保留英文 → 冻结为 keep 条目（事实，不是裁决）
            keep = True
            needs = False
            n_auto += 1
        elif not zh:
            needs = True
            n_ruling += 1
        else:
            n_auto += 1
        terms.append({"en": en, "zh": zh,
                      "type": {"paren": "term"}.get(t.get("verdict"), "term"),
                      "keep": keep,
                      "count": t.get("count", len(tgts)),
                      "needs_ruling": needs,
                      "evidence": t.get("evidence", [])[:2],
                      "frozen_at": datetime.now().isoformat(timespec="seconds")})

    n_need = sum(1 for t in terms if t["needs_ruling"])
    frozen = args.freeze and n_need == 0
    out = {"frozen": frozen,
           "version": datetime.now().strftime("%Y%m%d-%H%M"),
           "note": "needs_ruling=true 的条目等待人工裁决；frozen=true 后 96 ⑥ 逐条硬核验",
           "terms": terms}
    (BASE / "work" / "glossary.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"术语 {len(terms)} 条：自动采纳 {n_auto}，待裁决 {n_ruling}，"
          f"frozen={'true' if frozen else 'false'}"
          + ("（--freeze 被拒：仍有 needs_ruling 条目）" if args.freeze and not frozen else ""))
    if n_ruling:
        print("待裁决条目（zh 留空）：")
        for t in terms:
            if t["needs_ruling"]:
                print(f"  - {t['en']}  ×{t['count']}")


if __name__ == "__main__":
    main()
