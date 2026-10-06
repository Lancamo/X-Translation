#!/usr/bin/env python3
"""63_audit_structure.py — T0 结构断言：源 ↔ 成品的硬性一致性检查。

为什么单独做一层：这类问题一旦发生就是灾难（整章丢失 / 页面尺寸错乱 /
换台机器打开是豆腐块），概率低但后果不可挽回。所以做成**门禁**——
任一项 FAIL 即退出码非 0，不得交付。

检查项：
  1 页数一致
  2 逐页尺寸（宽 × 高）一致 —— 必须逐页。只看第一页会漏掉夹在中间的异形页
  3 逐页旋转角一致
  4 书签（目录）对应 —— 源侧有书签才检；逐条比对层级与指向页码，并要求成品书签已译
  5 章节标题完整性 —— 源侧没有书签时的替代口径：大字号标题必须都有非空译文
  6 字体全部嵌入 —— 有一处没嵌入，换台机器打开就变豆腐块
  7 文件完好可打开

源侧本来就没有的东西自动跳过（扫描件没有书签，不该判 FAIL）。

用法：
  python 63_audit_structure.py --pdf output/xxx_zh-CN.pdf
  python 63_audit_structure.py --pdf output/xxx.pdf --title-size 14 --tol 0.5
输出：控制台 PASS/FAIL 表 + work/verify/structure.json，FAIL>0 时退出码 1
"""
import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf
import os

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()
CJK = re.compile(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]")
# 纯数字/符号不参与核验：目录页的 "01"…"08"、页码、纯刻度串——
# 它们没有可翻译的内容，译与不译渲染结果完全一样，报出来只会制造假警报。
NUMERIC = re.compile(r"^[\s0-9$%¢€£¥.,+\-–—/×x:*()\[\]<>|~='\"#&°]+$")


def find_source(base):
    """源 PDF：优先 XTRANS_SRC，其次根目录下唯一的 *.pdf，最后 input.pdf。"""
    if os.environ.get("XTRANS_SRC"):
        return Path(os.environ["XTRANS_SRC"])
    cands = sorted(p for p in base.glob("*.pdf") if p.is_file())
    return cands[0] if len(cands) == 1 else base / "input.pdf"


def rel(p):
    """相对 BASE 的显示路径；在 BASE 外就原样显示。"""
    try:
        return Path(p).relative_to(BASE)
    except ValueError:
        return Path(p)


def dwidth(s):
    """终端显示宽度：CJK 全角算 2，其余算 1。用它对齐表格，否则中文列会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def padto(s, w):
    return s + " " * max(0, w - dwidth(s))


def embedded_fonts(doc):
    """返回 (已嵌入数, 未嵌入字体名集合)。用 extract_font 判——没嵌入就取不到字节。"""
    seen, missing = set(), set()
    for pno in range(doc.page_count):
        for f in doc.get_page_fonts(pno):
            xref, basefont = f[0], f[3]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                _name, _ext, _sub, buf = doc.extract_font(xref)
            except Exception:
                missing.add(basefont or f"xref{xref}")
                continue
            if not buf:
                missing.add(basefont or f"xref{xref}")
    return len(seen), missing


def check_toc(src, dst):
    """书签（目录）对应。源侧没有书签 -> 跳过。"""
    st, dt = src.get_toc(), dst.get_toc()
    if not st:
        return {"status": "SKIP", "detail": "源 PDF 无书签（改用「章节标题完整性」口径）"}
    if len(dt) != len(st):
        return {"status": "FAIL", "detail": f"条目数 {len(st)} -> {len(dt)}"}
    bad_page, bad_lvl, untranslated = [], [], []
    for i, (a, b) in enumerate(zip(st, dt)):
        if a[0] != b[0]:
            bad_lvl.append(i)
        if a[2] != b[2]:
            bad_page.append(f"#{i} {a[1][:20]!r} p{a[2]}->p{b[2]}")
        if not CJK.search(b[1]):
            untranslated.append(f"#{i} {b[1][:30]!r}")
    if bad_lvl or bad_page:
        return {"status": "FAIL",
                "detail": f"层级不符 {len(bad_lvl)} 条；页码不符 {len(bad_page)} 条"
                          + (" 例：" + bad_page[0] if bad_page else "")}
    if untranslated:
        return {"status": "FAIL", "detail": f"{len(untranslated)} 条书签未译 例：{untranslated[0]}"}
    return {"status": "PASS", "detail": f"{len(st)} 条书签层级/页码/译文全部对应"}


def check_titles(base, title_size):
    """章节标题完整性：源侧大字号单元必须都有非空译文。

    源 PDF 常拿不到书签（很多研报根本没写 outline），这条是「章节是否对应」的替代口径：
    章节标题往往是页面上字号最大的一类文字，漏掉它一定看得见。
    """
    up, tp = base / "work" / "units.json", base / "work" / "translations.json"
    if not up.exists():
        return {"status": "SKIP", "detail": f"无 {rel(up)}（未走到 L1 抽取）"}, []
    units = json.loads(up.read_text(encoding="utf-8"))
    tr = json.loads(tp.read_text(encoding="utf-8")) if tp.exists() else {}
    sizes = sorted(u["size"] for u in units if u.get("size"))
    thr = title_size
    if thr is None:
        med = sizes[len(sizes) // 2] if sizes else 10.0
        thr = max(12.0, round(med * 1.4, 1))
    cands = [u for u in units if u.get("size", 0) >= thr and len(u["text"].strip()) >= 2]
    skipped = [u for u in cands if NUMERIC.match(u["text"].strip()) or u.get("passthrough")]
    cands = [u for u in cands if u not in skipped]
    missing = []
    for u in cands:
        t = (tr.get(u["id"]) or "").strip()
        if not t:
            missing.append({"id": u["id"], "page": u["page"], "size": u["size"],
                            "text": u["text"][:60]})
    tag = f"阈值 {thr}pt，{len(cands)} 条标题（另跳过 {len(skipped)} 条纯数字/原样保留）"
    if not cands:
        return {"status": "SKIP", "detail": f"无 ≥{thr}pt 的可译标题单元"}, []
    if missing:
        return {"status": "FAIL", "detail": f"{tag}，其中 {len(missing)} 条无译文"}, missing
    return {"status": "PASS", "detail": f"{tag}，全部有译文"}, []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", default="output/product.pdf", help="成品 PDF")
    ap.add_argument("--src", default="", help="源 PDF（默认取 XTRANS_SRC 或 input.pdf）")
    ap.add_argument("--src-pdf", dest="src_pdf", default="", help=argparse.SUPPRESS)
    ap.add_argument("--title-size", type=float, default=0,
                    help="章节标题字号阈值；0 = 按全篇字号中位数 ×1.4 自适应")
    ap.add_argument("--tol", type=float, default=0.5, help="页面尺寸容差 (pt)")
    ap.add_argument("--out", default="work/verify/structure.json")
    args = ap.parse_args()

    src_path = Path(args.src or args.src_pdf) if (args.src or args.src_pdf) else find_source(BASE)
    if not src_path.is_absolute():
        src_path = BASE / src_path
    pdf_path = Path(args.pdf)
    if not pdf_path.is_absolute():
        pdf_path = BASE / pdf_path
    if not src_path.exists():
        print(f"[warn] 源 PDF 不存在：{src_path}；跳过需要源侧的比对项")

    checks, titles, notes = [], [], []

    # 7 文件完好可打开
    ok_open, open_detail = True, ""
    try:
        if pdf_path.stat().st_size == 0:
            ok_open, open_detail = False, "文件为 0 字节"
        dst = pymupdf.open(pdf_path)
        if not dst.is_pdf:
            ok_open, open_detail = False, "不是合法 PDF"
        elif dst.is_repaired:
            open_detail = "打开时被修复过（源文件可能已损坏）"
    except Exception as e:
        ok_open, open_detail = False, f"打不开：{e}"

    if not ok_open:
        checks.append({"id": "open", "name": "文件完好可打开", "status": "FAIL",
                       "detail": open_detail})
        report(checks, titles, notes, args.out, pdf_path, src_path)
        sys.exit(1)
    checks.append({"id": "open", "name": "文件完好可打开", "status": "PASS",
                   "detail": open_detail or f"{pdf_path.stat().st_size/1e6:.2f} MB，可正常解析"})

    src = None
    if src_path.exists():
        try:
            src = pymupdf.open(src_path)
        except Exception as e:
            notes.append(f"源 PDF 打不开：{e}")

    # 1 页数
    if src:
        n_ok = src.page_count == dst.page_count
        checks.append({"id": "pages", "name": "页数一致", "status": "PASS" if n_ok else "FAIL",
                       "detail": f"{src.page_count} ↔ {dst.page_count}"})
    else:
        checks.append({"id": "pages", "name": "页数一致", "status": "SKIP",
                       "detail": f"无源 PDF；成品 {dst.page_count} 页"})

    # 2/3 逐页尺寸与旋转
    if src:
        if src.page_count != dst.page_count:
            checks.append({"id": "size", "name": "逐页尺寸一致", "status": "SKIP",
                           "detail": "页数不同，逐页比对无意义"})
            checks.append({"id": "rotation", "name": "逐页旋转角一致", "status": "SKIP",
                           "detail": "页数不同"})
        else:
            bad = []
            rot = []
            for i in range(src.page_count):
                a, b = src[i].rect, dst[i].rect
                if abs(a.width - b.width) > args.tol or abs(a.height - b.height) > args.tol:
                    bad.append(f"p{i+1} {a.width:.1f}×{a.height:.1f} -> {b.width:.1f}×{b.height:.1f}")
                if src[i].rotation != dst[i].rotation:
                    rot.append(f"p{i+1} {src[i].rotation}° -> {dst[i].rotation}°")
            checks.append({"id": "size", "name": "逐页尺寸一致",
                           "status": "FAIL" if bad else "PASS",
                           "detail": (f"{len(bad)} 页不符 例：{bad[0]}" if bad
                                      else f"全部 {src.page_count} 页一致（容差 {args.tol}pt）")})
            checks.append({"id": "rotation", "name": "逐页旋转角一致",
                           "status": "FAIL" if rot else "PASS",
                           "detail": (f"{len(rot)} 页不符 例：{rot[0]}" if rot
                                      else "全部一致")})
        # 4 书签
        c = check_toc(src, dst)
        checks.append({"id": "toc", "name": "书签（目录）对应", **c})
    else:
        for cid, nm in (("size", "逐页尺寸一致"), ("rotation", "逐页旋转角一致"),
                        ("toc", "书签（目录）对应")):
            checks.append({"id": cid, "name": nm, "status": "SKIP", "detail": "无源 PDF"})

    # 5 章节标题完整性
    c, titles = check_titles(BASE, args.title_size or None)
    checks.append({"id": "titles", "name": "章节标题完整性", **c})

    # 6 字体嵌入
    try:
        total, missing = embedded_fonts(dst)
        checks.append({"id": "fonts", "name": "字体全部嵌入",
                       "status": "FAIL" if missing else "PASS",
                       "detail": (f"{len(missing)} 个未嵌入：" + "、".join(sorted(missing)[:4])
                                  if missing else f"{total} 个字体全部嵌入")})
    except Exception as e:
        checks.append({"id": "fonts", "name": "字体全部嵌入", "status": "FAIL",
                       "detail": f"字体检查失败：{e}"})

    nfail = sum(1 for c in checks if c["status"] == "FAIL")
    report(checks, titles, notes, args.out, pdf_path, src_path)
    sys.exit(1 if nfail else 0)


def report(checks, titles, notes, out, pdf_path, src_path):
    w = max(dwidth(c["name"]) for c in checks) + 2
    print(f"成品 : {rel(pdf_path)}")
    print(f"源   : {rel(src_path) if src_path.exists() else '(缺)'}\n")
    for c in checks:
        print(f"  [{c['status']}] {padto(c['name'], w)}  {c['detail']}")
    if titles:
        print("\n  未译标题：")
        for t in titles[:30]:
            print(f"    p{t['page']+1:<3} {t['size']:>4.1f}pt  {t['text']!r}")
    for n in notes:
        print(f"  [note] {n}")
    cnt = {k: sum(1 for c in checks if c["status"] == k) for k in ("PASS", "FAIL", "SKIP")}
    verdict = "禁止交付" if cnt["FAIL"] else "通过"
    print(f"\nT0 结构断言：PASS {cnt['PASS']} / FAIL {cnt['FAIL']} / SKIP {cnt['SKIP']} -> {verdict}")

    out = Path(out)
    if not out.is_absolute():
        out = BASE / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"pdf": str(rel(pdf_path)), "src": str(rel(src_path)),
                               "checks": checks, "untranslated_titles": titles,
                               "counts": cnt, "verdict": verdict, "notes": notes},
                              ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
