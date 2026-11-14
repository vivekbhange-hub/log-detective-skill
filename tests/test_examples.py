"""The bundled demo: committed outputs must be exactly what the scripts produce (examples/regenerate.py)."""
import io
import os
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from _load import LOGS, ROOT, load_path

regen = load_path(ROOT / "examples" / "regenerate.py", "regenerate_under_test")
OUTPUT = ROOT / "examples" / "output"
EXPECTED = ["cluster-redacted.md", "cluster.md", "diff.md", "timeline.md", "trace.md"]
FIXTURES = ["after.log", "before.log", "db.log", "nginx-access.log", "payments.log", "web.jsonl", "worker.log"]


class DeterminismTests(unittest.TestCase):
    def test_regenerating_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = regen.generate(Path(tmp))
            self.assertEqual(written, EXPECTED)
            self.assertEqual(regen.compare(OUTPUT, Path(tmp)), [])

    def test_result_does_not_depend_on_the_working_directory(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                out = Path(tmp) / "out"
                regen.generate(out)
                self.assertEqual(regen.compare(OUTPUT, out), [])
            finally:
                os.chdir(previous)

    def test_check_mode(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(regen.main(["--check"]), 0)
        self.assertIn("up to date", out.getvalue())

    def test_compare_reports_differences(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            (Path(a) / "x.txt").write_text("1", encoding="utf-8")
            (Path(b) / "x.txt").write_text("2", encoding="utf-8")
            (Path(b) / "y.txt").write_text("2", encoding="utf-8")
            self.assertEqual(regen.compare(Path(a), Path(b)), ["y.txt: not committed", "x.txt: content differs"])

    def test_outputs_use_lf_and_utf8(self):
        for name in EXPECTED:
            with self.subTest(file=name):
                data = (OUTPUT / name).read_bytes()
                self.assertNotIn(b"\r\n", data)
                data.decode("utf-8")

    def test_a_failing_script_is_reported(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(RuntimeError):
            regen.render("logdet-triage", "log_cluster", ["examples/logs/does-not-exist.log"])


class FixtureTests(unittest.TestCase):
    def test_fixture_set(self):
        self.assertEqual(sorted(p.name for p in LOGS.iterdir()), FIXTURES)

    def test_fixtures_are_fictional(self):
        for path in LOGS.iterdir():
            text = path.read_text(encoding="utf-8")
            with self.subTest(file=path.name):
                for ip in re.findall(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])", text):
                    self.assertTrue(ip.startswith("203.0.113."), f"{ip} is not an RFC 5737 documentation address")
                for email in re.findall(r"[\w.+-]+@[\w.-]+", text):
                    self.assertRegex(email, r"@example\.(com|org|net)$")
                for host in re.findall(r"https?://([^/\s\"'\\]+)", text):
                    self.assertRegex(host, r"(^|\.)example\.(com|org|net)$|^203\.0\.113\.\d+(:\d+)?$", host)
                self.assertNotRegex(text, r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
                self.assertNotRegex(text, r"AKIA[0-9A-Z]{16}")
                self.assertNotRegex(text, r"sk_live_|ghp_[A-Za-z0-9]{20}")

    def test_fixtures_cover_the_formats_the_pack_claims(self):
        web = (LOGS / "web.jsonl").read_text(encoding="utf-8").split("\n")[0]
        self.assertTrue(web.startswith("{") and '"level"' in web)
        worker = (LOGS / "worker.log").read_text(encoding="utf-8")
        self.assertRegex(worker, r"(?m)^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ERROR")
        self.assertIn("During handling of the above exception, another exception occurred:", worker)
        nginx = (LOGS / "nginx-access.log").read_text(encoding="utf-8")
        self.assertRegex(nginx, r'^203\.0\.113\.\d+ - - \[\d\d/Nov/2026:\d\d:\d\d:\d\d \+0100\] "(GET|POST) ')
        payments = (LOGS / "payments.log").read_text(encoding="utf-8")
        self.assertIn("\nCaused by: java.net.SocketTimeoutException", payments)
        self.assertRegex((LOGS / "db.log").read_text(encoding="utf-8"), r"UTC \[\d+\] FATAL:")

    def test_fixtures_use_lf_and_end_with_a_newline(self):
        for path in LOGS.iterdir():
            data = path.read_bytes()
            with self.subTest(file=path.name):
                self.assertNotIn(b"\r", data)
                self.assertTrue(data.endswith(b"\n"))

    def test_redaction_demo_has_something_to_redact(self):
        raw = (LOGS / "web.jsonl").read_text(encoding="utf-8") + (LOGS / "payments.log").read_text(encoding="utf-8")
        for needle in ("Bearer FAKE-TOKEN-0000", "ava.martin@example.com", "4242424242424242"):
            self.assertIn(needle, raw)
        redacted = (OUTPUT / "cluster-redacted.md").read_text(encoding="utf-8")
        for needle in ("FAKE-TOKEN-0000", "ava.martin@example.com", "4242424242424242", "ZmFrZXNpZw"):
            self.assertNotIn(needle, redacted)
        for placeholder in ("<email>", "<card>", "<redacted>"):
            self.assertIn(placeholder, redacted)


class DemoContentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cluster = (OUTPUT / "cluster.md").read_text(encoding="utf-8")
        cls.diff = (OUTPUT / "diff.md").read_text(encoding="utf-8")
        cls.trace = (OUTPUT / "trace.md").read_text(encoding="utf-8")
        cls.timeline = (OUTPUT / "timeline.md").read_text(encoding="utf-8")

    def test_cluster_report(self):
        self.assertIn("`sorry, too many clients already`", self.cluster)
        self.assertIn("[psycopg2.OperationalError]", self.cluster)
        self.assertIn("formats: json 454, text", self.cluster)
        self.assertRegex(self.cluster, r"\| 1 \| critical \| 140 \|")

    def test_trace_report(self):
        self.assertIn("## 1. psycopg2.OperationalError x40", self.trace)
        self.assertIn("- Surfaced as: `shop.db.DatabaseUnavailable: cannot open connection`", self.trace)
        self.assertIn("- Root cause: `java.net.SocketTimeoutException: Read timed out`", self.trace)
        self.assertIn("formats: java 4, python 44", self.trace)

    def test_timeline_report(self):
        self.assertIn("The earliest error-level signature in the window is `db:18` at 2026-11-12T14:02:57.390Z "
                      "(not seen before the window).", self.timeline)
        self.assertIn("| 2026-11-12T14:03:00Z | 2026-11-12T14:10:00Z |", self.timeline)
        self.assertIn("-- error spike starts", self.timeline)
        self.assertIn("-- error spike ends --", self.timeline)
        self.assertIn("<- first in window, seen before it", self.timeline)
        self.assertRegex(self.timeline, r"POST /api/checkout 500 .*\[nginx-access.log:\d+\]")
        # nginx wrote 15:03:01 +0100; the merged timeline shows it as 14:03:01Z
        self.assertIn("2026-11-12T14:03:01.000Z  nginx-access", self.timeline)

    def test_diff_report(self):
        self.assertIn("- Versus baseline: 2 new, 1 spiking (rate x3 or more), 3 steady, 1 down, 1 vanished", self.diff)
        self.assertRegex(self.diff, r"\| 1 \| NEW \| error \| 14 \| 0 \|")
        self.assertIn("SPIKING (x14)", self.diff)
        self.assertIn("## Vanished", self.diff)
        self.assertIn("using deprecated redis client v3", self.diff)


if __name__ == "__main__":
    unittest.main()
