#!/usr/bin/env python3
"""
合并译文：正文译文 + 页脚统一译文，并校验覆盖完整性。
输出 work/translations.json
"""
import json
import os
from pathlib import Path
BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()

WORK = BASE / "work"
FOOTER_SRC = "© 2026 Andreessen Horowitz. All rights reserved worldwide."
FOOTER_ZH = "© 2026 安德森·霍洛维茨。全球版权所有。"

units = json.load(open(f"{WORK}/units.json", encoding="utf-8"))
tr = json.load(open(f"{WORK}/translations_main.json", encoding="utf-8"))

# 页脚统一处理
n_footer = 0
for u in units:
    if u["passthrough"]:
        continue
    if u["text"].strip() == FOOTER_SRC:
        tr[u["id"]] = FOOTER_ZH
        n_footer += 1

need = {u["id"]: u for u in units if not u["passthrough"]}
missing = [k for k in need if k not in tr]
extra = [k for k in tr if k not in need]

print(f"待译单元: {len(need)}   已译: {len(need) - len(missing)}   页脚自动填充: {n_footer}")
if missing:
    print(f"\n!! 缺失 {len(missing)} 条：")
    for k in missing:
        print(f"   {k}  {need[k]['text'][:80]!r}")
if extra:
    print(f"\n!! 多余 {len(extra)} 条（units 中不存在）：{extra}")

if not missing and not extra:
    json.dump(tr, open(f"{WORK}/translations.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\nOK -> work/translations.json")
