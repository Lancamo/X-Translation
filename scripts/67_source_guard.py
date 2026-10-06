#!/usr/bin/env python3
"""67_source_guard.py — 源文件完整性守卫（绝对红线的执行机制）。

红线只有一句：

    整个过程不可以损坏原文件，不要对原文件进行任何的改动。

但"我觉得我没动它"和"它确实没被动过"是两件事。一句口号拦不住一次手滑——
最常见的事故是把某个脚本的 `--out` 顺手写成了源文件名，而每一步看起来都
"正常跑完了"。所以红线必须有可执行、可复核的机制，这个脚本就是那句红线的
牙齿：开工前记账，收工后复核，账不平就退出码非 0。

  --record   开工前记账：算源文件 SHA-256，连同大小/修改时间/权限位存成基线
             （work/verify/source_integrity.json）。**应在第一步跑，早于侦察。**
  --check    收工后复核：重算一遍逐项比对，任何一项不同 → 退出码 1
  --seal     给源文件加只读位（chmod 444），物理上堵住误写
  --unseal   去掉只读位，恢复记账时记录的权限（确认要替换源文件时才用）

为什么用 SHA-256 而不是"看一眼/看大小/看时间"：
  PDF 的误写常常只多出几百字节（见下），目视、比大小、比时间戳都可能漏。哈希不会漏。

真正危险的动作只有一个，实测（PyMuPDF 1.28.2，逐个真跑过）：

| 写法 | 结果 |
|---|---|
| `doc.save(源路径)` | **被 PyMuPDF 自己拒绝**：`ValueError: save to original must be incremental` |
| `doc.save(源路径, incremental=True)` | 被拒绝（本版报 encryption 变更，不允许增量保存） |
| **`doc.saveIncr()`** | **★ 静默就地改写原文件**——实测 3,603,6029 → 3,603,7211 字节，哈希变了 |
| 只读位（`chmod 444`）下再跑 `saveIncr()` | 被系统拒绝：`Permission denied` |
| 只读位下正常读取 | 完全不受影响（页数、抽文字都对） |

所以最蠢的写法库已经替我们拦住了，**唯一要防的是 `saveIncr()`**。这类改写只
追加几百字节、文件结构仍合法、能正常打开看起来一切正常——只有哈希知道。

为什么基线已存在且哈希不同时默认拒绝而不是自动重建：
  万一源文件真的被改过，最危险的动作就是"顺手重建基线"——那样红线就被悄悄
  抹平了。所以这里默认拦住，必须显式 --force，并在输出里留下"基线被重建"的
  记录，让这件事在交接文档里留痕。

用法：
  python3 67_source_guard.py --record            # 开工第一步
  python3 67_source_guard.py --check             # 收工最后一步
  python3 67_source_guard.py --record --seal     # 记账顺手加只读位
  python3 67_source_guard.py --unseal            # 要换源文件时先解锁
  python3 67_source_guard.py --check --files a.pdf b.pdf   # 额外素材一并守
输出：work/verify/source_integrity.json；--check 不通过时退出码 1。
"""
import argparse
import hashlib
import json
import os
import stat
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
VERIFY = BASE / "work" / "verify"
BASELINE = VERIFY / "source_integrity.json"
SEAL_MODE = 0o444
DEFAULT_MODE = 0o644


def dwidth(s):
    """终端显示宽度：CJK 全角算 2，其余算 1。中文列不对齐就是这里没做。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def padto(s, w):
    return s + " " * max(0, w - dwidth(s))


def rel(p):
    """相对 BASE 的显示路径；在 BASE 外就原样显示。"""
    try:
        return Path(p).resolve().relative_to(BASE)
    except ValueError:
        return Path(p)


def find_sources(extra):
    """要守护的文件：显式 --files > $XTRANS_SRC > 根目录下唯一的 *.pdf > input.pdf。

    与 63_audit_structure.py 的口径保持一致——同一个"源"，不能两处各认一个。
    """
    if extra:
        return [Path(p) for p in extra]
    if os.environ.get("XTRANS_SRC"):
        return [Path(os.environ["XTRANS_SRC"])]
    cands = sorted(p for p in BASE.glob("*.pdf") if p.is_file())
    return [cands[0]] if len(cands) == 1 else [BASE / "input.pdf"]


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ts(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


def fingerprint(path):
    """单文件指纹。分块读，别把 38MB 的 PDF 一口气吃进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    st = path.stat()
    return {"path": str(rel(path)), "size": st.st_size, "mtime": ts(st.st_mtime),
            "mode": oct(stat.S_IMODE(st.st_mode)), "sha256": h.hexdigest()}


def load_baseline():
    if not BASELINE.exists():
        return None
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"✗ 基线文件读不出来（{BASELINE}）：{e}")
        sys.exit(1)


def do_record(paths, force, seal):
    prev = {r["path"]: r for r in (load_baseline() or {}).get("files", [])}
    rows, moved = [], []
    for p in paths:
        if not p.exists():
            print(f"✗ 源文件不存在：{p}")
            return 1
        r = fingerprint(p)
        r["recorded_at"] = now_iso()
        # 权限位继承：已经 seal 过就别把 444 记成"原始权限"，否则 unseal 恢复错
        old = prev.get(r["path"])
        if old and r["mode"] == oct(SEAL_MODE):
            r["mode"] = old.get("mode", oct(DEFAULT_MODE))
        if old and old["sha256"] != r["sha256"]:
            moved.append((r, old))
        rows.append(r)

    if moved and not force:
        print("✗ 源文件与已有基线不一致，已拒绝重建基线：")
        for r, old in moved:
            print(f"    {r['path']}")
            print(f"      基线 {old['sha256'][:16]}…  {old['size']} 字节  {old['mtime']}")
            print(f"      当前 {r['sha256'][:16]}…  {r['size']} 字节  {r['mtime']}")
        print("\n  红线要求原文件不得改动。先查清是谁改的、能否恢复，不要急着往下走。")
        print("  确属用户主动替换源文件（拿到新版报告），再用 --force 重建基线，")
        print("  并把这个动作写进交接文档——**基线被重建这件事必须留痕**。")
        return 1

    if moved:
        print("⚠ 基线被显式重建（--force）：")
        for r, old in moved:
            print(f"    {r['path']}  {old['sha256'][:16]}… → {r['sha256'][:16]}…")

    VERIFY.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps({"recorded_at": now_iso(), "files": rows},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"✓ 已记账 {len(rows)} 个源文件 → {rel(BASELINE)}")
    for r in rows:
        print(f"    {padto(r['path'], 34)} {r['size']:>12,} 字节  "
              f"{r['mode']}  sha256 {r['sha256'][:16]}…")
    if seal:
        return do_seal(rows, unlock=False)
    return 0


def do_check(paths):
    """复核。注意 paths 只用于"基线里没有但用户点名要查"的补充，正常不传。"""
    base = load_baseline()
    if base is None:
        print("✗ 没有基线：先跑 --record。它应当在本流程的**第一步**执行，"
              "早于侦察——等到收工才记账，中间发生过什么就无从判断了。")
        return 1
    rows, bad, warn = [], 0, 0
    for r in base["files"]:
        p = BASE / r["path"]
        if not p.exists():
            rows.append((r["path"], "文件不存在", f"记账时 {r['size']} 字节", "FAIL"))
            bad += 1
            continue
        cur = fingerprint(p)
        # 判定分三级：**内容有没有变**才是红线，时间戳/权限位只作提示。
        # 把 mtime 不同也判 FAIL 会误伤——备份、同步盘、iCloud 都会碰时间戳，
        # 假警报多了红线就没人看了。而 saveIncr() 那类误写一定会改到内容。
        hard = [k for k in ("sha256", "size") if cur[k] != r[k]]
        soft = [k for k in ("mtime",) if cur[k] != r[k]]
        # 权限位只写进说明、不参与判定：加只读位是我们**自己**的防护动作，
        # 把它算成异常，每次 --seal 之后复核都会冒出假警告。
        extra = f"；权限位 {r['mode']} → {cur['mode']}" if cur["mode"] != r["mode"] else ""
        if hard:
            detail = "；".join(f"{k}: {r[k]} → {cur[k]}" if k != "sha256"
                               else f"sha256: {r[k][:16]}… → {cur[k][:16]}…" for k in hard) + extra
            rows.append((r["path"], "内容已变", detail, "FAIL"))
            bad += 1
        elif soft:
            detail = "；".join(f"{k}: {r[k]} → {cur[k]}" for k in soft) + extra
            rows.append((r["path"], "内容未变", detail, "WARN"))
            warn += 1
        else:
            rows.append((r["path"], "未变", f"sha256 {cur['sha256'][:16]}…{extra}", "PASS"))
    if paths:
        known = {r["path"] for r in base["files"]}
        for p in paths:
            if str(rel(p)) not in known:
                rows.append((str(rel(p)), "不在基线内", "记账时未包含此文件", "FAIL"))
                bad += 1

    w = [max(dwidth(x[i]) for x in rows + [("文件", "结论", "说明", "判定")]) for i in range(3)]
    print(f"{padto('文件', w[0])}  {padto('结论', w[1])}  {padto('说明', w[2])}  判定")
    print("-" * (sum(w) + 8))
    for r in rows:
        print(f"{padto(r[0], w[0])}  {padto(r[1], w[1])}  {padto(r[2], w[2])}  {r[3]}")
    print()
    # 落一份结论给 69_qc_summary.py 读。为什么不让汇总脚本自己算哈希：
    # 那一层是"只读、不判定"的定位，判定统一留在这个脚本里，口径只有一份。
    VERIFY.mkdir(parents=True, exist_ok=True)
    (VERIFY / "source_check.json").write_text(json.dumps(
        {"checked_at": now_iso(), "recorded_at": base.get("recorded_at"),
         "status": "fail" if bad else "pass", "fail": bad, "warn": warn,
         "files": [{"path": r[0], "verdict": r[1], "detail": r[2], "result": r[3]} for r in rows]},
        ensure_ascii=False, indent=1), encoding="utf-8")
    if bad:
        print(f"✗ 源文件完整性：FAIL {bad} 项——**停止全流程并上报**。")
        print("  不要试图'补救性覆盖'：先确认能否从备份/原始渠道恢复，恢复后用 --force 重建基线。")
        return 1
    if warn:
        print(f"✓ 源文件完整性：内容 PASS（{len(rows)} 个文件，哈希与记账时完全一致）")
        print(f"  ⚠ 另有 {warn} 项提示：修改时间被动过，内容没变。通常无害，"
              f"但既然有人碰过这个文件，顺手看一眼为什么。")
    else:
        print(f"✓ 源文件完整性：PASS（{len(rows)} 个文件，哈希与记账时完全一致）")
    print(f"  记账时间 {base.get('recorded_at')}，复核时间 {now_iso()}")
    return 0


def set_uchg(p, on):
    """macOS/BSD 的 uchg 位。返回 (成功?"True/None/False", 说明)。

    为什么除了 chmod 还要它：实测 chmod 444 只能挡住"改内容"（`saveIncr()` 被
    EACCES 拒掉），**改名、移动、删除照样能做**。而这条红线说的是"不要对原文件
    进行任何的改动"，所以再加一道 uchg —— 实测连 `mv` 都是 EPERM。

    只动 UF_IMMUTABLE 这一个位，不用 `chflags(p, 0)` 整体清零，免得顺手把用户
    自己设的 hidden 之类的标志抹掉。非 macOS/BSD 没有 os.chflags，跳过（保留
    chmod 那道栅栏）。
    """
    if not hasattr(os, "chflags"):
        return None, "非 macOS/BSD，跳过"
    st = os.stat(p)
    flag = getattr(os, "UF_IMMUTABLE", 0x00000002)
    flags = (st.st_flags | flag) if on else (st.st_flags & ~flag)
    if flags == st.st_flags:
        return True, ""
    try:
        os.chflags(p, flags)
        return True, ""
    except OSError as e:
        return False, str(e)


def do_seal(records, unlock):
    """加/去只读位。这是"物理层"的栅栏，防的是手滑与误跑，不是防恶意。"""
    rc = 0
    for r in records:
        p = BASE / r["path"] if not Path(r["path"]).is_absolute() else Path(r["path"])
        if not p.exists():
            print(f"✗ 不存在：{p}")
            rc = 1
            continue
        try:
            # 顺序不能反：uchg 在位时 os.chmod 会 EPERM，所以每一步都先解 uchg。
            # 这个顺序也让 seal 变成幂等的——重复 --seal 不会报错。
            set_uchg(p, False)
            if unlock:
                target = int(r.get("mode", oct(DEFAULT_MODE)), 8)
                os.chmod(p, target)
                print(f"✓ 已解锁 {rel(p)}  ({oct(target)}，uchg 已清)")
            else:
                os.chmod(p, SEAL_MODE)
                ok, why = set_uchg(p, True)
                if ok:
                    tip = "，另加 uchg（连改名/删除也挡）"
                elif ok is None:
                    tip = ""
                else:
                    tip = f"（uchg 未加：{why}）"
                print(f"✓ 已加只读位 {rel(p)}  ({oct(SEAL_MODE)}{tip})")
        except OSError as e:
            print(f"✗ 改权限失败 {rel(p)}：{e}")
            rc = 1
    if not unlock:
        print("  提示：PyMuPDF 以只读方式打开文件，加锁不影响任何读取操作；")
        print("       要替换源文件时先 --unseal。")
    return rc


def main():
    ap = argparse.ArgumentParser(description="源文件完整性守卫（红线执行机制）")
    ap.add_argument("--record", action="store_true", help="开工前记账，写基线")
    ap.add_argument("--check", action="store_true", help="收工后复核，不符则退出码 1")
    ap.add_argument("--seal", action="store_true", help="给源文件加只读位")
    ap.add_argument("--unseal", action="store_true", help="去掉只读位，恢复原权限")
    ap.add_argument("--force", action="store_true", help="基线已存在且哈希不同时强制重建（须留痕）")
    ap.add_argument("--files", nargs="*", default=[], help="额外要守护的素材文件")
    args = ap.parse_args()

    # --seal 允许与 --record 叠加（记账时顺手加只读位，是最常见的一次做完）；
    # record / check / unseal 三者互斥。
    core = [n for n, v in (("record", args.record), ("check", args.check),
                           ("unseal", args.unseal)) if v]
    if len(core) != 1 and not (args.seal and not core):
        ap.error("必须且只能选一个主动作：--record / --check / --unseal"
                 "（--seal 可单独用，也可与 --record 叠加）")

    paths = find_sources(args.files)
    want = args.files or [str(rel(p)) for p in paths]

    if args.record:
        return do_record(paths, args.force, args.seal)
    if args.check:
        return do_check(paths)
    # seal / unseal：以基线里的权限位为准；没有基线就用默认值
    recs = [r for r in (load_baseline() or {}).get("files", []) if str(rel(BASE / r["path"])) in want]
    if not recs:
        recs = [{"path": str(rel(p)), "mode": oct(DEFAULT_MODE)} for p in paths]
    return do_seal(recs, unlock=args.unseal)


if __name__ == "__main__":
    sys.exit(main())
