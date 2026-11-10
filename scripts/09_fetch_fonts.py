#!/usr/bin/env python3
"""09_fetch_fonts.py — 把字体弄到 work/fonts/：本地 → 缓存 → 下载 → 系统兜底。

为什么要有这一步：字体是回填的前提，但原先只有「从 GitHub 直接下载」一条路，
外网不稳或沙箱拦截时整条管线就卡住。本脚本按级降级，任何一级成功即停，
并且**明确打印用了哪一级**——降到系统字体时字形会变，必须让人看见。

降级链（按顺序，成功即停）：
  L0 work/fonts/ 已有静态字体          → 直接用
  L1 ~/.cache/x-translation/fonts/     → 复制（一次下载，跨项目复用）
  L2 下载可变字体（VF）                → 交给 11_embed_fonts.py 实例化
     源顺序：直连 GitHub → jsDelivr 镜像 → $XTRANS_FONT_MIRROR（可选，内网/自建镜像）
  L3 系统字体兜底                      → 从 macOS 自带字体提取/复制成静态 TTF
  L4 都没有                            → 明确报错，告诉用户把文件放哪

用法：
  python3 scripts/09_fetch_fonts.py            # 自动降级
  python3 scripts/09_fetch_fonts.py --offline  # 完全不联网，直接走本地/缓存/系统
  python3 scripts/09_fetch_fonts.py --status   # 只看当前字体来源，不做任何事

内网/自建镜像：设 XTRANS_FONT_MIRROR，脚本会尝试 <mirror>/<文件名>。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
FONTS = BASE / "work" / "fonts"
CACHE = Path.home() / ".cache" / "x-translation" / "fonts"
MANIFEST = FONTS / "FONT_SOURCE.json"

# 管线需要的静态字体（24_rebuild_text.py 里按这些名字取文件）
ZH_STATIC = ["NotoSerifSC-Regular.ttf", "NotoSerifSC-Medium.ttf",
             "NotoSerifSC-Bold.ttf", "NotoSerifSC-Black.ttf"]
LAT_STATIC = ["RobotoSerif-Regular.ttf", "RobotoSerif-Medium.ttf",
              "RobotoSerif-Bold.ttf", "RobotoSerif-Italic.ttf"]

# 可变字体源：直连在前，镜像在后；镜像失败会快速跳过，不会卡住
VF_SOURCES = {
    "NotoSerifSC-VF.ttf": [
        "https://raw.githubusercontent.com/notofonts/noto-cjk/main/Serif/Variable/TTF/Subset/NotoSerifSC-VF.ttf",
        "https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@main/Serif/Variable/TTF/Subset/NotoSerifSC-VF.ttf",
    ],
    "RobotoSerif-VF.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/robotoserif/RobotoSerif%5BGRAD%2Copsz%2Cwdth%2Cwght%5D.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/robotoserif/RobotoSerif%5BGRAD%2Copsz%2Cwdth%2Cwght%5D.ttf",
    ],
    "RobotoSerif-Italic-VF.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/robotoserif/RobotoSerif-Italic%5BGRAD%2Copsz%2Cwdth%2Cwght%5D.ttf",
        "https://cdn.jsdelivr.net/gh/google/fonts@main/ofl/robotoserif/RobotoSerif-Italic%5BGRAD%2Copsz%2Cwdth%2Cwght%5D.ttf",
    ],
}

# 系统兜底：macOS 自带字体 → 管线期望的文件名
SYSTEM_ZH_TTC = "/System/Library/Fonts/Supplemental/Songti.ttc"
SYSTEM_ZH_FACES = {
    "Regular": ("Songti SC", "Regular"),
    "Medium": ("Songti SC", "Regular"),    # 宋体无 Medium，用 Regular
    "Bold": ("Songti SC", "Bold"),
    # Songti SC Black 只有 8k 字形（另外三个字重是 33k），标题里遇到生僻字会缺字。
    # 兜底时字重略轻只是观感差一点，缺字是直接坏掉——所以 Black 退回 Bold。
    "Black": ("Songti SC", "Bold"),
}
SYSTEM_LAT_DIR = "/System/Library/Fonts/Supplemental"
SYSTEM_LAT_FILES = {
    "Regular": "Times New Roman.ttf",
    "Medium": "Times New Roman.ttf",       # Times 无 Medium，用 Regular
    "Bold": "Times New Roman Bold.ttf",
    "Italic": "Times New Roman Italic.ttf",
}


def valid_font(p: Path) -> bool:
    """不只是看文件在不在——下载可能拿到 HTML 错误页或截断文件。"""
    if not p.exists() or p.stat().st_size < 100_000:
        return False
    try:
        from fontTools.ttLib import TTFont
        f = TTFont(p, lazy=True)
        ok = "cmap" in f and len(f.getBestCmap()) > 100
        f.close()
        return ok
    except Exception:
        return False


def all_static_present() -> bool:
    return all(valid_font(FONTS / n) for n in ZH_STATIC + LAT_STATIC)


def static_present() -> list:
    return [n for n in ZH_STATIC + LAT_STATIC if not valid_font(FONTS / n)]


def try_download(url: str, dest: Path, timeout: int) -> bool:
    """单源单文件。失败不抛异常，交给下一级。

    先试断点续传；有些镜像不支持 Range（会返回 200 或 416），这时丢掉半截文件
    整包重下一次——否则一个坏掉的 .part 会让后续每次尝试都失败。
    """
    part = dest.with_suffix(dest.suffix + ".part")
    host = url.split("/")[2]
    # 只有本来就有半截文件时，续传失败才值得整包重下一次；
    # 否则（比如 404）重试只是白白多等一个连接超时。
    had_part = part.exists()
    attempts = (True, False) if had_part else (True,)
    for resume in attempts:
        cmd = ["curl", "-L", "--fail", "--silent", "--show-error",
               "--connect-timeout", "8", "--max-time", str(timeout),
               "-o", str(part), url]
        if resume:
            cmd[1:1] = ["-C", "-"]
        label = "续传" if (resume and had_part) else "下载"
        print(f"    尝试 {host}（{label}）… ", end="", flush=True)
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            print("没有 curl")
            return False
        if r.returncode != 0:
            print(f"失败（退出码 {r.returncode}）")
            # 传输中断（18）/超时（28）/接收失败（56）保留半截文件，下次能续传；
            # 其它错误（404、连不上）没有续传价值，直接清掉。
            if r.returncode not in (18, 28, 56):
                part.unlink(missing_ok=True)
            continue
        part.replace(dest)
        if valid_font(dest):
            print(f"OK（{dest.stat().st_size // 1024 // 1024} MB）")
            return True
        print("文件无效（可能是续传接错了），丢弃")
        dest.unlink(missing_ok=True)
    return False


def cache_dir():
    """缓存是加速手段，不是前提。写不进去（只读环境/沙箱）就退化成不缓存。"""
    try:
        CACHE.mkdir(parents=True, exist_ok=True)
        return CACHE
    except OSError:
        return None


def fetch_vf(offline: bool) -> list:
    """返回缺失的 VF 文件名列表。"""
    FONTS.mkdir(parents=True, exist_ok=True)
    cache = cache_dir()
    missing = []

    for name, urls in VF_SOURCES.items():
        dest = FONTS / name
        if valid_font(dest):
            print(f"  [本地] {name}")
            continue
        if cache and valid_font(cache / name):
            shutil.copyfile(cache / name, dest)
            print(f"  [缓存] {name}")
            continue
        if offline:
            missing.append(name)
            continue

        sources = list(urls)
        mirror = os.environ.get("XTRANS_FONT_MIRROR", "").rstrip("/")
        if mirror:
            sources.append(f"{mirror}/{name}")

        print(f"  [下载] {name}")
        ok = False
        for url in sources:
            if try_download(url, dest, timeout=600):
                if cache:
                    shutil.copyfile(dest, cache / name)
                ok = True
                break
        if not ok:
            missing.append(name)
    return missing


def system_fallback() -> bool:
    """给还缺的组补一套系统字体；已有可变字体的组留给 11 实例化。

    中文组 = NotoSerifSC-VF；拉丁组 = RobotoSerif-VF + RobotoSerif-Italic-VF。
    一组只要可变字体齐了就不动它，免得先写一遍系统字体、随后又被覆盖。
    """
    from fontTools.ttLib import TTCollection

    FONTS.mkdir(parents=True, exist_ok=True)
    zh_vf = (FONTS / "NotoSerifSC-VF.ttf").exists()
    lat_vf = ((FONTS / "RobotoSerif-VF.ttf").exists()
              and (FONTS / "RobotoSerif-Italic-VF.ttf").exists())

    picked, copied_lat = {}, {}

    if not zh_vf:
        if not Path(SYSTEM_ZH_TTC).exists():
            return False
        coll = TTCollection(SYSTEM_ZH_TTC)
        for weight, (family, style) in SYSTEM_ZH_FACES.items():
            out = FONTS / f"NotoSerifSC-{weight}.ttf"
            if valid_font(out):
                continue
            for f in coll.fonts:
                n = f["name"]
                if n.getDebugName(1) == family and n.getDebugName(2) == style:
                    f.save(out)
                    picked[weight] = f"{family} {style}"
                    break

    if not lat_vf:
        for weight, fn in SYSTEM_LAT_FILES.items():
            out = FONTS / f"RobotoSerif-{weight}.ttf"
            if valid_font(out):
                continue
            src = Path(SYSTEM_LAT_DIR) / fn
            if not src.exists():
                return False
            # 用 copyfile 而不是 copy2：copy2 会连权限位/扩展属性一起复制，
            # 在受限环境（沙箱、只读盘）里会因 chflags 失败而中断。
            shutil.copyfile(src, out)
            copied_lat[weight] = fn

    # 每组要么有可变字体（等 11 处理），要么静态字体已齐
    zh_ok = zh_vf or all(valid_font(FONTS / f"NotoSerifSC-{w}.ttf") for w in SYSTEM_ZH_FACES)
    lat_ok = lat_vf or all(valid_font(FONTS / f"RobotoSerif-{w}.ttf") for w in SYSTEM_LAT_FILES)
    if not (zh_ok and lat_ok):
        return False

    if picked or copied_lat:
        MANIFEST.write_text(json.dumps({
            "mode": "system-fallback",
            "note": "下列字重来自 macOS 自带字体，不是 Noto Serif SC / Roboto Serif；观感会有差异。",
            "zh": {"font": "Songti SC (宋体)", "faces": picked},
            "lat": {"font": "Times New Roman", "files": copied_lat},
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


def show_status() -> int:
    if MANIFEST.exists():
        print(MANIFEST.read_text(encoding="utf-8"))
    missing = static_present()
    if missing:
        print("静态字体缺失：", " ".join(missing))
        return 1
    print("8 个静态字体齐全。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="完全不联网")
    ap.add_argument("--status", action="store_true", help="只报告当前来源")
    a = ap.parse_args()

    if a.status:
        return show_status()

    try:
        import fontTools  # noqa: F401
    except ImportError:
        print("缺少 fontTools：pip install fonttools", file=sys.stderr)
        return 2

    print("字体获取（L0 本地 → L1 缓存 → L2 下载 → L3 系统兜底）")

    # 上次降级到系统字体的话，即使静态字体"齐全"也要先试着换回原字体，
    # 不要逼用户手动删文件。
    fallback = MANIFEST.exists()
    if all_static_present() and not fallback:
        print("  [L0] work/fonts/ 已齐全，跳过。")
        return 0
    if fallback:
        print("  [重试] 当前是系统字体兜底，先尝试换回原字体 …")

    missing_vf = fetch_vf(a.offline)
    if not missing_vf:
        print("\n可变字体已就绪 → 下一步：python3 scripts/11_embed_fonts.py")
        return 0

    if fallback and all_static_present():
        print("\n  仍拿不到可变字体，继续用已有的系统字体。")
        return 0

    print(f"\n[L3] 下载不完整（缺 {', '.join(missing_vf)}），改用系统字体兜底 …")
    if not system_fallback():
        print("\n[L4] 失败：既拿不到可变字体，本机也没有可用的系统字体。", file=sys.stderr)
        print("  手动解法：把以下文件放到 work/fonts/：", file=sys.stderr)
        for n in ZH_STATIC + LAT_STATIC:
            print(f"    {n}", file=sys.stderr)
        print("  或用 XTRANS_FONT_MIRROR=<内网镜像前缀> 重跑本脚本。", file=sys.stderr)
        return 2

    # 按组报告到底换了哪些，别让人以为全部字体都降级了
    zh_fell = not (FONTS / "NotoSerifSC-VF.ttf").exists()
    lat_fell = not ((FONTS / "RobotoSerif-VF.ttf").exists()
                    and (FONTS / "RobotoSerif-Italic-VF.ttf").exists())
    fell = []
    if zh_fell:
        fell.append("中文 → 宋体")
    if lat_fell:
        fell.append("拉丁 → Times New Roman")
    print(f"\n⚠️  已降级到系统字体：{'；'.join(fell)}。")
    if not zh_fell or not lat_fell:
        kept = "中文仍用 Noto Serif SC" if not zh_fell else "拉丁仍用 Roboto Serif"
        print(f"    （{kept}）")
    print("    流程可以继续，但降级部分的字形与原字体不同，交付前请确认差异是否可接受。")
    print("    来源记录：work/fonts/FONT_SOURCE.json")
    print("    网络恢复后重跑本脚本会自动尝试换回，不需要手动删文件。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
