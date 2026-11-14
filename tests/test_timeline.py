"""Tests for skills/logdet-timeline/timeline.py (merging, windows, first bad line, spikes, CLI)."""
import gzip
import json
import unittest
from pathlib import Path

from _load import LOGS, TempDir, load, run_main, write_log

tl = load("logdet-timeline", "timeline")
T0 = 1794492180.0   # 2026-11-12T14:03:00Z


def stamp(offset, ms=0):
    return tl.fmt_ts(T0 + offset, True)[:-1]


class HelperTests(unittest.TestCase):
    def test_split_source(self):
        self.assertEqual(tl.split_source("web=logs/web.jsonl"), ("web", "logs/web.jsonl"))
        self.assertEqual(tl.split_source("logs/web.jsonl"), ("web", "logs/web.jsonl"))
        self.assertEqual(tl.split_source("C:\\logs\\db.log"), ("db", "C:\\logs\\db.log"))
        self.assertEqual(tl.split_source("app.log.gz"), ("app", "app.log.gz"))
        self.assertEqual(tl.split_source("/var/log/syslog"), ("syslog", "/var/log/syslog"))

    def test_labels_are_made_unique(self):
        sources = tl.make_sources(["a/app.log", "b/app.log", "c/app.log"])
        self.assertEqual([s.label for s in sources], ["app", "app-2", "app-3"])

    def test_nice_width(self):
        self.assertEqual(tl.nice_width(0.4), 1)
        self.assertEqual(tl.nice_width(30), 30)
        self.assertEqual(tl.nice_width(31), 60)
        self.assertEqual(tl.nice_width(10 ** 9), 86400)

    def test_fmt_duration(self):
        self.assertEqual(tl.fmt_duration(600), "10m")
        self.assertEqual(tl.fmt_duration(5400), "1h30m")
        self.assertEqual(tl.fmt_duration(0), "0s")
        self.assertEqual(tl.fmt_duration(90061), "1d1h1m1s")


class SpikeTests(unittest.TestCase):
    def counter(self, per_bucket, width=60):
        rc = tl.RateCounter(100000)
        for i, n in enumerate(per_bucket):
            for _ in range(n):
                rc.add(T0 + i * width + 1)
        return rc

    def spikes(self, series, lo=float("-inf"), hi=float("inf"), method="mad", **kw):
        rc = self.counter(series)
        span = [T0 + 1, T0 + (len(series) - 1) * 60 + 1]
        return tl.detect_spikes(rc, span, lo, hi, 60, method, kw.get("threshold", 3.5), kw.get("min_spike", 3))

    def test_flat_series_has_no_spike(self):
        spikes, info = self.spikes([2, 3, 2, 2, 3, 2, 3, 2, 2, 3, 2, 2])
        self.assertEqual(spikes, [])
        self.assertEqual(info["baseline_median"], 2)

    def test_spike_is_found_and_adjacent_buckets_merge(self):
        series = [1, 0, 1, 0, 1, 1, 0, 1, 30, 45, 1, 0, 1, 1, 0, 1]
        spikes, info = self.spikes(series)
        self.assertEqual(len(spikes), 1)
        s = spikes[0]
        self.assertEqual((s["start"], s["end"]), (int(T0) // 60 * 60 + 8 * 60, int(T0) // 60 * 60 + 10 * 60))
        self.assertEqual((s["peak"], s["total"]), (45, 75))
        self.assertGreater(s["peak_score"], 3.5)

    def test_two_separate_spikes(self):
        series = [0, 1, 0, 0, 20, 0, 1, 0, 0, 25, 0, 1, 0, 0]
        spikes, _ = self.spikes(series)
        self.assertEqual(len(spikes), 2)

    def test_quiet_baseline_of_zeros(self):
        spikes, info = self.spikes([0] * 10 + [12] + [0] * 5)
        self.assertEqual(len(spikes), 1)
        self.assertEqual(info["baseline_median"], 0)

    def test_min_spike_floor(self):
        spikes, _ = self.spikes([0] * 10 + [2] + [0] * 5, min_spike=3)
        self.assertEqual(spikes, [])
        spikes, _ = self.spikes([0] * 10 + [2] + [0] * 5, min_spike=1)
        self.assertEqual(len(spikes), 1)

    def test_zscore_method(self):
        series = [1, 2, 1, 2, 1, 2, 1, 2, 1, 2, 40, 1, 2, 1]
        spikes, info = self.spikes(series, method="zscore", threshold=3.0)
        self.assertEqual(info["method"], "zscore")
        self.assertEqual([s["peak"] for s in spikes], [40])

    def test_baseline_comes_from_outside_the_window_when_possible(self):
        # a long incident would drag the median up if the window were included
        series = [0, 1, 0, 1, 0, 0, 1, 0, 0, 1] + [30] * 20
        lo = int(T0) // 60 * 60 + 10 * 60
        spikes, info = self.spikes(series, lo=lo, hi=lo + 20 * 60)
        self.assertEqual(info["baseline_buckets"], 10)
        self.assertEqual(len(spikes), 1)
        self.assertEqual(spikes[0]["total"], 600)

    def test_spikes_outside_the_window_are_not_reported(self):
        series = [0, 1, 0, 0, 40, 0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0]
        lo = int(T0) // 60 * 60 + 8 * 60
        spikes, _ = self.spikes(series, lo=lo, hi=lo + 4 * 60)
        self.assertEqual(spikes, [])

    def test_no_errors_at_all(self):
        spikes, info = tl.detect_spikes(tl.RateCounter(), [None, None], 0, 1, 60, "mad", 3.5, 3)
        self.assertEqual((spikes, info["baseline_buckets"]), ([], 0))

    def test_too_many_buckets_is_an_error(self):
        rc = self.counter([1, 1])
        with self.assertRaises(ValueError):
            tl.detect_spikes(rc, [0, 10 ** 9], float("-inf"), float("inf"), 1, "mad", 3.5, 3)


class FoldTests(unittest.TestCase):
    def ev(self, ts, src=0, sig="a", mark=None):
        class R(object):
            pass

        r = R()
        r.ts, r.lineno, r.level, r.msg, r.lines, r.truncated, r.hint = ts, int(ts), "error", "m", ["m"], 0, None
        e = tl.Event(r, src, sig)
        e.mark = mark
        return e

    def test_repeats_fold_within_gap(self):
        events = [self.ev(0), self.ev(10), self.ev(20), self.ev(200), self.ev(205)]
        out = tl.fold(events, 60)
        self.assertEqual([(e.ts, e.repeats, e.until) for e in out], [(0, 3, 20), (200, 2, 205)])

    def test_different_sources_or_signatures_do_not_fold(self):
        out = tl.fold([self.ev(0), self.ev(1, src=1), self.ev(2, sig="b")], 60)
        self.assertEqual(len(out), 3)

    def test_marked_events_start_their_own_run(self):
        out = tl.fold([self.ev(0), self.ev(1, mark="new")], 60)
        self.assertEqual(len(out), 2)

    def test_gap_zero_disables(self):
        self.assertEqual(len(tl.fold([self.ev(0), self.ev(1)], 0)), 2)


WEB = "".join(
    '{"ts":"%s","level":"%s","msg":"%s","service":"web"}\n' % (stamp(o) + "Z", lvl, msg)
    for o, lvl, msg in (
        (-1200, "error", "old failure 1"),                # before the window: signature seen earlier
        (-30, "info", "request ok"),
        (60, "error", "old failure 2"),                   # in window, but the signature was seen before
        (90, "error", "checkout failed for order 10"),    # new
        (95, "error", "checkout failed for order 11"),
        (100, "warning", "slow response 12ms"),
        (3000, "error", "far future failure"),            # after the window
    ))
WORKER = (
    "%s ERROR shop.worker: sync failed\nTraceback (most recent call last):\n  File \"/a.py\", line 1, in f\n"
    "ValueError: x\n"
    "%s INFO shop.worker: done\n"
    "ERROR: orphan line without a timestamp\n" % (stamp(70).replace("T", " ") + "", stamp(80).replace("T", " ")))
# nginx writes local time at +02:00: 16:04:40 +0200 is 14:04:40Z, i.e. 100 seconds after T0
NGINX = '203.0.113.5 - - [12/Nov/2026:16:04:40 +0200] "POST /api/checkout HTTP/1.1" 500 100 "-" "ua"\n'


class CollectTests(unittest.TestCase):
    def run_collect(self, files, lo, hi, min_rank=1, err_rank=3, tz=tl.UTC):
        with TempDir() as d:
            sources = tl.make_sources([write_log(d, n, t) for n, t in files])
            result = tl.collect(sources, tz, 2026, lo, hi, min_rank, err_rank)
            return sources, result

    def test_window_and_first_occurrence(self):
        sources, (events, firsts, seen_before, errors, span, truncated) = self.run_collect(
            [("web.jsonl", WEB)], T0 - 600, T0 + 600, min_rank=0)
        self.assertEqual([e.msg for e in events], ["request ok", "old failure 2", "checkout failed for order 10",
                                                   "checkout failed for order 11", "slow response 12ms"])
        self.assertEqual(sorted(firsts), ["checkout failed for order <n>", "old failure <n>"])
        self.assertEqual(firsts["checkout failed for order <n>"][1], 2)
        self.assertEqual(seen_before, {"old failure <n>"})
        self.assertEqual(sources[0].in_window, 5)
        self.assertFalse(truncated)

    def test_error_counts_cover_all_time_not_just_the_window(self):
        _, (_, _, _, errors, span, _) = self.run_collect([("web.jsonl", WEB)], T0 - 600, T0 + 600)
        self.assertEqual(sum(errors.data.values()), 5)
        self.assertEqual((span[0], span[1]), (T0 - 1200, T0 + 3000))

    def test_records_without_timestamps_are_counted_not_placed(self):
        sources, (events, *_rest) = self.run_collect([("worker.log", WORKER)], float("-inf"), float("inf"))
        self.assertEqual(sources[0].no_ts, 1)

    def test_level_threshold(self):
        _, (events, *_rest) = self.run_collect([("web.jsonl", WEB)], T0 - 600, T0 + 600, min_rank=3)
        self.assertEqual({e.level for e in events}, {"error"})

    def test_mixed_formats_and_timezones_merge_in_utc(self):
        with TempDir() as d:
            files = [write_log(d, "web.jsonl", WEB), write_log(d, "worker.log", WORKER), write_log(d, "nginx.log", NGINX)]
            sources = tl.make_sources(files)
            events, *_ = tl.collect(sources, tl.UTC, 2026, T0 - 600, T0 + 600, 3, 3)
        events.sort(key=lambda e: (e.ts, e.src, e.lineno))
        order = [(sources[e.src].label, round(e.ts - T0)) for e in events]
        self.assertEqual(order, [("web", 60), ("worker", 70), ("web", 90), ("web", 95), ("nginx", 100)])
        self.assertIn(("nginx", 100), order)       # +02:00 offset applied
        self.assertIn(("worker", 70), order)       # naive time taken as UTC
        self.assertEqual({lbl for lbl, _ in order}, {"web", "worker", "nginx"})

    def test_naive_timestamps_follow_tz(self):
        text = "%s ERROR a: x\n" % stamp(0).replace("T", " ")
        _, (events, *_rest) = self.run_collect([("a.log", text)], T0 - 10, T0 + 10, tz=tl.TzSpec("+01:00"))
        self.assertEqual(events, [])               # 14:03 in +01:00 is 13:03Z, outside the window
        _, (events, *_rest) = self.run_collect([("a.log", text)], T0 - 10, T0 + 10, tz=tl.TzSpec("UTC"))
        self.assertEqual(len(events), 1)

    def test_multiline_record_is_one_event_with_its_root_exception(self):
        _, (events, *_rest) = self.run_collect([("worker.log", WORKER)], T0, T0 + 600)
        first = [e for e in events if e.msg.startswith("shop.worker: sync failed")][0]
        self.assertEqual((first.nlines, first.hint), (4, "ValueError"))


class ReportTests(unittest.TestCase):
    def build(self, files, argv_extra=(), **kw):
        args = tl.build_parser().parse_args(["x", "--around", "2026-11-12T14:03:00Z", "--window", "10m"] + list(argv_extra))
        args._around = T0
        lo, hi = T0 - 600, T0 + 600
        with TempDir() as d:
            sources = tl.make_sources([write_log(d, n, t) for n, t in files])
            events, firsts, seen_before, errors, span, truncated = tl.collect(
                sources, tl.UTC, 2026, lo, hi, tl.LEVEL_RANK[args.level], tl.LEVEL_RANK[args.error_level])
            spikes, info = tl.detect_spikes(errors, span, lo, hi, 30, "mad", 3.5, 3)
            return tl.build_report(sources, events, firsts, seen_before, spikes, info, lo, hi, args, truncated,
                                   kw.get("mask", False))

    def test_first_bad_line_is_the_earliest_new_signature(self):
        rep = self.build([("web.jsonl", WEB)])
        fb = rep["first_bad_line"]
        self.assertEqual((fb["source"], fb["message"], fb["new"]), ("web", "checkout failed for order 10", True))
        news = {f["signature"]: f["new"] for f in rep["first_occurrences"]}
        self.assertEqual(news, {"old failure <n>": False, "checkout failed for order <n>": True})

    def test_events_are_marked(self):
        rep = self.build([("web.jsonl", WEB)])
        marks = {e["message"]: e["first_of_kind"] for e in rep["events"]}
        self.assertEqual(marks["checkout failed for order 10"], "new")
        self.assertEqual(marks["old failure 2"], "seen-before")
        self.assertNotIn("checkout failed for order 11", marks)      # folded into the first one

    def test_repeats_are_folded_and_counted(self):
        rep = self.build([("web.jsonl", WEB)])
        # the second checkout failure is within the 60s fold gap of the first (and carries no mark of its own)
        rows = [e for e in rep["events"] if e["message"].startswith("checkout failed")]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["repeats"], 2)
        self.assertIsNotNone(rows[0]["until"])

    def test_default_level_hides_info(self):
        rep = self.build([("web.jsonl", WEB)])
        self.assertNotIn("request ok", [e["message"] for e in rep["events"]])
        rep = self.build([("web.jsonl", WEB)], ["--level", "info"])
        self.assertIn("request ok", [e["message"] for e in rep["events"]])

    def test_max_lines_keeps_first_occurrences(self):
        many = "".join('{"ts":"%s","level":"warning","msg":"w%s noise"}\n' % (stamp(i) + "Z", "x" * (i % 7)) for i in range(-500, 500, 7))
        text = many + '{"ts":"%s","level":"error","msg":"late brand new failure"}\n' % (stamp(590) + "Z")
        rep = self.build([("web.jsonl", text)], ["--max-lines", "10", "--fold-gap", "0"])
        self.assertEqual(rep["events_shown"], 10)
        self.assertGreater(rep["events_omitted"], 0)
        self.assertIn("late brand new failure", [e["message"] for e in rep["events"]])

    def test_redact(self):
        text = '{"ts":"%s","level":"error","msg":"charge failed for ava@example.com card 4242 4242 4242 4242"}\n' % (stamp(5) + "Z")
        rep = self.build([("web.jsonl", text)], mask=True)
        blob = json.dumps(rep)
        self.assertNotIn("ava@example.com", blob)
        self.assertNotIn("4242 4242", blob)

    def test_markdown_sections(self):
        md = tl.render_markdown(self.build([("web.jsonl", WEB), ("nginx.log", NGINX)]))
        for heading in ("# Incident timeline", "## Sources", "## First bad lines", "## Error-rate spikes", "## Timeline"):
            self.assertIn(heading, md)
        self.assertIn("around 2026-11-12T14:03:00Z, 10m before and 10m after", md)
        self.assertIn("[web.jsonl:4]", md)
        self.assertIn("<- first of its kind, new in window", md)
        self.assertIn("<- first in window, seen before it", md)
        self.assertIn("(x2 until ", md)
        self.assertNotIn("\r", md)

    def test_markdown_without_errors_or_events(self):
        md = tl.render_markdown(self.build([("a.log", "%s INFO a: fine\n" % stamp(5).replace("T", " "))]))
        self.assertIn("No error-level records in the window.", md)
        self.assertIn("No error-level records, so no spikes.", md)
        self.assertIn("No records at this level in the window.", md)

    def test_json_is_serialisable(self):
        json.dumps(self.build([("web.jsonl", WEB), ("worker.log", WORKER)]))


class CliTests(unittest.TestCase):
    def files(self, d):
        return [write_log(d, "web.jsonl", WEB), write_log(d, "worker.log", WORKER), write_log(d, "nginx.log", NGINX)]

    def test_json_run(self):
        with TempDir() as d:
            code, out, _ = run_main(tl, self.files(d) + ["--around", "2026-11-12T14:03:00Z", "--window", "10m", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual([s["label"] for s in data["sources"]], ["web", "worker", "nginx"])
        self.assertEqual(data["first_bad_line"]["new"], True)
        times = [e["time"] for e in data["events"]]
        self.assertEqual(times, sorted(times))

    def test_label_syntax(self):
        with TempDir() as d:
            path = write_log(d, "x.jsonl", WEB)
            code, out, _ = run_main(tl, ["frontend=" + path, "--json", "--since", "2026-11-12T13:00:00Z"])
        self.assertEqual(json.loads(out)["sources"][0]["label"], "frontend")

    def test_before_after_override_the_window(self):
        with TempDir() as d:
            path = write_log(d, "web.jsonl", WEB)
            code, out, _ = run_main(tl, [path, "--around", "2026-11-12T14:03:00Z", "--before", "1m", "--after", "5s", "--level", "info", "--json"])
        data = json.loads(out)
        self.assertEqual(data["window"]["start"], "2026-11-12T14:02:00Z")
        self.assertEqual(data["window"]["end"], "2026-11-12T14:03:05Z")
        self.assertEqual(data["events_in_window"], 1)

    def test_since_until(self):
        with TempDir() as d:
            path = write_log(d, "web.jsonl", WEB)
            code, out, _ = run_main(tl, [path, "--since", "2026-11-12T14:04:00Z", "--until", "2026-11-12T14:04:31Z", "--json"])
        self.assertEqual(json.loads(out)["events_in_window"], 2)

    def test_no_window_means_everything(self):
        with TempDir() as d:
            path = write_log(d, "web.jsonl", WEB)
            code, out, _ = run_main(tl, [path, "--json"])
        data = json.loads(out)
        self.assertEqual(data["events_in_window"], 6)    # warning and above: five errors and one warning
        self.assertIsNone(data["window"]["start"])

    def test_out_file_is_lf(self):
        with TempDir() as d:
            path = write_log(d, "web.jsonl", WEB)
            code, out, _ = run_main(tl, [path, "-o", d + "/t.md"])
            with open(d + "/t.md", "rb") as handle:
                data = handle.read()
        self.assertEqual((code, out), (0, ""))
        self.assertNotIn(b"\r\n", data)

    def test_gzip_source(self):
        with TempDir() as d:
            path = d + "/web.jsonl.gz"
            with gzip.open(path, "wb") as f:
                f.write(WEB.encode("utf-8"))
            code, out, _ = run_main(tl, [path, "--json"])
        self.assertEqual(json.loads(out)["sources"][0]["label"], "web")

    def test_out_creates_folders_and_never_overwrites_the_input(self):
        with TempDir() as d:
            path = write_log(d, "a.log", WEB)
            code, out, _ = run_main(tl, [path, "-o", d + "/new/sub/report.md"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(d) / "new" / "sub" / "report.md").exists())
            code, _, err = run_main(tl, [path, "-o", path])
            self.assertEqual(code, 2)
            self.assertIn("refusing to overwrite", err)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), WEB.encode("utf-8"))

    def test_error_exits(self):
        self.assertEqual(run_main(tl, ["/no/such.log"])[0], 2)
        with TempDir() as d:
            path = write_log(d, "a.log", WEB)
            self.assertEqual(run_main(tl, [path, "--around", "soon"])[0], 2)
            self.assertEqual(run_main(tl, [path, "--around", "2026-11-12T14:03:00Z", "--window", "ten"])[0], 2)
            self.assertEqual(run_main(tl, [path, "--around", "2026-11-12T14:03:00Z", "--since", "2026-11-12T14:00:00Z"])[0], 2)
            self.assertEqual(run_main(tl, [path, "--since", "2026-11-12T15:00:00Z", "--until", "2026-11-12T14:00:00Z"])[0], 2)
            self.assertEqual(run_main(tl, [path, "--bucket", "0s"])[0], 2)
            self.assertEqual(run_main(tl, [path, "--tz", "Nowhere/Land"])[0], 2)
            self.assertEqual(run_main(tl, [path, "--level", "loud"])[0], 2)

    def test_explicit_bucket_and_method(self):
        with TempDir() as d:
            path = write_log(d, "web.jsonl", WEB)
            code, out, _ = run_main(tl, [path, "--bucket", "2m", "--method", "zscore", "--json"])
        self.assertEqual(json.loads(out)["spike"]["bucket_seconds"], 120)
        self.assertEqual(json.loads(out)["spike"]["method"], "zscore")


class ExampleLogTests(unittest.TestCase):
    def test_example_incident(self):
        files = [str(LOGS / n) for n in ("web.jsonl", "worker.log", "db.log", "nginx-access.log", "payments.log")]
        code, out, _ = run_main(tl, files + ["--around", "2026-11-12T14:03:00Z", "--window", "10m", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        fb = data["first_bad_line"]
        self.assertEqual((fb["source"], fb["new"]), ("db", True))
        self.assertTrue(fb["time"].startswith("2026-11-12T14:02:57"))
        self.assertEqual(len(data["spikes"]), 1)
        self.assertTrue(data["spikes"][0]["start"].startswith("2026-11-12T14:03:00"))
        seen_before = [f["signature"] for f in data["first_occurrences"] if not f["new"]]
        self.assertTrue(any("charge failed" in s for s in seen_before))     # payments timeouts are old noise
        first_500 = [e for e in data["events"] if e["message"].startswith("POST /api/checkout 500")][0]
        self.assertEqual(first_500["source"], "nginx-access")
        self.assertTrue(first_500["time"].startswith("2026-11-12T14:03:01"))      # logged as 15:03:01 +0100


if __name__ == "__main__":
    unittest.main()
