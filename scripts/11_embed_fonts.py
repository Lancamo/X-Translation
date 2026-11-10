#!/usr/bin/env python3
"""
实例化静态字体。

1) Noto Serif SC（中文，OFL）—— 并做 cmap 去歧义
2) Roboto Serif（拉丁，OFL，与原版正文同源）—— 正文/数字/专有名词
"""
import collections
import os
import sys
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from pathlib import Path
BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()

FONTS = BASE / "work/fonts"

# 管线真正用的是 8 个静态字体；可变字体只是用来生成它们的。
# 三个 VF 相互独立：只下到其中一部分时，能实例化的先做，剩下的沿用已有静态字体，
# 不要把已经拿到的也丢掉。
_STATIC = ["NotoSerifSC-Regular.ttf", "NotoSerifSC-Medium.ttf",
           "NotoSerifSC-Bold.ttf", "NotoSerifSC-Black.ttf",
           "RobotoSerif-Regular.ttf", "RobotoSerif-Medium.ttf",
           "RobotoSerif-Bold.ttf", "RobotoSerif-Italic.ttf"]
_VF_ZH = FONTS / "NotoSerifSC-VF.ttf"
_VF_LAT = FONTS / "RobotoSerif-VF.ttf"
_VF_LAT_IT = FONTS / "RobotoSerif-Italic-VF.ttf"
_have_zh = _VF_ZH.exists()
_have_lat = _VF_LAT.exists() and _VF_LAT_IT.exists()

if not (_have_zh or _have_lat):
    if all((FONTS / n).exists() for n in _STATIC):
        note = FONTS / "FONT_SOURCE.json"
        src = "系统字体兜底，字形已换" if note.exists() else "已有静态字体"
        print(f"没有可变字体，但 8 个静态字体已就绪（{src}），跳过实例化。")
        sys.exit(0)
    sys.exit("字体不完整：没有可变字体，静态字体也没齐。"
             "先跑 scripts/09_fetch_fonts.py。")


def rank(cp: int):
    if 0x20 <= cp <= 0x7E:
        return (0, cp)
    if 0xF900 <= cp <= 0xFAFF:
        return (4, cp)
    if 0x2E80 <= cp <= 0x2FDF:
        return (4, cp)
    if 0x2F800 <= cp <= 0x2FA1F:
        return (4, cp)
    if 0xFE30 <= cp <= 0xFE4F:
        return (4, cp)
    if 0xFF00 <= cp <= 0xFFEF:
        return (3, cp)
    if 0x3000 <= cp <= 0x303F:
        return (2, cp)
    return (1, cp)


def deambiguate(font: TTFont) -> int:
    cmap = font.getBestCmap()
    rev = collections.defaultdict(list)
    for cp, g in cmap.items():
        rev[g].append(cp)
    drop = set()
    for g, cps in rev.items():
        if len(cps) < 2:
            continue
        win = min(cps, key=rank)
        drop |= {c for c in cps if c != win}
    if drop:
        for sub in font["cmap"].tables:
            for cp in list(sub.cmap):
                if cp in drop:
                    del sub.cmap[cp]
    return len(drop)


def make(src, axes, out, deamb=False):
    f = TTFont(src)
    instantiateVariableFont(f, axes, inplace=True, updateFontNames=True)
    n = deambiguate(f) if deamb else 0
    f.save(out)
    chk = TTFont(out)
    print(f"  {os.path.basename(out):<34} {os.path.getsize(out)//1024:>6} KB  "
          f"family={chk['name'].getDebugName(1)!r} glyf={'glyf' in chk}"
          + (f"  去歧义={n}" if deamb else ""))
    chk.close()


if _have_zh:
    print("Noto Serif SC (中文)")
    for name, w in [("Regular", 400), ("Medium", 500), ("Bold", 700), ("Black", 900)]:
        make(str(_VF_ZH), {"wght": w},
             f"{FONTS}/NotoSerifSC-{name}.ttf", deamb=True)
else:
    print("Noto Serif SC：无可变字体，沿用现有静态字体")

if _have_lat:
    print("\nRoboto Serif (拉丁)")
    for name, w in [("Regular", 400), ("Medium", 500), ("Bold", 700)]:
        make(str(_VF_LAT),
             {"wght": w, "wdth": 100, "opsz": 20, "GRAD": 0},
             f"{FONTS}/RobotoSerif-{name}.ttf")
    make(str(_VF_LAT_IT),
         {"wght": 400, "wdth": 100, "opsz": 20, "GRAD": 0},
         f"{FONTS}/RobotoSerif-Italic.ttf")
else:
    print("\nRoboto Serif：可变字体不全，沿用现有静态字体")

still_missing = [n for n in _STATIC if not (FONTS / n).exists()]
if still_missing:
    sys.exit(f"静态字体仍缺 {', '.join(still_missing)}：先跑 scripts/09_fetch_fonts.py。")

# 两组都已换回原字体，系统兜底的来源记录可以清掉
if _have_zh and _have_lat:
    (FONTS / "FONT_SOURCE.json").unlink(missing_ok=True)
