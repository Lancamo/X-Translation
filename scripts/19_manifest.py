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


def ent(p) -> dict:
    p = Path(p) if Path(p).is_absolute() else BASE / p
    if not p.exists():
        return {"path": str(p), "missing": True}
    return {"path": str(p.relative_to(BASE)), "sha256": sha256(p), "size": p.stat().st_size}


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
         "scripts": [ent(x) for x in a.scripts]}

    d = BASE / "work" / "manifests" / a.run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "run_manifest.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    with open(BASE / "work" / "manifests" / "history.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
    n_in = sum(1 for x in m["inputs"] if not x.get("missing"))
    n_out = sum(1 for x in m["outputs"] if not x.get("missing"))
    print(f"manifest -> {d / 'run_manifest.json'}（inputs {n_in} / outputs {n_out} / "
          f"scripts {len(m['scripts'])}，git {m['git_commit'] or '—'}）")


if __name__ == "__main__":
    main()
