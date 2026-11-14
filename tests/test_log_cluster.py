"""Tests for skills/logdet-triage/log_cluster.py (parsing, normalising, clustering, baseline, CLI)."""
import gzip
import io
import json
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock

from _load import LOGS, TempDir, load, run_main, write_log

lc = load("logdet-triage", "log_cluster")
T0 = 1794492180.0   # 2026-11-12T14:03:00Z


def start(line, **kw):
    return lc.parse_start(line, kw.get("tz", lc.UTC), kw.get("year", 2026), kw.get("lines_mode", False))


class TimestampTests(unittest.TestCase):
    def ts(self, text, tz=None, year=2026):
        hit = lc.find_timestamp(text, tz or lc.UTC, year)
        return None if hit is None else hit[0]

    def test_iso_variants(self):
        self.assertEqual(self.ts("2026-11-12T14:03:00Z x"), T0)
        self.assertEqual(self.ts("2026-11-12 14:03:00 x"), T0)
        self.assertEqual(self.ts("2026-11-12 14:03:00,500 x"), T0 + 0.5)
        self.assertEqual(self.ts("2026-11-12T14:03:00.250Z"), T0 + 0.25)
        self.assertEqual(self.ts("2026/11/12 14:03:00 go log"), T0)
        self.assertEqual(self.ts("2026-11-12 14:03:00.100 UTC [12] LOG"), T0 + 0.1)

    def test_offsets_are_converted_to_utc(self):
        self.assertEqual(self.ts("2026-11-12T15:03:00+01:00"), T0)
        self.assertEqual(self.ts("2026-11-12T15:03:00+0100"), T0)
        self.assertEqual(self.ts("2026-11-12T08:33:00-05:30"), T0)

    def test_naive_times_use_tz(self):
        self.assertEqual(self.ts("2026-11-12 15:03:00", lc.TzSpec("+01:00")), T0)
        self.assertEqual(self.ts("2026-11-12 14:03:00", lc.TzSpec("UTC")), T0)
        self.assertEqual(self.ts("2026-11-12 14:03:00", lc.TzSpec("-0500")), T0 + 5 * 3600)

    def test_iana_timezone(self):
        try:
            tz = lc.TzSpec("Europe/Berlin")
        except ValueError:
            self.skipTest("no IANA tz database on this machine")
        self.assertEqual(self.ts("2026-11-12 15:03:00", tz), T0)           # CET, UTC+1
        self.assertEqual(self.ts("2026-07-12 16:03:00", tz), self.ts("2026-07-12T14:03:00Z"))   # CEST, UTC+2

    def test_bad_timezone(self):
        with self.assertRaises(ValueError):
            lc.TzSpec("Not/AZone")

    def test_apache_format(self):
        self.assertEqual(self.ts('1.2.3.4 - - [12/Nov/2026:15:03:00 +0100] "GET / HTTP/1.1"'), T0)
        self.assertEqual(self.ts("[10/Oct/2000:13:55:36 -0700]"), 971211336.0)

    def test_syslog_uses_year(self):
        self.assertEqual(self.ts("Nov 12 14:03:00 host sshd[1]: x", year=2026), T0)
        self.assertEqual(self.ts("Nov  2 14:03:00 host x", year=2026), T0 - 10 * 86400)

    def test_epoch_seconds_and_millis(self):
        self.assertEqual(self.ts("1794492180 started"), T0)
        self.assertEqual(self.ts("1794492180.5 started"), T0 + 0.5)
        self.assertEqual(self.ts("1794492180500 started"), T0 + 0.5)
        self.assertIsNone(self.ts("shop started 1794492180"))      # epoch only counts at the line start
        self.assertIsNone(self.ts("12345 items"))

    def test_invalid_dates_are_rejected(self):
        self.assertIsNone(self.ts("2026-13-40 25:61:61 nope"))
        self.assertIsNone(self.ts("no time here"))

    def test_fmt_ts(self):
        self.assertEqual(lc.fmt_ts(T0), "2026-11-12T14:03:00Z")
        self.assertEqual(lc.fmt_ts(T0 + 0.5), "2026-11-12T14:03:00.500Z")
        self.assertEqual(lc.fmt_ts(T0, ms=True), "2026-11-12T14:03:00.000Z")
        self.assertEqual(lc.fmt_ts(T0 + 0.9996), "2026-11-12T14:03:01Z")

    def test_parse_when_and_duration(self):
        self.assertEqual(lc.parse_when("2026-11-12T14:03:00Z"), T0)
        self.assertEqual(lc.parse_when("2026-11-12T14:03"), T0)
        self.assertEqual(lc.parse_when("2026-11-12"), T0 - 14 * 3600 - 180)
        self.assertEqual(lc.parse_when("1794492180"), T0)
        with self.assertRaises(ValueError):
            lc.parse_when("yesterday-ish")
        self.assertEqual(lc.parse_duration("90s"), 90)
        self.assertEqual(lc.parse_duration("10m"), 600)
        self.assertEqual(lc.parse_duration("1h30m"), 5400)
        self.assertEqual(lc.parse_duration("45"), 45)
        with self.assertRaises(ValueError):
            lc.parse_duration("ten minutes")


class FormatTests(unittest.TestCase):
    def test_python_logging(self):
        r = start("2026-11-12 14:03:01,123 ERROR shop.orders: payment failed for order 1001")
        self.assertEqual((r.level, r.fmt, r.msg), ("error", "text", "shop.orders: payment failed for order 1001"))
        self.assertAlmostEqual(r.ts, T0 + 1.123)

    def test_python_basicconfig_has_no_timestamp(self):
        r = start("WARNING:root:disk almost full")
        self.assertEqual((r.level, r.ts, r.msg), ("warning", None, "root:disk almost full"))

    def test_log4j_with_thread(self):
        r = start("2026-11-12 14:03:01,123 [http-nio-8080-exec-1] ERROR com.acme.Foo - charge failed")
        self.assertEqual((r.level, r.msg), ("error", "com.acme.Foo - charge failed"))

    def test_node_winston_lowercase_level(self):
        r = start("2026-11-12T14:03:01.812Z error: connect ECONNREFUSED 203.0.113.9:5432")
        self.assertEqual((r.level, r.msg), ("error", "connect ECONNREFUSED 203.0.113.9:5432"))

    def test_go_standard_log_has_no_level(self):
        r = start("2026/11/12 14:03:01 connection refused to db")
        self.assertEqual(r.ts, T0 + 1)
        self.assertEqual(r.level, "error")           # inferred from the words
        self.assertTrue(r.inferred)

    def test_go_logrus_logfmt(self):
        r = start('time="2026-11-12T14:03:01Z" level=error msg="db down" err="dial tcp: refused" host=a')
        self.assertEqual((r.fmt, r.level, r.msg), ("logfmt", "error", "db down: dial tcp: refused"))
        self.assertEqual(r.ts, T0 + 1)

    def test_logfmt_without_timestamp_field(self):
        r = start("level=warn msg=slow duration=3s")
        self.assertEqual((r.fmt, r.level, r.msg, r.ts), ("logfmt", "warning", "slow", None))

    def test_syslog(self):
        r = start("Nov 12 14:03:01 web1 sshd[441]: Failed password for root from 203.0.113.5")
        self.assertEqual((r.fmt, r.level), ("syslog", "error"))
        self.assertEqual(r.msg, "sshd: Failed password for root from 203.0.113.5")
        self.assertEqual(r.ts, T0 + 1)

    def test_nginx_access_levels_follow_status(self):
        line = '203.0.113.17 - - [12/Nov/2026:15:03:01 +0100] "POST /api/orders/88213/pay?x=1 HTTP/1.1" %d 187 "-" "curl/8"'
        r = start(line % 502)
        self.assertEqual((r.fmt, r.level, r.ts), ("access", "error", T0 + 1))
        self.assertEqual(lc.signature(r), "POST /api/orders/<id>/pay 502")
        self.assertEqual(start(line % 404).level, "warning")
        self.assertEqual(start(line % 200).level, "info")

    def test_access_log_with_garbage_request(self):
        r = start('203.0.113.9 - - [12/Nov/2026:14:03:01 +0000] "-" 400 0 "-" "-"')
        self.assertEqual((r.fmt, r.level), ("access", "warning"))

    def test_json_key_variants(self):
        r = start('{"ts":"2026-11-12T14:03:01Z","level":"error","msg":"boom"}')
        self.assertEqual((r.fmt, r.level, r.msg, r.ts), ("json", "error", "boom", T0 + 1))
        r = start('{"@timestamp":"2026-11-12T14:03:01.5Z","severity":"WARNING","message":"slow"}')
        self.assertEqual((r.level, r.msg, r.ts), ("warning", "slow", T0 + 1.5))
        r = start('{"time":1794492181000,"levelname":"CRITICAL","event":"down"}')
        self.assertEqual((r.level, r.msg, r.ts), ("critical", "down", T0 + 1))
        r = start('{"log":{"level":"info"},"message":"ok"}')
        self.assertEqual((r.level, r.msg), ("info", "ok"))
        r = start('{"log.level":"debug","message":"ecs"}')
        self.assertEqual(r.level, "debug")

    def test_json_pino_numeric_level_and_error_object(self):
        r = start('{"level":50,"time":1794492181000,"msg":"request failed","err":{"type":"TimeoutError","message":"3s"}}')
        self.assertEqual(r.level, "error")
        self.assertEqual(r.msg, "request failed: TimeoutError: 3s")

    def test_json_without_level_is_inferred(self):
        r = start('{"msg":"payment failed","error":"card declined"}')
        self.assertEqual((r.level, r.inferred), ("error", True))
        r = start('{"msg":"hello"}')
        self.assertEqual((r.level, r.inferred), ("info", True))

    def test_json_stack_field_is_kept_for_trace_tools(self):
        r = start('{"level":"error","msg":"x","stack":"Error: boom\\n    at f (/a.js:1:2)"}')
        self.assertIn("at f", r.stack)

    def test_invalid_json_falls_back_to_text(self):
        r = start('{"broken json ERROR 2026-11-12 14:03:01')
        self.assertTrue(r is None or r.fmt != "json")

    def test_indented_line_is_a_continuation(self):
        self.assertIsNone(start("    at com.acme.Foo.bar(Foo.java:1)"))
        self.assertIsNone(start("Traceback (most recent call last):"))

    def test_norm_level(self):
        for raw, want in (("WARN", "warning"), ("Warning", "warning"), ("fatal", "critical"), ("trace", "debug"),
                          ("err", "error"), (30, "info"), (60, "critical"), ("50", "error"), ("loud", None),
                          (True, None), (None, None)):
            self.assertEqual(lc.norm_level(raw), want, raw)


class RecordJoinTests(unittest.TestCase):
    def records(self, text, **kw):
        return list(lc.iter_records(text.split("\n"), kw.get("tz", lc.UTC), 2026))

    def test_python_traceback_is_joined_to_its_log_line(self):
        text = ("2026-11-12 14:03:01,100 ERROR app: job failed\n"
                "Traceback (most recent call last):\n"
                '  File "/app/a.py", line 3, in run\n'
                "    go()\n"
                "ValueError: bad input\n"
                "2026-11-12 14:03:02,000 INFO app: next")
        recs = self.records(text)
        self.assertEqual(len(recs), 2)
        self.assertEqual(len(recs[0].lines), 5)
        self.assertEqual(recs[0].hint, "ValueError")
        self.assertEqual(recs[0].lineno, 1)
        self.assertEqual(recs[1].lineno, 6)
        self.assertEqual(lc.signature(recs[0]), "app: job failed [ValueError]")

    def test_java_trace_root_cause_is_deepest_caused_by(self):
        text = ("2026-11-12 14:03:01,100 [t] ERROR c.a.S - charge failed\n"
                "com.acme.GatewayException: failed\n"
                "\tat com.acme.G.charge(G.java:1)\n"
                "Caused by: java.net.SocketTimeoutException: Read timed out\n"
                "\tat java.base/x.Y.z(Y.java:2)\n"
                "\t... 6 more\n"
                "2026-11-12 14:03:02,000 INFO next")
        recs = self.records(text)
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0].hint, "java.net.SocketTimeoutException")

    def test_go_panic_hint(self):
        text = ("2026/11/12 14:03:01 server error\n"
                "panic: runtime error: index out of range [5] with length 3\n"
                "\n"
                "goroutine 1 [running]:\n"
                "main.main()\n"
                "2026/11/12 14:03:02 restarted")
        recs = self.records(text)
        self.assertEqual(recs[0].hint, "panic: runtime error: index out of range [<n>] with length <n>")
        self.assertEqual(len(recs), 2)

    def test_exception_in_a_plain_info_line_raises_the_level(self):
        text = "2026-11-12 14:03:01 job done\nTraceback (most recent call last):\nKeyError: 'x'"
        rec = self.records(text)[0]
        self.assertEqual(rec.level, "error")

    def test_blank_lines_inside_a_record_are_kept_trailing_ones_dropped(self):
        text = "2026-11-12 14:03:01 ERROR a: x\nTraceback (most recent call last):\n\nValueError: y\n\n\n2026-11-12 14:03:02 INFO b: z"
        recs = self.records(text)
        self.assertEqual(recs[0].lines, ["2026-11-12 14:03:01 ERROR a: x", "Traceback (most recent call last):", "",
                                         "ValueError: y"])

    def test_logs_without_timestamps_are_one_record_per_line(self):
        text = "starting up\nlistening on port 80\nERROR: db unreachable\n  detail line\nshutting down"
        recs = self.records(text)
        self.assertEqual([r.msg for r in recs], ["starting up", "listening on port 80", "db unreachable", "shutting down"])
        self.assertEqual(len(recs[2].lines), 2)

    def test_orphan_continuation_before_the_first_record(self):
        recs = self.records("    stray indented line\n2026-11-12 14:03:01 INFO a: b")
        self.assertEqual(len(recs), 2)

    def test_continuation_is_capped(self):
        text = "2026-11-12 14:03:01 ERROR a: x\n" + "\n".join("  frame %d" % i for i in range(1000))
        rec = self.records(text)[0]
        self.assertEqual(len(rec.lines), lc.MAX_CONTINUATION)
        self.assertEqual(rec.truncated, 1000 - (lc.MAX_CONTINUATION - 1))

    def test_json_lines_are_never_joined(self):
        text = '{"level":"error","msg":"a"}\n{"level":"info","msg":"b"}'
        self.assertEqual(len(self.records(text)), 2)

    def test_crlf_input(self):
        recs = list(lc.iter_records(["2026-11-12 14:03:01 INFO a: b\r\n", "2026-11-12 14:03:02 INFO a: c\r\n"]))
        self.assertEqual([r.msg for r in recs], ["a: b", "a: c"])


class NormalizeTests(unittest.TestCase):
    def n(self, text):
        return lc.normalize(text)

    def test_numbers_and_units(self):
        self.assertEqual(self.n("timeout after 3000ms on attempt 3"), "timeout after <n>ms on attempt <n>")
        self.assertEqual(self.n("took 1.5s"), "took <n>s")
        self.assertEqual(self.n("order-1234 shipped"), "order-<n> shipped")

    def test_identifiers_with_digits_inside_words_survive(self):
        self.assertEqual(self.n("utf8 http2 ec2 sha256"), "utf8 http2 ec2 sha256")

    def test_uuid_hex_and_tokens(self):
        self.assertEqual(self.n("id 550e8400-e29b-41d4-a716-446655440000 failed"), "id <uuid> failed")
        self.assertEqual(self.n("commit deadbeef12 and 0x7fff"), "commit <hex> and <hex>")
        self.assertEqual(self.n("key sk9Xa7Qm2LpR4tVw8Zb3Nc1Hd6 rejected"), "key <token> rejected")
        self.assertEqual(self.n("plain words stay plain"), "plain words stay plain")

    def test_ips_ports_and_emails(self):
        self.assertEqual(self.n("from 203.0.113.5:8443 to 10.0.0.1"), "from <ip> to <ip>")
        self.assertEqual(self.n("mail to ava.martin@example.com failed"), "mail to <email> failed")
        self.assertEqual(self.n("version 1.2.3"), "version <n>.<n>")   # not an IP

    def test_paths_and_urls(self):
        self.assertEqual(self.n("cannot open /var/lib/app/data.db"), "cannot open <path>")
        self.assertEqual(self.n(r"cannot open C:\Users\ava\app.log now"), "cannot open <path> now")
        self.assertEqual(self.n("GET https://api.example.com/v1/orders?id=5 failed"), "GET https://api.example.com/<path> failed")
        self.assertEqual(self.n("ping https://example.com ok"), "ping https://example.com ok")
        self.assertEqual(self.n("choose and/or other"), "choose and/or other")
        self.assertEqual(self.n("GET /health ok"), "GET /health ok")

    def test_quoted_values(self):
        self.assertEqual(self.n('column "price" does not exist'), 'column "<str>" does not exist')
        self.assertEqual(self.n("KeyError: 'user_id'"), "KeyError: '<str>'")
        self.assertEqual(self.n("can't open it, don't retry"), "can't open it, don't retry")

    def test_timestamps(self):
        self.assertEqual(self.n("started at 2026-11-12T14:03:01.123Z ok"), "started at <ts> ok")
        self.assertEqual(self.n("at 14:03:01 retry"), "at <time> retry")
        self.assertEqual(self.n("date 2026-11-12 only"), "date <ts> only")

    def test_bearer_and_jwt(self):
        self.assertEqual(self.n("sent Bearer abc123def456 to api"), "sent Bearer <token> to api")
        self.assertEqual(self.n("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.ZmFrZXNpZw bad"), "jwt <jwt> bad")

    def test_whitespace_is_collapsed_and_result_is_capped(self):
        self.assertEqual(self.n("a    b \t c"), "a b c")
        self.assertLessEqual(len(self.n("x " * 500)), 240)

    def test_same_event_same_signature(self):
        a = self.n("payment failed for order 1001 (user alice@example.com) from 203.0.113.5 after 3000ms")
        b = self.n("payment failed for order 99 (user bob@example.com) from 203.0.113.77 after 12ms")
        self.assertEqual(a, b)

    def test_cache_returns_same_value(self):
        self.assertEqual(self.n("retry 5"), self.n("retry 5"))


class RedactTests(unittest.TestCase):
    def test_masks(self):
        r = lc.redact
        self.assertEqual(r("mail ava.martin@example.com now"), "mail <email> now")
        self.assertEqual(r("Authorization: Bearer abc.def.ghi123 end"), "Authorization: <redacted> end")
        self.assertEqual(r("header Bearer abcdef123456 here"), "header Bearer <redacted> here")
        self.assertEqual(r("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.ZmFrZXNpZw ok"), "jwt <jwt> ok")
        self.assertEqual(r("password=hunter2 user=ava"), "password=<redacted> user=ava")
        self.assertEqual(r('{"api_key": "abc123"}'), '{"api_key": "<redacted>"}')
        self.assertEqual(r("from 203.0.113.5 and 2001:db8:0:0:0:0:0:1"), "from <ip> and <ip>")
        self.assertEqual(r("key sk_live_" + "ABCDEFGH12345678 key"), "key <token> key")
        self.assertEqual(r("aws AKIA" + "ABCDEFGHIJKLMNOP"), "aws <token>")
        self.assertEqual(r("sha " + "a1" * 20), "sha <token>")

    def test_cards_need_a_valid_luhn_checksum(self):
        self.assertEqual(lc.redact("card 4242 4242 4242 4242 ok"), "card <card> ok")
        self.assertEqual(lc.redact("card 4242424242424242 ok"), "card <card> ok")
        self.assertEqual(lc.redact("card 4242-4242-4242-4242 ok"), "card <card> ok")
        self.assertEqual(lc.redact("order 1234567890123456 ok"), "order 1234567890123456 ok")
        self.assertEqual(lc.redact("took 1794492180123 ms"), "took 1794492180123 ms")

    def test_clean_text_is_unchanged(self):
        self.assertEqual(lc.redact("nothing secret here, 42 items"), "nothing secret here, 42 items")


class RateTests(unittest.TestCase):
    def test_histogram(self):
        rc = lc.RateCounter()
        for t in (0, 1, 2, 50, 51, 99):
            rc.add(T0 + t)
        h = rc.histogram(T0, T0 + 99, 4)
        self.assertEqual(sum(h), 6)
        self.assertEqual(h[0], 3)
        self.assertEqual(h[-1], 1)

    def test_resolution_doubles_when_too_many_slots(self):
        rc = lc.RateCounter(limit=16)
        for t in range(1000):
            rc.add(T0 + t)
        self.assertLessEqual(len(rc.data), 16)
        self.assertGreater(rc.res, 1)
        self.assertEqual(sum(rc.data.values()), 1000)
        self.assertEqual(sum(rc.histogram(T0, T0 + 999, 10)), 1000)

    def test_rebucket(self):
        rc = lc.RateCounter()
        for t in (0, 5, 65):
            rc.add(T0 + t)
        buckets = rc.rebucket(60)
        self.assertEqual(sorted(buckets.values()), [1, 2])

    def test_sparkline(self):
        self.assertEqual(lc.sparkline([0, 0, 0]), "\u2581\u2581\u2581")
        s = lc.sparkline([0, 1, 10])
        self.assertEqual((s[0], s[-1]), ("\u2581", "\u2588"))
        self.assertEqual(lc.sparkline([0, 5, 10], ascii_only=True), "_=#")
        self.assertEqual(lc.sparkline([]), "")


LOG = """2026-11-12 14:00:00,000 INFO shop: started
2026-11-12 14:01:00,000 ERROR shop.pay: gateway timeout after 3000ms for order 1
2026-11-12 14:01:10,000 ERROR shop.pay: gateway timeout after 2900ms for order 2
2026-11-12 14:02:00,000 WARNING shop.cache: miss for key a1
2026-11-12 14:02:30,000 ERROR shop.db: connection refused
Traceback (most recent call last):
  File "/app/db.py", line 1, in c
ConnectionRefusedError: [Errno 111] refused
2026-11-12 14:03:00,000 ERROR shop.pay: gateway timeout after 3100ms for order 3
2026-11-12 14:04:00,000 DEBUG shop: tick
"""


class AnalyzeTests(unittest.TestCase):
    def analyze(self, text, **kw):
        with TempDir() as d:
            return lc.analyze([write_log(d, "a.log", text)], **kw)

    def test_clusters_counts_and_range(self):
        an = self.analyze(LOG)
        self.assertEqual(an.records, 7)
        self.assertEqual(an.lines, 10)
        sigs = {c.sig: c for c in an.clusters.values()}
        pay = sigs["shop.pay: gateway timeout after <n>ms for order <n>"]
        self.assertEqual(pay.count, 3)
        self.assertEqual(lc.fmt_ts(pay.first), "2026-11-12T14:01:00Z")
        self.assertEqual(lc.fmt_ts(pay.last), "2026-11-12T14:03:00Z")
        self.assertIn("shop.db: connection refused [ConnectionRefusedError]", sigs)
        self.assertEqual(lc.fmt_ts(an.min_ts), "2026-11-12T14:00:00Z")
        self.assertEqual(lc.fmt_ts(an.max_ts), "2026-11-12T14:04:00Z")

    def test_level_filter_keeps_that_level_and_worse(self):
        an = self.analyze(LOG, min_level="error")
        self.assertEqual(an.counted, 4)
        self.assertEqual(an.level_skipped, 3)
        an = self.analyze(LOG, min_level="warning")
        self.assertEqual(an.counted, 5)

    def test_since_until(self):
        an = self.analyze(LOG, since=lc.parse_when("2026-11-12T14:01:30Z"), until=lc.parse_when("2026-11-12T14:03:00Z"))
        self.assertEqual(an.counted, 2)          # 14:02:00 and 14:02:30; until is exclusive
        self.assertEqual(an.time_skipped, 5)

    def test_records_without_timestamps(self):
        an = self.analyze("ERROR: a\nERROR: a\nERROR: b\n")
        self.assertEqual(an.no_ts, 3)
        self.assertIsNone(an.min_ts)
        an = self.analyze("ERROR: a\n", since=1.0)
        self.assertEqual(an.counted, 0)          # a time filter cannot place them, so they are skipped

    def test_max_clusters_overflow(self):
        text = "".join("2026-11-12 14:00:%02d ERROR a: unique%s\n" % (i % 60, "x" * i) for i in range(1, 30))
        an = self.analyze(text, max_clusters=5)
        self.assertEqual(len(an.clusters), 5)
        self.assertEqual(an.overflow, 24)

    def test_sample_is_first_occurrence_and_is_limited(self):
        an = self.analyze(LOG, sample_lines=2)
        db = [c for c in an.clusters.values() if "refused" in c.sig][0]
        self.assertEqual(db.sample_line, 5)
        self.assertEqual(db.sample.count("\n"), 2)
        self.assertIn("(2 more lines)", db.sample)

    def test_multiple_files_are_clustered_together(self):
        with TempDir() as d:
            a = write_log(d, "a.log", "2026-11-12 14:00:00 ERROR x: boom 1\n")
            b = write_log(d, "b.log", "2026-11-12 14:00:05 ERROR x: boom 2\n")
            an = lc.analyze([a, b])
        self.assertEqual(len(an.clusters), 1)
        self.assertEqual(list(an.clusters.values())[0].count, 2)

    def test_gzip_input(self):
        with TempDir() as d:
            path = d + "/a.log.gz"
            with gzip.open(path, "wb") as f:
                f.write(LOG.encode("utf-8"))
            an = lc.analyze([path])
        self.assertEqual(an.records, 7)

    def test_invalid_utf8_is_tolerated(self):
        with TempDir() as d:
            path = d + "/a.log"
            with open(path, "wb") as f:
                f.write(b"2026-11-12 14:00:00 ERROR x: bad \xff\xfe byte\n")
            an = lc.analyze([path])
        self.assertEqual(an.records, 1)

    def test_missing_file_raises(self):
        with self.assertRaises(OSError):
            lc.analyze(["/no/such/file.log"])


class ReportTests(unittest.TestCase):
    def report(self, text, base_text=None, **kw):
        with TempDir() as d:
            an = lc.analyze([write_log(d, "a.log", text)])
            base = lc.analyze([write_log(d, "b.log", base_text)]) if base_text is not None else None
            return lc.build_report(an, base=base, **kw)

    def test_ranking_puts_worse_levels_first_then_counts(self):
        rep = self.report(LOG)
        levels = [c["level"] for c in rep["clusters"]]
        self.assertEqual(levels, sorted(levels, key=lambda l: -lc.LEVEL_RANK[l]))
        self.assertEqual(rep["clusters"][0]["count"], 3)
        self.assertEqual([c["rank"] for c in rep["clusters"]], list(range(1, len(rep["clusters"]) + 1)))

    def test_top_limits_clusters(self):
        rep = self.report(LOG, top=2)
        self.assertEqual((rep["shown"], rep["cluster_count"]), (2, 5))

    def test_rate_histogram_has_requested_buckets(self):
        rep = self.report(LOG, buckets=8)
        for c in rep["clusters"]:
            self.assertEqual(len(c["rate"]), 8)
            self.assertEqual(sum(c["rate"]), c["count"])
            self.assertEqual(len(c["sparkline"]), 8)

    def test_json_serialisable(self):
        json.dumps(self.report(LOG, base_text=LOG))

    def test_baseline_statuses(self):
        before = ("".join("2026-11-12 10:%02d:00 INFO a: steady %d\n" % (i, i) for i in range(10)) +
                  "".join("2026-11-12 10:%02d:30 INFO a: noisy\n" % i for i in range(10)) +
                  "2026-11-12 10:05:00 WARNING a: old warning\n" * 4 +
                  "2026-11-12 10:09:59 INFO a: end\n")
        after = ("".join("2026-11-12 11:%02d:00 INFO a: steady %d\n" % (i, i) for i in range(10)) +
                 "".join("2026-11-12 11:%02d:30 INFO a: noisy\n" % i for i in range(1)) * 1 +
                 "".join("2026-11-12 11:%02d:10 ERROR a: brand new problem\n" % i for i in range(5)) +
                 "".join("2026-11-12 11:0%d:20 INFO a: growing\n" % (i % 10) for i in range(40)) +
                 "2026-11-12 11:09:59 INFO a: end\n")
        before += "2026-11-12 10:00:20 INFO a: growing\n" * 3
        rep = self.report(after, before)
        status = {c["signature"]: c["status"] for c in rep["clusters"]}
        self.assertEqual(status["a: brand new problem"], "new")
        self.assertEqual(status["a: growing"], "spiking")
        self.assertEqual(status["a: steady <n>"], "steady")
        self.assertEqual(status["a: noisy"], "down")
        self.assertEqual([v["signature"] for v in rep["vanished"]], ["a: old warning"])
        self.assertEqual(rep["summary"]["new"], 1)
        # new first, then spiking, then steady, then down
        self.assertEqual([c["status"] for c in rep["clusters"]][:2], ["new", "spiking"])
        new = [c for c in rep["clusters"] if c["status"] == "new"][0]
        self.assertEqual(new["baseline_count"], 0)

    def test_spiking_needs_min_count(self):
        before = "2026-11-12 10:00:00 INFO a: x\n2026-11-12 10:09:00 INFO a: end\n"
        after = "2026-11-12 10:00:00 INFO a: x\n2026-11-12 10:00:01 INFO a: x\n2026-11-12 10:09:00 INFO a: end\n"
        rep = self.report(after, before, min_count=3)
        self.assertEqual({c["signature"]: c["status"] for c in rep["clusters"]}["a: x"], "steady")

    def test_redact_masks_samples_and_signatures(self):
        text = '2026-11-12 14:00:00 ERROR a: failed for ava@example.com card 4242 4242 4242 4242 ip 203.0.113.5\n'
        rep = self.report(text, mask=True)
        sample = rep["clusters"][0]["sample"]
        for secret in ("ava@example.com", "4242 4242", "203.0.113.5"):
            self.assertNotIn(secret, sample)
        self.assertIn("<email>", sample)
        self.assertIn("<card>", sample)

    def test_markdown_rendering(self):
        with TempDir() as d:
            an = lc.analyze([write_log(d, "a.log", LOG)])
            rep = lc.build_report(an, buckets=6)
        md = lc.render_markdown(rep, "warning")
        self.assertTrue(md.startswith("# Log clusters\n"))
        self.assertIn("| # | Level | Count | First seen | Last seen | Rate | Signature |", md)
        self.assertIn("`shop.pay: gateway timeout after <n>ms for order <n>`", md)
        self.assertIn("## Samples", md)
        self.assertIn("Level filter: warning and above", md)
        self.assertNotIn("\r", md)

    def test_markdown_escapes_table_breakers(self):
        text = "2026-11-12 14:00:00 ERROR a: left | right `code`\n"
        with TempDir() as d:
            md = lc.render_markdown(lc.build_report(lc.analyze([write_log(d, "a.log", text)])))
        row = [l for l in md.splitlines() if l.startswith("| 1 |")][0]
        self.assertIn("left \\| right 'code'", row)

    def test_markdown_for_empty_result(self):
        with TempDir() as d:
            an = lc.analyze([write_log(d, "a.log", LOG)], min_level="critical")
            md = lc.render_markdown(lc.build_report(an), "critical")
        self.assertIn("No records matched.", md)


class CliTests(unittest.TestCase):
    def test_json_output(self):
        with TempDir() as d:
            path = write_log(d, "a.log", LOG)
            code, out, err = run_main(lc, [path, "--json", "--level", "error", "--top", "2"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["shown"], 2)
        self.assertEqual(data["clusters"][0]["level"], "error")

    def test_markdown_default_and_out_file_has_lf(self):
        with TempDir() as d:
            path = write_log(d, "a.log", LOG)
            target = d + "/report.md"
            code, out, _ = run_main(lc, [path, "-o", target, "--ascii"])
            with open(target, "rb") as handle:
                data = handle.read()
        self.assertEqual((code, out), (0, ""))
        self.assertNotIn(b"\r\n", data)
        self.assertTrue(data.startswith(b"# Log clusters"))

    def test_out_creates_folders_and_never_overwrites_the_input(self):
        with TempDir() as d:
            path = write_log(d, "a.log", LOG)
            code, out, _ = run_main(lc, [path, "-o", d + "/new/sub/report.md"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(d) / "new" / "sub" / "report.md").exists())
            code, _, err = run_main(lc, [path, "-o", path])
            self.assertEqual(code, 2)
            self.assertIn("refusing to overwrite", err)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), LOG.encode("utf-8"))

    def test_errors_exit_2(self):
        code, _, err = run_main(lc, ["/no/such.log"])
        self.assertEqual(code, 2)
        self.assertIn("cannot read", err)
        with TempDir() as d:
            path = write_log(d, "a.log", LOG)
            self.assertEqual(run_main(lc, [path, "--tz", "Nope/Zone"])[0], 2)
            self.assertEqual(run_main(lc, [path, "--since", "whenever"])[0], 2)
            self.assertEqual(run_main(lc, [path, "--top", "0"])[0], 2)
            self.assertEqual(run_main(lc, [path, "--fail-on-new"])[0], 2)
            self.assertEqual(run_main(lc, [path, "--level", "loud"])[0], 2)

    def test_fail_on_new(self):
        with TempDir() as d:
            before = write_log(d, "before.log", "2026-11-12 10:00:00 ERROR a: old\n")
            same = write_log(d, "same.log", "2026-11-12 11:00:00 ERROR a: old\n")
            worse = write_log(d, "worse.log", "2026-11-12 11:00:00 ERROR a: old\n2026-11-12 11:00:01 ERROR a: new\n")
            mild = write_log(d, "mild.log", "2026-11-12 11:00:00 ERROR a: old\n2026-11-12 11:00:01 WARNING a: new warn\n")
            self.assertEqual(run_main(lc, [same, "--baseline", before, "--fail-on-new"])[0], 0)
            self.assertEqual(run_main(lc, [worse, "--baseline", before, "--fail-on-new"])[0], 1)
            self.assertEqual(run_main(lc, [mild, "--baseline", before, "--fail-on-new"])[0], 0)

    def test_stdin(self):
        with mock.patch("sys.stdin", io.StringIO(LOG)):
            code, out, _ = run_main(lc, ["-", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["records"], 7)

    def test_since_until_flags(self):
        with TempDir() as d:
            path = write_log(d, "a.log", LOG)
            code, out, _ = run_main(lc, [path, "--json", "--since", "2026-11-12T14:01:30Z", "--until", "2026-11-12T14:03:00Z"])
        self.assertEqual(json.loads(out)["counted"], 2)

    def test_emit_writes_utf8_and_lf(self):
        class Fake(object):
            def __init__(self):
                self.buffer = io.BytesIO()

            def flush(self):
                pass

            def write(self, text):
                raise AssertionError("should use the buffer")

        fake = Fake()
        with mock.patch("sys.stdout", fake):
            lc.emit("a\u2588b\nc")
        self.assertEqual(fake.buffer.getvalue(), "a\u2588b\nc\n".encode("utf-8"))


class ExampleLogTests(unittest.TestCase):
    def test_example_web_log_clusters(self):
        an = lc.analyze([str(LOGS / "web.jsonl")], min_level="error")
        rep = lc.build_report(an, top=3)
        self.assertEqual(rep["clusters"][0]["signature"].split(":")[0], "checkout failed")
        self.assertGreater(rep["clusters"][0]["count"], 100)

    def test_example_diff(self):
        an = lc.analyze([str(LOGS / "after.log")])
        base = lc.analyze([str(LOGS / "before.log")])
        rep = lc.build_report(an, base=base)
        self.assertEqual(rep["summary"], {"new": 2, "spiking": 1, "steady": 3, "down": 1, "vanished": 1})


if __name__ == "__main__":
    unittest.main()
