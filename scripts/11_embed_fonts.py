#!/usr/bin/env python3
"""
实例化静态字体。

1) Noto Serif SC（中文，OFL）—— 并做 cmap 去歧义
2) Roboto Serif（拉丁，OFL，与原版正文同源）—— 正文/数字/专有名词
"""
import collections
import os
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont
from pathlib import Path
BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()

FONTS = BASE / "work/fonts"


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


print("Noto Serif SC (中文)")
for name, w in [("Regular", 400), ("Medium", 500), ("Bold", 700), ("Black", 900)]:
    make(f"{FONTS}/NotoSerifSC-VF.ttf", {"wght": w},
         f"{FONTS}/NotoSerifSC-{name}.ttf", deamb=True)

print("\nRoboto Serif (拉丁)")
for name, w in [("Regular", 400), ("Medium", 500), ("Bold", 700)]:
    make(f"{FONTS}/RobotoSerif-VF.ttf",
         {"wght": w, "wdth": 100, "opsz": 20, "GRAD": 0},
         f"{FONTS}/RobotoSerif-{name}.ttf")
make(f"{FONTS}/RobotoSerif-Italic-VF.ttf",
     {"wght": 400, "wdth": 100, "opsz": 20, "GRAD": 0},
     f"{FONTS}/RobotoSerif-Italic.ttf")
