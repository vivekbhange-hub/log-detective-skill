"""Load helper scripts by path (skill folders have hyphens, so they are not packages)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "examples" / "logs"


def load(skill: str, script: str):
    """Import skills/<skill>/<script>.py as a module named <script>_under_test."""
    return load_path(ROOT / "skills" / skill / f"{script}.py", f"{script}_under_test")


def load_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_log(directory: str, name: str, text: str) -> str:
    """Write a fixture log with LF endings and return its path."""
    path = Path(directory) / name
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def run_main(module, argv):
    """Call module.main(argv); return (exit code, stdout text, stderr text)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as exc:      # argparse errors
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class TempDir(object):
    """unittest-friendly temporary directory."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        return self._tmp.name

    def __exit__(self, *exc):
        self._tmp.cleanup()
        return False
