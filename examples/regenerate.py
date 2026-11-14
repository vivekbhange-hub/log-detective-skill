#!/usr/bin/env python3
"""Regenerate (or verify) every file in examples/output/ from the synthetic logs in examples/logs/.

  python examples/regenerate.py           rewrite examples/output/
  python examples/regenerate.py --check   exit 1 if the committed outputs are stale

Runs the same commands the README shows, from the repository root, so the file names printed in
the reports are the ones you see when you run them yourself. Standard library only.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "examples" / "output"
LOGS = "examples/logs"

# (output file, skill folder, script, arguments)
RUNS = [
    ("cluster.md", "logdet-triage", "log_cluster",
     [LOGS + "/web.jsonl", LOGS + "/worker.log", LOGS + "/db.log", LOGS + "/payments.log",
      "--level", "warning", "--top", "8"]),
    ("cluster-redacted.md", "logdet-triage", "log_cluster",
     [LOGS + "/web.jsonl", LOGS + "/payments.log", "--level", "warning", "--redact", "--top", "12"]),
    ("diff.md", "logdet-triage", "log_cluster",
     [LOGS + "/after.log", "--baseline", LOGS + "/before.log", "--top", "10"]),
    ("trace.md", "logdet-trace", "trace_group",
     [LOGS + "/worker.log", LOGS + "/payments.log", "--app-prefix", "shop/", "--app-prefix", "com.fernhill"]),
    ("timeline.md", "logdet-timeline", "timeline",
     [LOGS + "/web.jsonl", LOGS + "/worker.log", LOGS + "/db.log", LOGS + "/nginx-access.log",
      LOGS + "/payments.log", "--around", "2026-11-12T14:03:00Z", "--window", "10m"]),
]


def _load(skill: str, script: str):
    path = ROOT / "skills" / skill / f"{script}.py"
    spec = importlib.util.spec_from_file_location(f"_regen_{script}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render(skill: str, script: str, argv: list) -> bytes:
    """Run one script's main() from the repo root and return what it printed, as UTF-8 with LF."""
    module = _load(skill, script)
    previous = Path.cwd()
    os.chdir(ROOT)
    try:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = module.main(argv)
    finally:
        os.chdir(previous)
    if code != 0:
        raise RuntimeError(f"{script} {' '.join(argv)} exited with {code}")
    return buffer.getvalue().replace("\r\n", "\n").encode("utf-8")


def generate(out: Path) -> list:
    """Write every report into `out`; return the relative paths written."""
    out.mkdir(parents=True, exist_ok=True)
    for name, skill, script, argv in RUNS:
        (out / name).write_bytes(render(skill, script, argv))
    return sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())


def compare(expected_dir: Path, actual_dir: Path) -> list:
    """Return a list of differences between two output folders."""
    problems = []
    expected = {p.relative_to(expected_dir).as_posix() for p in expected_dir.rglob("*") if p.is_file()}
    actual = {p.relative_to(actual_dir).as_posix() for p in actual_dir.rglob("*") if p.is_file()}
    for name in sorted(expected ^ actual):
        problems.append(f"{name}: {'missing from regenerated output' if name in expected else 'not committed'}")
    for name in sorted(expected & actual):
        if (expected_dir / name).read_bytes() != (actual_dir / name).read_bytes():
            problems.append(f"{name}: content differs")
    return problems


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="verify instead of rewriting")
    args = parser.parse_args(argv)
    if not args.check:
        for name in generate(OUTPUT):
            print(f"wrote examples/output/{name}")
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        generate(Path(tmp))
        problems = compare(OUTPUT, Path(tmp))
    for problem in problems:
        print(problem)
    print("examples/output is up to date" if not problems else f"{len(problems)} stale file(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
