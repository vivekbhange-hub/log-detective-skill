"""Tests for skills/logdet-trace/trace_group.py (trace detection, root cause, grouping, CLI)."""
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from _load import LOGS, TempDir, load, run_main, write_log

tg = load("logdet-trace", "trace_group")

PY_SIMPLE = """2026-11-12 14:03:01,100 ERROR shop.worker: job 17 failed
Traceback (most recent call last):
  File "/app/shop/jobs.py", line 40, in run
    do_work(job)
  File "/app/shop/work.py", line 12, in do_work
    return items[idx]
IndexError: list index out of range
2026-11-12 14:03:02,000 INFO shop.worker: next job
"""

PY_CHAINED = """2026-11-12 14:03:01,100 ERROR shop.worker: sync failed
Traceback (most recent call last):
  File "/app/shop/db.py", line 41, in connect
    return psycopg2.connect(dsn)
  File "/usr/lib/python3.12/site-packages/psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn)
psycopg2.OperationalError: too many clients already

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/shop/sync.py", line 88, in run_batch
    conn = db.connect()
  File "/app/shop/db.py", line 47, in connect
    raise DatabaseUnavailable("cannot open connection")
shop.db.DatabaseUnavailable: cannot open connection
2026-11-12 14:03:02,000 INFO next
"""

PY_FROM = """Traceback (most recent call last):
  File "/app/a.py", line 1, in f
    g()
KeyError: 'x'

The above exception was the direct cause of the following exception:

Traceback (most recent call last):
  File "/app/b.py", line 2, in h
    raise ValueError("bad") from exc
ValueError: bad
"""

JAVA = """2026-11-12 14:03:01,100 [http-1] ERROR com.acme.ChargeService - charge failed for order 7
com.acme.GatewayException: Gateway call failed after 3 attempts
\tat com.acme.GatewayClient.charge(GatewayClient.java:112)
\tat com.acme.ChargeService.charge(ChargeService.java:58)
\tat org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:884)
\tat java.base/java.lang.Thread.run(Thread.java:1583)
Caused by: java.net.SocketTimeoutException: Read timed out
\tat java.base/sun.nio.ch.NioSocketImpl.timedRead(NioSocketImpl.java:278)
\tat org.apache.http.impl.io.SocketInputBuffer.fillBuffer(SocketInputBuffer.java:82)
\tat com.acme.GatewayClient.post(GatewayClient.java:147)
\tat com.acme.GatewayClient.charge(GatewayClient.java:104)
\t... 6 more
2026-11-12 14:03:02,000 [http-1] INFO com.acme.X - next
"""

KOTLIN_SUPPRESSED = """Exception in thread "main" java.lang.IllegalStateException: boom
\tat com.acme.Main.run(Main.kt:10)
\tSuppressed: java.io.IOException: close failed
\t\tat com.acme.Res.close(Res.kt:5)
Caused by: java.lang.NullPointerException
\tat com.acme.Main.init(Main.kt:3)
"""

NODE = """2026-11-12T14:03:01.100Z error: unhandled
TypeError: Cannot read properties of undefined (reading 'id')
    at getOrder (/srv/app/src/orders.js:42:17)
    at async handler (/srv/app/src/routes/checkout.js:18:5)
    at Layer.handle [as handle_request] (/srv/app/node_modules/express/lib/router/layer.js:95:5)
    at process.processTicksAndRejections (node:internal/process/task_queues:95:5) {
  code: 'E_FOO'
}
2026-11-12T14:03:02.000Z info: next
"""

NODE_CAUSE = """Error: request failed
    at call (/srv/app/src/client.js:10:3)
    at run (/srv/app/src/main.js:4:1) {
  [cause]: Error: ECONNREFUSED 203.0.113.9:5432
      at connect (/srv/app/src/db.js:7:9)
}
"""

GO = """2026/11/12 14:03:01 starting
panic: runtime error: index out of range [5] with length 3

goroutine 1 [running]:
main.(*Server).handle(0xc000123456, {0x7a1b20, 0xc000234567})
\t/app/cmd/server/handler.go:88 +0x1f5
main.main()
\t/app/cmd/server/main.go:21 +0x65
goroutine 7 [chan receive]:
main.worker()
\t/app/cmd/server/worker.go:10 +0x25
exit status 2
2026/11/12 14:03:05 after
"""

GO_RUNTIME_FRAMES = """panic: assignment to entry in nil map

goroutine 5 [running]:
panic({0x4a1c20, 0x4d5fb0})
\t/usr/local/go/src/runtime/panic.go:884 +0x213
main.(*Cache).Set(0x0, {0x4b1d3a, 0x3}, 0x1)
\t/app/cache.go:31 +0x45
created by main.start in goroutine 1
\t/app/main.go:12 +0x66
"""

NET = """2026-11-12 14:03:01,000 ERROR Shop.Orders - failed
System.InvalidOperationException: Order failed
 ---> System.Data.SqlClient.SqlException (0x80131904): Timeout expired
   at System.Data.SqlClient.SqlConnection.OnError(SqlException exception) in /_/SqlConnection.cs:line 100
   at Shop.Data.OrderRepo.Load(Int32 id) in C:\\src\\Shop\\Data\\OrderRepo.cs:line 42
   --- End of inner exception stack trace ---
   at Shop.Orders.OrderService.Place(Order o) in C:\\src\\Shop\\Orders\\OrderService.cs:line 77
   at Shop.Web.CheckoutController.Post() in C:\\src\\Shop\\Web\\CheckoutController.cs:line 20
2026-11-12 14:03:02,000 INFO ok
"""


def scan(text, prefixes=(), **kw):
    with TempDir() as d:
        return tg.scan([write_log(d, "a.log", text)], list(prefixes), **kw)


def only(groups):
    assert len(groups) == 1, list(groups)
    return list(groups.values())[0]


class PythonTests(unittest.TestCase):
    def test_simple_traceback(self):
        groups, stats = scan(PY_SIMPLE)
        g = only(groups)
        self.assertEqual(stats["traces"], 1)
        self.assertEqual(g.key[0], "IndexError")
        self.assertEqual(g.root, "IndexError: list index out of range")
        self.assertIsNone(g.surfaced)
        self.assertEqual([f.where() for f in g.frames], ["/app/shop/work.py:12", "/app/shop/jobs.py:40"])  # innermost first
        self.assertEqual(g.context, "shop.worker: job 17 failed")
        self.assertEqual(tg.fmt_ts(g.first), "2026-11-12T14:03:01.100Z")
        self.assertEqual(g.first_line, 2)

    def test_chained_exception_groups_by_root_cause(self):
        groups, stats = scan(PY_CHAINED)
        g = only(groups)
        self.assertEqual(stats["traces"], 1)
        self.assertEqual(g.key[0], "psycopg2.OperationalError")           # what originally broke
        self.assertEqual(g.surfaced, "shop.db.DatabaseUnavailable: cannot open connection")
        self.assertEqual(g.frames[0].where(), "/app/shop/db.py:41")          # library frame skipped
        self.assertEqual(len(g.frames), 3)
        self.assertEqual(g.frames[-1].func, "run_batch")
        self.assertIn("During handling of the above exception", g.sample)

    def test_direct_cause_chain(self):
        g = only(scan(PY_FROM)[0])
        self.assertEqual(g.key[0], "KeyError")
        self.assertEqual(g.surfaced, "ValueError: bad")

    def test_identical_traces_form_one_group_with_counts_and_times(self):
        second = PY_SIMPLE.replace("14:03:01,100", "14:09:00,500")
        groups, stats = scan(PY_SIMPLE + second)
        g = only(groups)
        self.assertEqual((g.count, stats["traces"]), (2, 2))
        self.assertEqual(tg.fmt_ts(g.first), "2026-11-12T14:03:01.100Z")
        self.assertEqual(tg.fmt_ts(g.last), "2026-11-12T14:09:00.500Z")

    def test_different_lines_or_types_are_different_groups(self):
        other_line = PY_SIMPLE.replace("line 12", "line 13")
        other_type = PY_SIMPLE.replace("IndexError", "KeyError")
        groups, _ = scan(PY_SIMPLE + other_line + other_type)
        self.assertEqual(len(groups), 3)

    def test_traceback_without_trailing_newline_at_eof(self):
        groups, stats = scan(PY_SIMPLE.rstrip("\n").rsplit("\n", 1)[0])
        self.assertEqual(stats["traces"], 1)

    def test_truncated_traceback_is_ignored(self):
        groups, stats = scan("Traceback (most recent call last):\n  File \"/a.py\", line 1, in f\n")
        self.assertEqual(stats["traces"], 0)

    def test_multiline_exception_message(self):
        text = ("Traceback (most recent call last):\n  File \"/app/a.py\", line 1, in f\n    g()\n"
                "RuntimeError: first line\n    second line\n    third line\nnext log line\n")
        g = only(scan(text)[0])
        self.assertEqual(g.key[0], "RuntimeError")
        self.assertIn("second line", g.sample)


class JavaDotnetTests(unittest.TestCase):
    def test_java_root_cause_is_innermost_caused_by(self):
        groups, stats = scan(JAVA, ["com.acme"])
        g = only(groups)
        self.assertEqual(stats["formats"], {"java": 1})
        self.assertEqual(g.key[0], "java.net.SocketTimeoutException")
        self.assertEqual(g.root, "java.net.SocketTimeoutException: Read timed out")
        self.assertEqual(g.surfaced, "com.acme.GatewayException: Gateway call failed after 3 attempts")
        self.assertEqual([f.where() for f in g.frames], ["GatewayClient.java:147", "GatewayClient.java:104", "GatewayClient.java:112"])
        self.assertEqual(g.context, "com.acme.ChargeService - charge failed for order 7")
        self.assertEqual(tg.fmt_ts(g.first), "2026-11-12T14:03:01.100Z")

    def test_java_default_library_detection_without_prefix(self):
        g = only(scan(JAVA)[0])
        funcs = [f.func for f in g.frames]
        self.assertNotIn("org.springframework.web.servlet.FrameworkServlet.service", funcs)
        self.assertNotIn("org.apache.http.impl.io.SocketInputBuffer.fillBuffer", funcs)
        self.assertTrue(all(f.startswith("com.acme") for f in funcs))

    def test_suppressed_is_not_the_root_and_its_frames_are_ignored(self):
        g = only(scan(KOTLIN_SUPPRESSED, ["com.acme"])[0])
        self.assertEqual(g.key[0], "java.lang.NullPointerException")
        self.assertEqual(g.surfaced, "java.lang.IllegalStateException: boom")
        self.assertNotIn("com.acme.Res.close", [f.func for f in g.frames])
        self.assertEqual(g.frames[0].where(), "Main.kt:3")

    def test_dotnet_inner_exception_is_the_root(self):
        groups, stats = scan(NET, ["Shop"])
        g = only(groups)
        self.assertEqual(stats["formats"], {"dotnet": 1})
        self.assertEqual(g.key[0], "System.Data.SqlClient.SqlException")
        self.assertEqual(g.surfaced, "System.InvalidOperationException: Order failed")
        self.assertEqual(g.frames[0].where(), "C:\\src\\Shop\\Data\\OrderRepo.cs:42")
        self.assertEqual(g.frames[0].func, "Shop.Data.OrderRepo.Load")
        # frames after "End of inner exception stack trace" belong to the outer exception and still count
        self.assertIn("Shop.Orders.OrderService.Place", [f.func for f in g.frames])

    def test_frames_without_a_header_still_form_a_trace(self):
        text = "\tat com.acme.Foo.bar(Foo.java:10)\n\tat com.acme.Foo.baz(Foo.java:20)\n"
        g = only(scan(text, ["com.acme"])[0])
        self.assertEqual(g.key[0], "(unknown exception)")

    def test_two_back_to_back_java_traces(self):
        groups, stats = scan(JAVA + JAVA.replace("14:03:01", "14:04:01"), ["com.acme"])
        self.assertEqual(stats["traces"], 2)
        self.assertEqual(only(groups).count, 2)


class NodeGoTests(unittest.TestCase):
    def test_node_trace_skips_node_modules_and_internals(self):
        groups, stats = scan(NODE)
        g = only(groups)
        self.assertEqual(stats["formats"], {"node": 1})
        self.assertEqual(g.key[0], "TypeError")
        self.assertEqual(g.root, "TypeError: Cannot read properties of undefined (reading 'id')")
        self.assertEqual([f.where() for f in g.frames], ["/srv/app/src/orders.js:42", "/srv/app/src/routes/checkout.js:18"])
        self.assertEqual(tg.fmt_ts(g.first), "2026-11-12T14:03:01.100Z")

    def test_node_cause(self):
        g = only(scan(NODE_CAUSE, ["/srv/app/src"])[0])
        self.assertEqual(g.key[0], "Error")
        self.assertIn("ECONNREFUSED", g.root)
        self.assertEqual(g.frames[0].where(), "/srv/app/src/db.js:7")

    def test_go_panic(self):
        groups, stats = scan(GO, ["/app/"])
        g = only(groups)
        self.assertEqual(stats["formats"], {"go": 1})
        self.assertEqual(g.key[0], "panic: runtime error: index out of range [<n>] with length <n>")
        self.assertEqual(g.root, "panic: runtime error: index out of range [5] with length 3")
        self.assertEqual([f.func for f in g.frames], ["main.(*Server).handle", "main.main"])   # first goroutine only
        self.assertEqual(g.frames[0].where(), "/app/cmd/server/handler.go:88")
        self.assertEqual(tg.fmt_ts(g.first), "2026-11-12T14:03:01Z")

    def test_go_runtime_frames_are_library(self):
        g = only(scan(GO_RUNTIME_FRAMES)[0])
        self.assertEqual(g.key[0], "panic: assignment to entry in nil map")
        self.assertEqual(g.frames[0].where(), "/app/cache.go:31")
        self.assertNotIn("panic", [f.func for f in g.frames])

    def test_bare_goroutine_dump(self):
        text = "goroutine 1 [running]:\nmain.main()\n\t/app/main.go:5 +0x1\n"
        g = only(scan(text, ["/app"])[0])
        self.assertEqual(g.key[0], "goroutine dump")

    def test_sigterm_line_without_frames_is_not_a_trace(self):
        groups, stats = scan("SIGTERM: received, shutting down\nnext line\n")
        self.assertEqual(stats["traces"], 0)


class PrefixAndClassificationTests(unittest.TestCase):
    def frame(self, file=None, func=None):
        return tg.Frame(func, file, "1", "python")

    def test_default_library_paths(self):
        for path in ("/usr/lib/python3.12/site-packages/x.py", "/usr/lib/python3.12/threading.py",
                     "/srv/app/node_modules/express/index.js", "/app/vendor/lib/x.go", "node:internal/x",
                     "<frozen importlib._bootstrap>", "/usr/local/go/src/runtime/panic.go",
                     "C:\\Python312\\Lib\\threading.py", "/go/pkg/mod/github.com/x/y@v1/z.go"):
            self.assertTrue(tg.is_lib(self.frame(path, "f")), path)
        for func in ("java.util.List.add", "jdk.internal.Foo.bar", "org.springframework.Foo.bar", "kotlin.io.Foo",
                     "System.Linq.Enumerable.First", "runtime.gopanic", "io.netty.channel.X.y"):
            self.assertTrue(tg.is_lib(self.frame("X.java", func)), func)
        self.assertFalse(tg.is_lib(self.frame("/app/shop/orders.py", "place")))

    def test_without_prefix_everything_else_is_app(self):
        self.assertTrue(tg.is_app(self.frame("/app/shop/orders.py", "place"), []))
        self.assertFalse(tg.is_app(self.frame("/x/site-packages/y.py", "f"), []))

    def test_prefix_matches_path_start_contained_dir_or_function(self):
        f = tg.is_app
        self.assertTrue(f(self.frame("src/orders.py", "place"), ["src/"]))
        self.assertTrue(f(self.frame("/app/src/orders.py", "place"), ["src/"]))
        self.assertTrue(f(self.frame("Foo.java", "com.acme.Foo.bar"), ["com.acme"]))
        self.assertTrue(f(self.frame("C:\\work\\src\\a.py", "g"), ["src/"]))
        self.assertFalse(f(self.frame("/app/lib/other.py", "g"), ["src/"]))
        self.assertFalse(f(self.frame("/app/shop/x.py", "g"), ["src/", "com.acme"]))

    def test_library_paths_win_over_a_contained_prefix(self):
        self.assertFalse(tg.is_app(self.frame("/app/node_modules/pkg/src/index.js", "f"), ["src/"]))
        self.assertTrue(tg.is_app(self.frame("src/own.js", "f"), ["src/"]))

    def test_prefix_changes_the_grouping_frame(self):
        text = PY_CHAINED
        with_prefix = only(scan(text, ["shop/sync.py"])[0])
        self.assertEqual(with_prefix.frames[0].func, "run_batch")
        default = only(scan(text)[0])
        self.assertEqual(default.frames[0].func, "connect")

    def test_library_only_trace_is_reported_as_such(self):
        text = ("Traceback (most recent call last):\n"
                '  File "/usr/lib/python3.12/site-packages/lib/a.py", line 1, in f\n    g()\n'
                "RuntimeError: inside lib\n")
        g = only(scan(text)[0])
        self.assertTrue(g.lib_only)
        self.assertEqual(g.frames, [])
        self.assertTrue(g.key[1].startswith("(no application frame)"))
        report = tg.build_report(*scan(text), ["x.log"], [])
        self.assertFalse(report["groups"][0]["application_frame_found"])
        md = tg.render_markdown(report)
        self.assertIn("No application frame found", md)
        self.assertIn("only library or runtime frames", md)


class JsonEmbeddedTests(unittest.TestCase):
    def test_stack_inside_a_json_field(self):
        line = json.dumps({"ts": "2026-11-12T14:03:01.5Z", "level": "error", "msg": "unhandled",
                           "stack": "TypeError: x is undefined\n    at f (/srv/app/src/a.js:3:4)\n    at g (/srv/app/src/b.js:5:6)"})
        groups, stats = scan(line + "\n" + line.replace("14:03:01.5", "14:04:00") + "\n")
        g = only(groups)
        self.assertEqual((g.count, stats["traces"]), (2, 2))
        self.assertEqual(g.key[0], "TypeError")
        self.assertEqual(tg.fmt_ts(g.first), "2026-11-12T14:03:01.500Z")
        self.assertEqual(g.context, "unhandled")

    def test_python_traceback_in_a_json_field(self):
        tb = 'Traceback (most recent call last):\n  File "/app/a.py", line 3, in f\n    g()\nValueError: bad'
        line = json.dumps({"level": "error", "message": "failed", "exc_info": tb, "traceback": tb})
        groups, _ = scan(line + "\n")
        self.assertEqual(only(groups).key[0], "ValueError")

    def test_json_line_without_a_stack_is_ignored(self):
        groups, stats = scan('{"level":"error","msg":"plain"}\n')
        self.assertEqual(stats["traces"], 0)


class FilterTests(unittest.TestCase):
    TWO = PY_SIMPLE + PY_SIMPLE.replace("14:03:01,100", "14:09:00,500")

    def test_since_until(self):
        groups, stats = scan(self.TWO, since=tg.parse_when("2026-11-12T14:05:00Z"))
        self.assertEqual((only(groups).count, stats["time_filtered"]), (1, 1))
        groups, stats = scan(self.TWO, until=tg.parse_when("2026-11-12T14:05:00Z"))
        self.assertEqual(only(groups).count, 1)

    def test_time_filter_drops_traces_without_a_timestamp(self):
        groups, stats = scan(PY_FROM, since=1.0)
        self.assertEqual(groups, {})
        self.assertEqual(stats["time_filtered"], 1)

    def test_max_groups_overflow(self):
        text = "".join(PY_SIMPLE.replace("line 12", "line %d" % (100 + i)) for i in range(6))
        groups, stats = scan(text, max_groups=3)
        self.assertEqual((len(groups), stats["overflow"]), (3, 3))

    def test_naive_timestamps_use_tz(self):
        groups, _ = scan(PY_SIMPLE, tz=tg.TzSpec("+01:00"))
        self.assertEqual(tg.fmt_ts(only(groups).first), "2026-11-12T13:03:01.100Z")


class ReportTests(unittest.TestCase):
    def report(self, text, prefixes=(), **kw):
        groups, stats = scan(text, prefixes)
        return tg.build_report(groups, stats, ["a.log"], list(prefixes), **kw)

    def test_ordering_and_top(self):
        text = PY_SIMPLE * 3 + PY_FROM + JAVA
        rep = self.report(text, top=2)
        self.assertEqual(rep["group_count"], 3)
        self.assertEqual(rep["shown"], 2)
        self.assertEqual(rep["groups"][0]["count"], 3)
        self.assertEqual(rep["groups"][0]["exception"], "IndexError")

    def test_min_count(self):
        rep = self.report(PY_SIMPLE * 3 + PY_FROM, min_count=2)
        self.assertEqual([g["exception"] for g in rep["groups"]], ["IndexError"])

    def test_json_fields(self):
        rep = self.report(PY_CHAINED, ["shop/"])
        g = rep["groups"][0]
        self.assertEqual(g["exception"], "psycopg2.OperationalError")
        self.assertEqual(g["top_app_frames"][0], {"file": "/app/shop/db.py", "line": 41, "function": "connect"})
        self.assertTrue(g["application_frame_found"])
        self.assertEqual(g["format"], "python")
        json.dumps(rep)

    def test_redact(self):
        text = PY_SIMPLE.replace("IndexError: list index out of range", "ValueError: bad user ava@example.com at 203.0.113.9")
        g = self.report(text, mask=True)["groups"][0]
        self.assertNotIn("ava@example.com", json.dumps(g))
        self.assertNotIn("203.0.113.9", json.dumps(g))

    def test_markdown(self):
        md = tg.render_markdown(self.report(JAVA, ["com.acme"]))
        self.assertTrue(md.startswith("# Stack trace groups\n"))
        self.assertIn("| # | Count | Exception | First seen | Last seen | Top application frame |", md)
        self.assertIn("- Root cause: `java.net.SocketTimeoutException: Read timed out`", md)
        self.assertIn("- Surfaced as: `com.acme.GatewayException", md)
        self.assertIn("1. `GatewayClient.java:147` in `com.acme.GatewayClient.post`", md)
        self.assertIn("frames matching `com.acme`", md)
        self.assertNotIn("\r", md)

    def test_markdown_without_traces(self):
        md = tg.render_markdown(self.report("just a normal log\n"))
        self.assertIn("No stack traces found.", md)


class CliTests(unittest.TestCase):
    def test_json_and_files(self):
        with TempDir() as d:
            a = write_log(d, "a.log", PY_SIMPLE)
            b = write_log(d, "b.log", JAVA)
            code, out, _ = run_main(tg, [a, b, "--json", "--app-prefix", "com.acme"])
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["traces"], 2)
        self.assertEqual(data["app_prefixes"], ["com.acme"])

    def test_out_file_is_lf(self):
        with TempDir() as d:
            a = write_log(d, "a.log", PY_SIMPLE)
            code, out, _ = run_main(tg, [a, "-o", d + "/t.md"])
            with open(d + "/t.md", "rb") as handle:
                data = handle.read()
        self.assertEqual((code, out), (0, ""))
        self.assertNotIn(b"\r\n", data)

    def test_out_creates_folders_and_never_overwrites_the_input(self):
        with TempDir() as d:
            path = write_log(d, "a.log", PY_SIMPLE)
            code, out, _ = run_main(tg, [path, "-o", d + "/new/sub/report.md"])
            self.assertEqual(code, 0)
            self.assertTrue((Path(d) / "new" / "sub" / "report.md").exists())
            code, _, err = run_main(tg, [path, "-o", path])
            self.assertEqual(code, 2)
            self.assertIn("refusing to overwrite", err)
            with open(path, "rb") as handle:
                self.assertEqual(handle.read(), PY_SIMPLE.encode("utf-8"))

    def test_errors_exit_2(self):
        self.assertEqual(run_main(tg, ["/no/such.log"])[0], 2)
        with TempDir() as d:
            a = write_log(d, "a.log", PY_SIMPLE)
            self.assertEqual(run_main(tg, [a, "--since", "never"])[0], 2)
            self.assertEqual(run_main(tg, [a, "--top", "0"])[0], 2)
            self.assertEqual(run_main(tg, [a, "--tz", "Mars/Base"])[0], 2)

    def test_stdin(self):
        with mock.patch("sys.stdin", io.StringIO(PY_SIMPLE)):
            code, out, _ = run_main(tg, ["-", "--json"])
        self.assertEqual(json.loads(out)["traces"], 1)

    def test_gzip(self):
        import gzip
        with TempDir() as d:
            path = d + "/a.log.gz"
            with gzip.open(path, "wb") as f:
                f.write(PY_SIMPLE.encode("utf-8"))
            code, out, _ = run_main(tg, [path, "--json"])
        self.assertEqual(json.loads(out)["traces"], 1)


class ExampleLogTests(unittest.TestCase):
    def test_example_logs(self):
        groups, stats = tg.scan([str(LOGS / "worker.log"), str(LOGS / "payments.log")], ["shop/", "com.fernhill"])
        by_type = {g.key[0]: g for g in groups.values()}
        self.assertEqual(set(by_type), {"psycopg2.OperationalError", "java.net.SocketTimeoutException",
                                        "smtplib.SMTPServerDisconnected"})
        self.assertEqual(by_type["psycopg2.OperationalError"].count, 40)
        self.assertEqual(by_type["java.net.SocketTimeoutException"].count, 4)
        self.assertEqual(by_type["psycopg2.OperationalError"].surfaced, "shop.db.DatabaseUnavailable: cannot open connection")


if __name__ == "__main__":
    unittest.main()
