#!/usr/bin/env python3
"""19_manifest.py — 写 run_manifest.json：产物回答「我从哪来」（SYNTHESIS P0-6）。

W3C PROV 三要素的最小落地：
  Entity   = inputs / outputs（各带 sha256+size）
  Activity = 每次构建（run_id + 命令 + 脚本版本=脚本哈希 + git commit）
  Agent    = 脚本本身（scripts/ 的哈希即其版本）

用法（构建脚本收尾时调）：
  python3 19_manifest.py --run-id v15 \
      --inputs input.pdf work/translations.json \
      --outputs work/stage/V15_final.pdf \
      --scripts 24_rebuild_text.py 40_apply_overlay.py \
      --command "bash work/build_v1.4.sh work/stage/V15_final.pdf"
（--scripts 传 scripts/ 下的文件名即可，不必带目录前缀；相对路径按各自基准解析）

输出：work/manifests/<run_id>/run_manifest.json + 追加一行到 manifests/history.jsonl
（append-only；大 PDF 不进 Git，小 JSON 进 Git）。不记录敏感原值。
"""
import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

BASE = Path(os.environ.get("XTRANS_BASE", Path.cwd())).resolve()


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ent(p, subdir: str = "") -> dict:
    """subdir：相对路径的解析基准。

    脚本一律放在 `scripts/` 下，所以 `--scripts` 传裸文件名即可——
    不设这个基准的话，`24_rebuild_text.py` 会被拼成 `<BASE>/24_rebuild_text.py`，
    每一项都记成 missing，脚本哈希（即脚本版本）就全丢了。
    """
    q = Path(p)
    if not q.is_absolute():
        q = (BASE / subdir / q) if subdir else (BASE / q)
    if not q.exists():
        return {"path": str(q), "missing": True}
    return {"path": str(q.relative_to(BASE)), "sha256": sha256(q), "size": q.stat().st_size}


def git_commit():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=BASE,
                              capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--inputs", nargs="*", default=[])
    ap.add_argument("--outputs", nargs="*", default=[])
    ap.add_argument("--scripts", nargs="*", default=[])
    ap.add_argument("--command", default="")
    ap.add_argument("--status", default="done")
    a = ap.parse_args()

    m = {"run_id": a.run_id,
         "created_at": datetime.now().isoformat(timespec="seconds"),
         "status": a.status,
         "git_commit": git_commit(),
         "command": a.command,
         "inputs": [ent(x) for x in a.inputs],
         "outputs": [ent(x) for x in a.outputs],
         "scripts": [ent(x, "scripts") for x in a.scripts]}

    d = BASE / "work" / "manifests" / a.run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    with open(BASE / "work" / "manifests" / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
    n_in = sum(1 for x in m["inputs"] if not x.get("missing"))
    n_out = sum(1 for x in m["outputs"] if not x.get("missing"))
    n_scr = sum(1 for x in m["scripts"] if not x.get("missing"))
    print(f"manifest -> {d / 'run_manifest.json'}（inputs {n_in} / outputs {n_out} / "
          f"scripts {n_scr}，git {m['git_commit'] or '—'}）")

    # 找不到的项只记了路径、没有哈希——等于溯源记录不全，必须报出来
    gaps = [x["path"] for x in m["inputs"] + m["outputs"] + m["scripts"]
            if x.get("missing")]
    if gaps:
        print(f"[warn] {len(gaps)} 项没找到，这些项在溯源里没有哈希：")
        for g in gaps:
            print(f"   {g}")


if __name__ == "__main__":
    main()
