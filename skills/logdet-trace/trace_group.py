#!/usr/bin/env python3
"""trace_group: find stack traces in a log and group them by what broke and where.

Understands Python tracebacks (including chained "During handling of the above exception"), Java and
Kotlin traces with "Caused by:", Node/JS `at ...` frames (with `[cause]:`), Go panics and goroutine
dumps, and .NET traces (with `--->` inner exceptions). Traces inside JSON log fields (`stack`,
`stack_trace`, `traceback`, ...) are found too.

Each trace is reduced to its root cause: the innermost exception of the chain, and the first
application frame of it (frames from libraries, vendored code and runtimes are skipped). Traces with
the same root exception type and application frame form one group. For each group it prints the
count, first and last seen, the root-cause line, the top three application frames with file:line
and one sample.

  trace_group.py app.log --app-prefix src/ --app-prefix com.acme
  trace_group.py a.log b.log --json --top 5

Standard library only, streams the file, deterministic output, Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import deque

# ---- begin shared core: keep this block identical in log_cluster.py, trace_group.py and timeline.py ----
import gzip
import itertools
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

LEVELS = ("debug", "info", "warning", "error", "critical")
LEVEL_RANK = {name: i for i, name in enumerate(LEVELS)}
MAX_LINE = 20000
MAX_CONTINUATION = 400
_LEVEL_WORDS = {
    "trace": "debug", "debug": "debug", "dbg": "debug", "info": "info", "information": "info",
    "notice": "info", "warn": "warning", "warning": "warning", "error": "error", "err": "error",
    "severe": "error", "fatal": "critical", "critical": "critical", "crit": "critical",
    "panic": "critical", "emerg": "critical", "emergency": "critical", "alert": "critical",
}
_NUMERIC_LEVELS = {10: "debug", 20: "debug", 30: "info", 40: "warning", 50: "error", 60: "critical"}


def norm_level(value):
    """Map a level word or a pino/bunyan number (10..60) to one of LEVELS, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return _NUMERIC_LEVELS.get(int(value))
    if isinstance(value, str):
        word = value.strip().lower()
        if word.isdigit():
            return _NUMERIC_LEVELS.get(int(word))
        return _LEVEL_WORDS.get(word)
    return None


# --------------------------------------------------------------------------- time

class TzSpec(object):
    """Timezone for timestamps that carry none: UTC, +05:30, -0700 or an IANA name."""

    def __init__(self, text="UTC"):
        self.text = (text or "UTC").strip()
        self.fixed = 0
        self.zone = None
        if self.text.upper() in ("UTC", "Z", "GMT", ""):
            return
        m = re.match(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", self.text, re.I)
        if m:
            seconds = int(m.group(2)) * 3600 + int(m.group(3) or 0) * 60
            self.fixed = seconds if m.group(1) == "+" else -seconds
            return
        try:
            from zoneinfo import ZoneInfo
            self.zone = ZoneInfo(self.text)
        except Exception:
            raise ValueError("unknown timezone %r (use UTC, +05:30 or an IANA name)" % text)

    def offset(self, y, mo, d, h, mi, s):
        if self.zone is None:
            return self.fixed
        off = datetime(y, mo, d, h, mi, min(s, 59), tzinfo=self.zone).utcoffset()
        return int(off.total_seconds()) if off else 0


UTC = TzSpec("UTC")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"))}
_ISO = re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})[T ](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,9}))?"
                  r"(\s?(?:Z\b|UTC\b|GMT\b|[+-]\d{2}:?\d{2}\b))?")
_APACHE = re.compile(r"(\d{2})/([A-Z][a-z]{2})/(\d{4}):(\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})")
_SYSLOG = re.compile(r"\b([A-Z][a-z]{2}) {1,2}(\d{1,2}) (\d{2}):(\d{2}):(\d{2})\b")
_EPOCH_START = re.compile(r"^\W{0,2}(\d{13}|\d{10}(?:\.\d{1,9})?)\b")


_days_cache = {}
_ts_cache = {}


def _days_from_civil(y, m, d):
    key = (y, m, d)
    cached = _days_cache.get(key)
    if cached is not None:
        return cached
    days = _days_uncached(y, m, d)
    if len(_days_cache) < 20000:
        _days_cache[key] = days
    return days


def _days_uncached(y, m, d):
    y -= 1 if m <= 2 else 0
    era = (y if y >= 0 else y - 399) // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146097 + doe - 719468


def _epoch(y, mo, d, h, mi, s, frac, offset):
    if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s <= 60):
        return None
    return _days_from_civil(y, mo, d) * 86400 + h * 3600 + mi * 60 + s + frac - offset


def epoch_from_number(value):
    """Seconds, milliseconds, microseconds or nanoseconds since 1970 -> seconds, or None."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x >= 1e17:
        x /= 1e9
    elif x >= 1e14:
        x /= 1e6
    elif x >= 1e11:
        x /= 1e3
    return x if 946684800 <= x <= 4102444800 else None


def find_timestamp(text, tz=UTC, year=None, max_pos=None):
    """Find the first timestamp in text. Returns (epoch_seconds, start, end) or None.

    Understands ISO-8601/RFC 3339 (also `2026/11/12 14:03:01`, with `,` or `.` fractions, Z/UTC/offset),
    Apache `[10/Oct/2000:13:55:36 -0700]`, syslog `Nov 12 14:03:01` (year from `year`, default: this
    year) and a leading epoch in seconds or milliseconds. Naive times use `tz`.
    """
    m = _ISO.search(text)
    if m and (max_pos is None or m.start() <= max_pos):
        zone = m.group(8)
        offset = None
        if zone:
            zone = zone.strip()
            offset = 0 if zone in ("Z", "UTC", "GMT") else \
                (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[-2:]) * 60)
        elif tz.zone is None:
            offset = tz.fixed
        base = None
        key = None
        if offset is not None:
            key = (text[m.start():m.start() + 19], offset)
            base = _ts_cache.get(key)
        if base is None:
            y, mo, d, h, mi, s = map(int, m.group(1, 2, 3, 4, 5, 6))
            if offset is None:
                offset = tz.offset(y, mo, d, h, mi, s)
            base = _epoch(y, mo, d, h, mi, s, 0.0, offset)
            if base is not None and key is not None and len(_ts_cache) < 50000:
                _ts_cache[key] = base
        if base is not None:
            digits = m.group(7)
            return (base + int(digits) / float(10 ** len(digits)) if digits else base), m.start(), m.end()
    m = _APACHE.search(text)
    if m and (max_pos is None or m.start() <= max_pos) and m.group(2) in _MONTHS:
        zone = m.group(7)
        offset = (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[3:5]) * 60)
        value = _epoch(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)), int(m.group(4)),
                       int(m.group(5)), int(m.group(6)), 0.0, offset)
        if value is not None:
            return value, m.start(), m.end()
    m = _SYSLOG.search(text)
    if m and (max_pos is None or m.start() <= max_pos) and m.group(1) in _MONTHS:
        y = year if year else datetime.now(timezone.utc).year
        mo, d, h, mi, s = _MONTHS[m.group(1)], int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        value = _epoch(y, mo, d, h, mi, s, 0.0, tz.offset(y, mo, d, h, mi, s))
        if value is not None:
            return value, m.start(), m.end()
    m = _EPOCH_START.match(text)
    if m:
        value = epoch_from_number(m.group(1))
        if value is not None:
            return value, m.start(1), m.end(1)
    return None


def fmt_ts(epoch, ms=None):
    """Format epoch seconds as 2026-11-12T14:03:01Z (milliseconds shown when ms is True, or when
    ms is None and the value has a fractional part)."""
    total_ms = int(round(epoch * 1000))
    seconds, millis = divmod(total_ms, 1000)
    base = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds)
    if ms is None:
        ms = millis != 0
    text = base.strftime("%Y-%m-%dT%H:%M:%S")
    return text + (".%03d" % millis if ms else "") + "Z"


def parse_when(text, tz=UTC, year=None):
    """Parse a user-supplied instant: ISO-8601 (seconds, minutes or date only), RFC 3339, epoch."""
    t = text.strip()
    hit = find_timestamp(t, tz, year)
    if hit and hit[1] == 0:
        return hit[0]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2}))?(\s?(?:Z|UTC|[+-]\d{2}:?\d{2}))?$", t)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        h, mi = int(m.group(4) or 0), int(m.group(5) or 0)
        zone = (m.group(6) or "").strip()
        if zone:
            offset = 0 if zone in ("Z", "UTC") else (1 if zone[0] == "+" else -1) * (int(zone[1:3]) * 3600 + int(zone[-2:]) * 60)
        else:
            offset = tz.offset(y, mo, d, h, mi, 0)
        value = _epoch(y, mo, d, h, mi, 0, 0.0, offset)
        if value is not None:
            return value
    if re.match(r"^\d+(?:\.\d+)?$", t):
        value = epoch_from_number(t)
        if value is not None:
            return value
    raise ValueError("cannot parse time %r (use e.g. 2026-11-12T14:03:00Z)" % text)


def parse_duration(text):
    """'90s', '10m', '2h', '1h30m', '1d' or a plain number of seconds -> seconds (float)."""
    t = text.strip().lower()
    if re.match(r"^\d+(?:\.\d+)?$", t):
        return float(t)
    if not re.match(r"^(?:\d+(?:\.\d+)?[smhdw])+$", t):
        raise ValueError("cannot parse duration %r (use e.g. 90s, 10m, 2h, 1h30m)" % text)
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    return float(sum(float(n) * unit[u] for n, u in re.findall(r"(\d+(?:\.\d+)?)([smhdw])", t)))


# --------------------------------------------------------------------------- records

class Rec(object):
    """One log record: the first line plus any continuation lines (stack trace, wrapped text)."""
    __slots__ = ("ts", "level", "inferred", "msg", "lines", "lineno", "fmt", "hint", "sig", "stack",
                 "truncated")

    def __init__(self, ts, level, inferred, msg, line, fmt, sig=None, stack=None):
        self.ts = ts
        self.level = level
        self.inferred = inferred
        self.msg = msg
        self.lines = [line]
        self.lineno = 0
        self.fmt = fmt
        self.hint = None
        self.sig = sig
        self.stack = stack
        self.truncated = 0

    @property
    def raw(self):
        return "\n".join(self.lines)


_JSON_LEVEL = ("level", "levelname", "severity", "lvl", "log.level", "loglevel", "log_level",
               "severity_text", "levelno")
_JSON_MSG = ("msg", "message", "event", "text", "log", "body", "short_message", "@message", "summary")
_JSON_TS = ("ts", "time", "timestamp", "@timestamp", "t", "datetime", "asctime", "date", "eventtime",
            "created")
_JSON_ERR = ("error", "err", "exception", "exc_info", "error.message", "error_message", "reason")
_JSON_STACK = ("stack", "stacktrace", "stack_trace", "traceback", "error.stack_trace", "exc_text",
               "error.stack")
_ERR_WORDS = re.compile(r"\b(?:fatal|panic|traceback|exception|error|errors|failed|failure|unable to|"
                        r"cannot|refused|denied|timed out|timeout|critical)\b", re.I)
_WARN_WORDS = re.compile(r"\b(?:warn|warning|deprecated|retry|retrying|slow|degraded)\b", re.I)
_LEVEL_TOKEN = re.compile(r"(?i)(?<![\w.])(trace|debug|info|notice|warn(?:ing)?|error|err|severe|fatal|"
                          r"critical|crit|panic|emerg|alert)(?![\w.])")
_LEAD_LEVEL = re.compile(r"^\s*[\[(<]?(TRACE|DEBUG|INFO|NOTICE|WARN(?:ING)?|ERROR|ERR|SEVERE|FATAL|"
                         r"CRITICAL|CRIT|PANIC)[\])>]?(?::|\s|$)")
_ACCESS = re.compile(r'^(\S+) \S+ (\S+) \[([^\]]+)\] "([^"]*)" (\d{3}) (\S+)')
_SYSLOG_LINE = re.compile(r"^(?:<\d{1,3}>)?[A-Z][a-z]{2} {1,2}\d{1,2} \d{2}:\d{2}:\d{2} (\S+) "
                          r"([^\s:\[]+)(?:\[\d+\])?: (.*)$")
_LOGFMT_HINT = re.compile(r"(?:^|\s)(?:level|lvl|msg)=")
_KV = re.compile(r'([\w.@-]+)=("(?:[^"\\]|\\.)*"|\S*)')
_CONT_LINE = re.compile(r"^(?:\s|Traceback \(most recent call last\)|Caused by:|\.\.\. \d+ (?:more|common)|"
                        r"goroutine \d+ \[|During handling of the above|The above exception was)")
_ID_SEG = re.compile(r"^(?:\d+|[0-9a-fA-F]{8,}|[A-Za-z0-9_.-]*\d[A-Za-z0-9_.-]{3,})$")
_PY_EXC = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure|Timeout|Iteration))"
                     r"(?::|\s*$)")
_JAVA_EXC = re.compile(r"^(?:Caused by: |Exception in thread \"[^\"]*\" )?((?:[A-Za-z_$][\w$]*\.)+"
                       r"[A-Za-z_$][\w$]*(?:Exception|Error|Throwable))(?::|\s*$)")


def guess_level(msg):
    """Level for a line that does not state one: keyword based, flagged as inferred by callers."""
    if _ERR_WORDS.search(msg):
        return "error"
    if _WARN_WORDS.search(msg):
        return "warning"
    return "info"


def _err_text(value):
    if isinstance(value, str):
        return value.strip().split("\n", 1)[0][:300]
    if isinstance(value, dict):
        kind = value.get("type") or value.get("class") or value.get("name") or ""
        text = value.get("message") or value.get("msg") or ""
        return ("%s: %s" % (kind, text) if kind and text else str(kind or text))[:300]
    if isinstance(value, list) and value:
        return _err_text(value[0])
    return "" if value is None or value is False else str(value)[:300]


def route_signature(path):
    """/api/orders/123?x=1 -> /api/orders/<id>: keeps the route, drops the query and ids."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    return "/".join("<id>" if seg and _ID_SEG.match(seg) else seg for seg in path.split("/"))


def _parse_json(s, tz, year):
    try:
        obj = json.loads(s)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    low = {}
    for key, value in obj.items():
        low.setdefault(str(key).lower(), value)

    def pick(names):
        for name in names:
            if name in low:
                return low[name]
            if "." in name:
                cur = obj
                for part in name.split("."):
                    if isinstance(cur, dict) and part in cur:
                        cur = cur[part]
                    else:
                        break
                else:
                    return cur
        return None

    ts = None
    raw_ts = pick(_JSON_TS)
    if isinstance(raw_ts, (int, float)) and not isinstance(raw_ts, bool):
        ts = epoch_from_number(raw_ts)
    elif isinstance(raw_ts, str):
        hit = find_timestamp(raw_ts, tz, year)
        ts = hit[0] if hit else epoch_from_number(raw_ts)
    level = norm_level(pick(_JSON_LEVEL))
    raw_msg = pick(_JSON_MSG)
    msg = raw_msg if isinstance(raw_msg, str) else ("" if raw_msg is None else json.dumps(raw_msg)[:200])
    err_value = pick(_JSON_ERR)
    err = _err_text(err_value)
    if msg and err and err not in msg:
        msg = msg + ": " + err
    elif err and not msg:
        msg = err
    elif not msg:
        msg = s[:200]
    msg = msg.strip().split("\n", 1)[0]
    stack = pick(_JSON_STACK)
    if not isinstance(stack, str) and isinstance(err_value, dict):
        stack = err_value.get("stack") or err_value.get("stack_trace")
    stack = stack if isinstance(stack, str) and "\n" in stack else None
    inferred = False
    if level is None:
        level = "error" if err or stack else guess_level(msg)
        inferred = True
    return Rec(ts, level, inferred, msg, s, "json", stack=stack)


def _parse_logfmt(s, tz, year):
    kv = {}
    for m in _KV.finditer(s):
        value = m.group(2)
        if value.startswith('"'):
            value = value[1:-1].replace('\\"', '"')
        kv.setdefault(m.group(1).lower(), value)
    level = norm_level(kv.get("level") or kv.get("lvl") or kv.get("severity"))
    msg = kv.get("msg") or kv.get("message")
    err = kv.get("err") or kv.get("error")
    ts = None
    for key in ("time", "ts", "timestamp", "t"):
        if key in kv:
            hit = find_timestamp(kv[key], tz, year)
            ts = hit[0] if hit else epoch_from_number(kv[key])
            if ts is not None:
                break
    if msg is None and level is None:
        return None
    if msg is None:
        msg = s
    if err and err not in msg:
        msg = msg + ": " + err
    inferred = level is None
    if level is None:
        level = "error" if err else guess_level(msg)
    return Rec(ts, level, inferred, msg, s, "logfmt")


_LEVEL_NEXT = re.compile(r"[\s\[(<]*(trace|debug|info|notice|warn(?:ing)?|error|err|severe|fatal|critical|crit|"
                         r"panic|emerg|alert)(?![\w.])", re.I)


def _split_level(s, start, end):
    """Find the level word just after a timestamp; return (level, message)."""
    m = _LEVEL_NEXT.match(s, end)
    if m:
        return _LEVEL_WORDS.get(m.group(1).lower()), s[m.end():].lstrip(" \t:-|])>")
    head = s[end:end + 90]
    m = _LEVEL_TOKEN.search(head)
    if m and len(head[:m.start()].split()) <= 3:
        return norm_level(m.group(1)), s[end + m.end():].lstrip(" \t:-|])>")
    return None, s[end:].lstrip(" \t:-|]")


def parse_start(line, tz=UTC, year=None, lines_mode=False):
    """Return a Rec if `line` starts a new record, None if it continues the previous one.

    Recognises JSON lines, logfmt, nginx/apache access logs, syslog, and text lines with a timestamp
    and/or a level word (Python logging, Log4j, Go, Node and similar). In lines_mode (logs with no
    timestamps) every non-indented line is a record.
    """
    s = line
    if not s.strip():
        return None
    first = s[0]
    if first == "{":
        rec = _parse_json(s, tz, year)
        if rec is not None:
            return rec
    if first in " \t":
        return None
    m = _ACCESS.match(s) if '"' in s and "[" in s else None
    if m and (m.group(4).split(" ", 1)[0].isalpha() or m.group(4) in ("-", "")):
        hit = find_timestamp(m.group(3), tz, year)
        parts = m.group(4).split(" ")
        method = parts[0] if parts and parts[0] else "-"
        path = parts[1] if len(parts) > 1 else "-"
        status = int(m.group(5))
        level = "error" if status >= 500 else ("warning" if status >= 400 else "info")
        rec = Rec(hit[0] if hit else None, level, False,
                  "%s %s %d from %s" % (method, path, status, m.group(1)), s, "access",
                  sig="%s %s %d" % (method, route_signature(path), status))
        return rec
    m = _SYSLOG_LINE.match(s) if (first == "<" or s[3:4] == " ") else None
    if m:
        hit = find_timestamp(s, tz, year, max_pos=20)
        if hit:
            rest = m.group(3)
            level = None
            lm = _LEAD_LEVEL.match(rest)
            if lm:
                level = norm_level(lm.group(1))
            rec = Rec(hit[0], level or guess_level(rest), level is None, "%s: %s" % (m.group(2), rest), s,
                      "syslog")
            return rec
    hit = find_timestamp(s, tz, year, max_pos=40)
    if hit:
        if _LOGFMT_HINT.search(s):
            rec = _parse_logfmt(s, tz, year)
            if rec is not None:
                if rec.ts is None:
                    rec.ts = hit[0]
                return rec
        level, msg = _split_level(s, hit[1], hit[2])
        inferred = level is None
        return Rec(hit[0], level or guess_level(msg), inferred, msg, s, "text")
    if _LOGFMT_HINT.search(s):
        rec = _parse_logfmt(s, tz, year)
        if rec is not None:
            return rec
    m = _LEAD_LEVEL.match(s)
    if m:
        return Rec(None, norm_level(m.group(1)), False, s[m.end():].lstrip(" \t:-|])>"), s, "text")
    if lines_mode and not _CONT_LINE.match(s):
        return Rec(None, guess_level(s), True, s, s, "text")
    return None


def exception_hint(lines):
    """Root exception type found in a record's continuation lines (deepest `Caused by:`, else the
    first exception line, else a Go panic message), or None."""
    first = caused = panic = None
    for text in lines[1:]:
        if not text or text[0] in " \t":
            continue
        if text.startswith("Caused by:"):
            m = _JAVA_EXC.match(text)
            if m:
                caused = m.group(1)
        elif text.startswith("panic:"):
            panic = panic or normalize(text)[:120]
        elif first is None:
            m = _PY_EXC.match(text) or _JAVA_EXC.match(text)
            if m:
                first = m.group(1)
    return caused or first or panic


def _finish(rec):
    while len(rec.lines) > 1 and not rec.lines[-1].strip():
        rec.lines.pop()
    if len(rec.lines) > 1:
        rec.hint = exception_hint(rec.lines)
        if rec.hint and rec.level in ("info", "warning", "debug") and rec.inferred:
            rec.level = "error"
    if rec.stack and rec.hint is None:
        rec.hint = exception_hint(rec.stack.split("\n"))
    return rec


def iter_records(lines, tz=UTC, year=None, sniff=300):
    """Yield Rec objects from an iterable of text lines, joining continuation lines to the record
    they follow. Rec.lineno is the 1-based line number of the record's first line."""
    iterator = iter(lines)
    probe = list(itertools.islice(iterator, sniff))
    seen = starts = 0
    for line in probe:
        if line.strip() and line[0] not in " \t":
            seen += 1
            if parse_start(line.rstrip("\r\n")[:MAX_LINE], tz, year) is not None:
                starts += 1
    lines_mode = seen > 0 and starts < 0.3 * seen
    current = None
    lineno = 0
    for line in itertools.chain(probe, iterator):
        lineno += 1
        text = line.rstrip("\r\n")[:MAX_LINE]
        if not text.strip():
            if current is not None and not lines_mode and len(current.lines) < MAX_CONTINUATION:
                current.lines.append("")
            continue
        rec = parse_start(text, tz, year, lines_mode)
        if rec is None:
            if current is not None:
                if len(current.lines) < MAX_CONTINUATION:
                    current.lines.append(text)
                else:
                    current.truncated += 1
                continue
            rec = Rec(None, guess_level(text), True, text.strip(), text, "text")
        rec.lineno = lineno
        if current is not None:
            yield _finish(current)
        current = rec
    if current is not None:
        yield _finish(current)


def open_log(path):
    """Open a log for reading as text: '-' is stdin, *.gz is decompressed; bad bytes are replaced."""
    if path == "-":
        stream = sys.stdin
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
        return stream
    if path.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8-sig", errors="replace")


def write_report(path, text, inputs=()):
    """Write the report to `path` (UTF-8, LF), creating parent folders. Returns an error string if
    `path` is one of the input logs, which must never be overwritten, else None."""
    target = os.path.abspath(path)
    for source in inputs:
        if source != "-" and os.path.abspath(source) == target:
            return "refusing to overwrite the input log %s; choose another -o path" % source
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return None


def emit(text):
    """Write text to stdout as UTF-8 with LF line endings on every platform."""
    out = sys.stdout
    if not text.endswith("\n"):
        text += "\n"
    buffer = getattr(out, "buffer", None)
    if buffer is not None:
        out.flush()
        buffer.write(text.encode("utf-8", "replace"))
        buffer.flush()
    else:
        out.write(text)
        out.flush()


# --------------------------------------------------------------------------- signatures

_Q_DQ = re.compile(r'"[^"\n]{0,200}"')
_Q_SQ = re.compile(r"'[^'\n]{0,200}'")
_N_URL = re.compile(r"[a-z][a-z0-9+.-]{1,10}://([^/\s\"'<>?#:]+)(?::\d+)?[^\s\"'<>]*")
_N_PATH = re.compile(r"/(?:[\w.@%+~=,-]+/)+[\w.@%+~=,-]*")
_N_WINPATH = re.compile(r"(?:[A-Za-z]:\\|\\\\)[^\s\"'<>|]+")
_N_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_N_JWT = re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*")
_N_BEARER = re.compile(r"\b(?i:bearer)\s+[\w.~+/=-]{6,}")
_N_TS = re.compile(r"\d{4}[-/]\d{2}[-/]\d{2}(?:[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|\s?UTC|[+-]\d{2}:?\d{2})?)?")
_N_TIME = re.compile(r"\d{2}:\d{2}:\d{2}(?:[.,]\d+)?")
_N_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_N_IP = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d{1,5})?")
_N_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b|\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{8,}\b")
_N_TOKEN = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{20,}\b")
_N_NUM = re.compile(r"\d+(?:\.\d+)?")
_HAS_DIGIT = re.compile(r"\d")
_HEX8 = re.compile(r"[0-9a-fA-F]{8}|0x[0-9a-fA-F]")
_ALNUM20 = re.compile(r"[A-Za-z0-9]{20}")
_norm_cache = {}


def _quoted(m):
    text = m.group(0)
    inner = text[1:-1]
    if "://" in inner:
        return text[0] + normalize(inner) + text[0]
    return text[0] + "<str>" + text[0]


def _quoted_single(m):
    i, text = m.start(), m.group(0)
    j = m.end()
    if (i and m.string[i - 1].isalnum()) or (j < len(m.string) and m.string[j].isalnum()):
        return text          # an apostrophe inside a word, not a quote
    return _quoted(m)


def _url(m):
    full = m.group(0)
    rest = full.split("://", 1)[1]
    return "%s://%s%s" % (full.split("://", 1)[0], m.group(1), "/<path>" if "/" in rest or "?" in rest else "")


def _path(m):
    i = m.start()
    if i and (m.string[i - 1].isalnum() or m.string[i - 1] in "_/<>.:-"):
        return m.group(0)    # part of a longer token such as and/or or a date, not a path
    return "<path>"


def _ip(m):
    i = m.start()
    if i and (m.string[i - 1].isalnum() or m.string[i - 1] == "."):
        return m.group(0)
    if any(int(p) > 255 for p in re.split(r"[.:]", m.group(0))[:4]):
        return m.group(0)
    return "<ip>"


def _num(m):
    i = m.start()
    return m.group(0) if i and m.string[i - 1].isalpha() else "<n>"


def normalize(text):
    """Reduce a log message to its signature: numbers, ids, UUIDs, IPs, hex, paths, quoted values,
    timestamps, emails and tokens become placeholders, so repeats of one event compare equal."""
    cached = _norm_cache.get(text)
    if cached is not None:
        return cached
    s = text
    if '"' in s:
        s = _Q_DQ.sub(_quoted, s)
    if "'" in s:
        s = _Q_SQ.sub(_quoted_single, s)
    if "://" in s:
        s = _N_URL.sub(_url, s)
    if "/" in s:
        s = _N_PATH.sub(_path, s)
    if "\\" in s:
        s = _N_WINPATH.sub("<path>", s)
    if "@" in s:
        s = _N_EMAIL.sub("<email>", s)
    if "eyJ" in s:
        s = _N_JWT.sub("<jwt>", s)
    if "earer" in s or "EARER" in s:
        s = _N_BEARER.sub("Bearer <token>", s)
    if _HAS_DIGIT.search(s):
        if "-" in s or "/" in s:
            s = _N_TS.sub("<ts>", s)
        if ":" in s:
            s = _N_TIME.sub("<time>", s)
        if _HEX8.search(s):
            if "-" in s:
                s = _N_UUID.sub("<uuid>", s)
            s = _N_HEX.sub("<hex>", s)
        if "." in s:
            s = _N_IP.sub(_ip, s)
        if _ALNUM20.search(s):
            s = _N_TOKEN.sub("<token>", s)
        s = _N_NUM.sub(_num, s)
    result = " ".join(s.split())[:240]
    if len(_norm_cache) > 50000:
        _norm_cache.clear()
    _norm_cache[text] = result
    return result


def signature(rec):
    """Cluster key for a record: its normalised message plus the root exception type, if any."""
    base = rec.sig if rec.sig is not None else normalize(rec.msg)
    return "%s [%s]" % (base, rec.hint) if rec.hint else base


# --------------------------------------------------------------------------- redaction

_REDACT = [
    (re.compile(r"(?i)\b(authorization)(\s*[:=]\s*)(?:bearer\s+|basic\s+|token\s+)?[^\s,;\"']+"),
     r"\1\2<redacted>"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{6,}"), "Bearer <redacted>"),
    (re.compile(r"\beyJ[\w-]{5,}\.[\w-]{5,}\.[\w-]*"), "<jwt>"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|access[_-]?key|session(?:id)?|"
                r"cookie)([\"']?\s*[:=]\s*[\"']?)([^\s\"',;&]+)"), r"\1\2<redacted>"),
    (re.compile(r"\b(?:(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
                r"xox[abprs]-[A-Za-z0-9-]{10,})\b"), "<token>"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    (re.compile(r"\b[0-9a-fA-F]{32,}\b"), "<token>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "<ip>"),
    (re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){4,7}[0-9a-fA-F]{1,4}\b"), "<ip>"),
]
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")


def _luhn(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def _card_sub(m):
    digits = re.sub(r"\D", "", m.group(0))
    return "<card>" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)


def redact(text):
    """Mask emails, tokens, JWTs, bearer/authorization headers, secrets in key=value pairs, IPs and
    card-like numbers (13-19 digits passing the Luhn check)."""
    for pattern, replacement in _REDACT:
        text = pattern.sub(replacement, text)
    return _CARD.sub(_card_sub, text)


# --------------------------------------------------------------------------- rates

class RateCounter(object):
    """Event counts per time slot. Memory stays bounded: when more than `limit` slots exist the
    slot width doubles (1 s, 2 s, 4 s, ...) and neighbouring slots merge."""
    __slots__ = ("res", "data", "limit")

    def __init__(self, limit=512):
        self.res = 1
        self.data = {}
        self.limit = limit

    def add(self, epoch, n=1):
        key = int(epoch // self.res)
        self.data[key] = self.data.get(key, 0) + n
        if len(self.data) > self.limit:
            while len(self.data) > self.limit:
                merged = {}
                for k, v in self.data.items():
                    merged[k // 2] = merged.get(k // 2, 0) + v
                self.data = merged
                self.res *= 2

    def histogram(self, start, end, buckets):
        """Counts in `buckets` equal slices of [start, end]."""
        start = math.floor(start)
        span = max(math.floor(end) + 1 - start, 1)
        out = [0] * buckets
        for k, v in self.data.items():
            i = int(((k + 0.5) * self.res - start) / span * buckets)
            out[min(max(i, 0), buckets - 1)] += v
        return out

    def rebucket(self, width):
        """{bucket_start_epoch: count} using slots of at least `width` seconds."""
        width = max(int(width), self.res)
        out = {}
        for k, v in self.data.items():
            b = (k * self.res) // width * width
            out[b] = out.get(b, 0) + v
        return out


_SPARK = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_SPARK_ASCII = "_.:-=+*#"


def sparkline(counts, ascii_only=False):
    """One character per bucket, scaled to the largest bucket; empty buckets are the lowest mark."""
    marks = _SPARK_ASCII if ascii_only else _SPARK
    top = max(counts) if counts else 0
    if top <= 0:
        return marks[0] * len(counts)
    return "".join(marks[0] if c <= 0 else marks[max(1, int(math.ceil(c / float(top) * 7)))] for c in counts)
# ---- end shared core ----

DEFAULT_LIB_PATH = re.compile(
    r"(?:site-packages|dist-packages|node_modules|(?:^|/)vendor/|/lib/python\d|/python\d[\d.]*/lib/|"
    r"(?:^|/)Lib/|<frozen |^<|^node:|^internal/|/usr/local/go/|/usr/lib/go|GOROOT|/pkg/mod/|"
    r"^(?:/usr/lib|/opt/homebrew)/)", re.I)
DEFAULT_LIB_FUNC = re.compile(
    r"^(?:java\.|javax\.|jakarta\.|jdk\.|sun\.|com\.sun\.|kotlin\.|kotlinx\.|scala\.|groovy\.|"
    r"org\.springframework\.|org\.apache\.|org\.hibernate\.|org\.eclipse\.|org\.jboss\.|io\.netty\.|"
    r"reactor\.|org\.junit\.|System\.|Microsoft\.|runtime\.|runtime/|net/http\.|reflect\.|syscall\.|"
    r"panic$|internal/|node:)")
_PY_FRAME = re.compile(r'^\s+File "(.+?)", line (\d+)(?:, in (.*))?$')
_AT_LINE = re.compile(r"^\s+at\s+(.+?)\s*$")
_JAVA_FRAME = re.compile(r"^([\w$./<>@-]+)\((?:Native Method|Unknown Source|([\w$.-]+\.(?:java|kt|kts|scala|"
                         r"groovy|clj))(?::(\d+))?)\)$")
_NET_FRAME = re.compile(r"^([\w`<>.+$\[\],]+)\((.*?)\)(?: in (.+?):line (\d+))?$")
_NODE_FRAME = re.compile(r"^(?:async )?(?:new )?(?:(.+?) \()?(.+?):(\d+):(\d+)\)?$")
_CAUSE = re.compile(r"^\s*(?:Caused by:\s+|\[cause\]:\s+|---> )(.+)$")
_SUPPRESSED = re.compile(r"^\s*Suppressed:\s+(.+)$")
_MORE = re.compile(r"^\s*\.\.\. \d+ (?:more|common frames omitted)\s*$")
_NET_END = re.compile(r"^\s*--- End of inner exception stack trace ---\s*$")
_EXC_HEAD = re.compile(r"^(?:Uncaught |Unhandled (?:\w+ )?)?(?:Exception in thread \"[^\"]*\" )?"
                       r"(?:\(node:\d+\) )?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)(?::\s?(.*))?$")
_EXC_FQN = re.compile(r"((?:[a-z_][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable))(?::\s*(.*))?$")
_EXC_SUFFIX = re.compile(r"(?:Error|Exception|Throwable|Fault|Failure|Panic|Timeout|Abort|Interrupt|Rejection)$")
_PREFIX_LEVEL = re.compile(r"^\s*[\[(<]?(?:TRACE|DEBUG|INFO|NOTICE|WARN|WARNING|ERROR|SEVERE|FATAL|CRITICAL)"
                           r"[\])>]?[:\s-]+")
_PY_EXC_LINE = re.compile(r"^([\w.]+)(?::\s?(.*))?$")
_GO_HEAD = re.compile(r"^(panic: .+|fatal error: .+|SIG[A-Z]+: .+)$")
_GO_GOROUTINE = re.compile(r"^goroutine (\d+) \[([^\]]*)\]:?$")
_GO_FILE = re.compile(r"^\s+(\S+\.go):(\d+)(?: \+0x[0-9a-fA-F]+)?$")
_GO_FUNC = re.compile(r"^(?:created by )?([\w./*()\-]+(?:\.[\w*()\-]+)*)(?:\(.*\))?(?: in goroutine \d+)?$")


class Frame(object):
    __slots__ = ("func", "file", "line", "kind")

    def __init__(self, func, file, line, kind):
        self.func = func
        self.file = file
        self.line = line
        self.kind = kind

    def where(self):
        if self.file and self.line:
            return "%s:%s" % (self.file, self.line)
        return self.file or self.func or "?"


class Exc(object):
    __slots__ = ("type", "message", "frames", "shown")

    def __init__(self, type_, message, shown=None):
        self.type = type_
        self.message = message or ""
        self.frames = []
        self.shown = shown

    def line(self):
        if self.shown:
            return self.shown[:300]
        return ("%s: %s" % (self.type, self.message) if self.message else self.type)[:300]


class Trace(object):
    __slots__ = ("fmt", "chain", "lineno", "ts", "context", "lines")

    def __init__(self, fmt, lineno, ts, context):
        self.fmt = fmt
        self.chain = []          # outermost exception first, root cause last
        self.lineno = lineno
        self.ts = ts
        self.context = context
        self.lines = []


def strip_prefix(text):
    """Drop a leading timestamp and an upper-case level word from a log line."""
    if text[:1].isdigit() or text[:1] in "[<":
        hit = find_timestamp(text, UTC, 2026, max_pos=40)
        if hit:
            text = text[hit[2]:]
    text = text.lstrip(" \t:-|])")
    m = _PREFIX_LEVEL.match(text)
    return text[m.end():] if m else text


def parse_exc_header(text):
    """(type, message) when the line looks like an exception header, else None."""
    t = strip_prefix(text).strip()
    m = _EXC_HEAD.match(t)
    if m and _EXC_SUFFIX.search(m.group(1).rsplit(".", 1)[-1]):
        return m.group(1), m.group(2) or ""
    m = _EXC_FQN.search(t)
    if m:
        return m.group(1), m.group(2) or ""
    return None


def parse_frame(text):
    """Parse an `at ...` line (Java, .NET or Node) into a Frame, or None if it is not one."""
    m = _AT_LINE.match(text)
    if not m:
        return None
    body = m.group(1)
    if body.endswith(" {"):
        body = body[:-2].rstrip()
    j = _JAVA_FRAME.match(body)
    if j:
        return Frame(j.group(1).split("/")[-1], j.group(2), j.group(3), "java")
    n = _NET_FRAME.match(body)
    if n:
        return Frame(n.group(1), n.group(3), n.group(4), "dotnet")
    k = _NODE_FRAME.match(body)
    if k:
        return Frame(k.group(1) or "<anonymous>", k.group(2), k.group(3), "node")
    return Frame(body, None, None, "node")


class TraceScanner(object):
    """Line-by-line detector. feed() returns the traces that the line completed."""

    def __init__(self):
        self.mode = None
        self.trace = None
        self.ring = deque(maxlen=8)
        self.pending = None
        self.py_stage = None
        self.py_frames = None
        self.py_segments = None
        self.cur = None
        self.stack = None
        self.sink = None
        self.goroutines = 0
        self.go_frame_open = None

    # ---- context from the log lines just before the trace
    def _context(self, tz, year):
        ts, msg = None, None
        for _, text in reversed(self.ring):
            rec = parse_start(text, tz, year)
            if rec is not None and rec.ts is not None:
                return rec.ts, rec.msg[:200]
            if rec is not None and msg is None:
                msg = rec.msg[:200]
        return ts, msg

    def _begin(self, fmt, lineno, tz, year):
        ts, msg = self._context(tz, year)
        self.trace = Trace(fmt, lineno, ts, msg)
        self.mode = fmt

    def _end(self):
        done, self.trace, self.mode = self.trace, None, None
        self.cur = self.stack = self.sink = None
        self.py_stage = self.py_frames = self.py_segments = None
        self.goroutines = 0
        if done is not None and done.fmt == "go" and not any(e.frames for e in done.chain):
            return None
        if done is not None and done.fmt == "frames":
            kinds = [f.kind for e in done.chain for f in e.frames]
            done.fmt = kinds[0] if kinds else "frames"
        if done is not None and done.fmt == "python":
            segments = done.chain
            done.chain = list(reversed(segments))
        return done if done is not None and done.chain else None

    def finish(self):
        done = self._end() if self.trace is not None else None
        return [done] if done else []

    def feed(self, text, lineno, tz=UTC, year=None):
        out = []
        for _ in range(2):          # a line that ends a trace is then handled as an ordinary line
            if self.mode == "python":
                if self._py(text):
                    return out
            elif self.mode == "frames":
                if self._frames(text):
                    return out
            elif self.mode == "go":
                if self._go(text):
                    return out
            else:
                if self._idle(text, lineno, tz, year):
                    return out
                break
            done = self._end()
            if done is not None:
                out.append(done)
        if text and text[0] not in " \t" and text.strip():
            self.ring.append((lineno, text))
        return out

    # ---- idle: look for the start of a trace
    def _idle(self, text, lineno, tz, year):
        if text.rstrip().endswith("Traceback (most recent call last):"):
            self._begin("python", lineno, tz, year)
            self.trace.lines.append(text)
            self.py_stage = "frames"
            self.py_frames = []
            self.py_segments = []
            self.trace.chain = self.py_segments
            return True
        stripped = strip_prefix(text) if text[:1] not in " \t" else text
        goroutine = _GO_GOROUTINE.match(text)
        if goroutine or _GO_HEAD.match(stripped.strip()):
            if goroutine:
                header = ("goroutine dump", "")
            else:
                head = stripped.strip()
                header = (normalize(head)[:160], head)
            self._begin("go", lineno, tz, year)
            exc = Exc(header[0], "", header[1] or header[0])
            self.trace.chain = [exc]
            self.cur = exc
            self.trace.lines.append(text)
            if goroutine:
                self.goroutines = 1
            return True
        inner = _CAUSE.match(text) if self.pending is not None and text.lstrip().startswith("--->") else None
        if inner:
            self._begin("frames", lineno, tz, year)
            outer = Exc(*self.pending[:2])
            head = parse_exc_header(inner.group(1)) or _loose_header(inner.group(1))
            exc = Exc(head[0], head[1])
            self.trace.chain = [outer, exc]
            self.cur = exc
            self.stack = [outer, exc]
            self.trace.lines.extend([self.pending[2], text])
            self.pending = None
            return True
        frame = parse_frame(text)
        if frame is not None:
            self._begin("frames", lineno, tz, year)
            if self.pending is not None:
                exc = Exc(*self.pending[:2])
                self.trace.lines.append(self.pending[2])
            else:
                exc = Exc("(unknown exception)", "")
            self.trace.chain = [exc]
            self.cur = exc
            self.stack = [exc]
            self.cur.frames.append(frame)
            self.trace.lines.append(text)
            self.pending = None
            return True
        self.pending = None
        if text and text[0] not in " \t":
            head = parse_exc_header(text)
            if head:
                self.pending = (head[0], head[1], text)
        return False

    # ---- python
    def _py(self, text):
        t = self.trace
        if self.py_stage == "frames":
            m = _PY_FRAME.match(text)
            if m:
                self.py_frames.append(Frame(m.group(3) or "<module>", m.group(1), m.group(2), "python"))
                t.lines.append(text)
                return True
            if not text.strip() or text[0] in " \t":
                t.lines.append(text)
                return True
            m = _PY_EXC_LINE.match(text.strip())
            exc = Exc(m.group(1), m.group(2)) if m else Exc(text.strip()[:80], "")
            exc.frames = list(reversed(self.py_frames))
            self.py_segments.append(exc)
            self.py_frames = []
            t.lines.append(text)
            self.py_stage = "after"
            return True
        if self.py_stage == "after":
            if not text.strip() or text[0] in " \t":
                t.lines.append(text)
                return True
            if text.startswith(("During handling of the above exception", "The above exception was the direct cause")):
                t.lines.append(text)
                self.py_stage = "sep"
                return True
            return False
        if self.py_stage == "sep":
            if not text.strip():
                t.lines.append(text)
                return True
            if text.rstrip().endswith("Traceback (most recent call last):"):
                t.lines.append(text)
                self.py_stage = "frames"
                self.py_frames = []
                return True
        return False

    # ---- java / .NET / node
    def _frames(self, text):
        t = self.trace
        frame = parse_frame(text)
        if frame is not None:
            if self.sink is not None:
                self.sink.append(frame)
            else:
                self.cur.frames.append(frame)
            t.lines.append(text)
            return True
        if _MORE.match(text):
            t.lines.append(text)
            return True
        if _NET_END.match(text):
            if self.stack and len(self.stack) > 1:
                self.stack.pop()
                self.cur = self.stack[-1]
            t.lines.append(text)
            return True
        m = _SUPPRESSED.match(text)
        if m:
            self.sink = []
            t.lines.append(text)
            return True
        m = _CAUSE.match(text)
        if m:
            head = parse_exc_header(m.group(1)) or _loose_header(m.group(1))
            exc = Exc(head[0], head[1])
            t.chain.append(exc)
            self.cur = exc
            self.sink = None
            if text.lstrip().startswith("--->"):
                self.stack.append(exc)
            t.lines.append(text)
            return True
        if text.strip() and text[0] in " \t" and self.sink is not None:
            t.lines.append(text)
            return True
        return False

    # ---- go
    def _go(self, text):
        t = self.trace
        if not text.strip():
            t.lines.append(text)
            return True
        if text.startswith("[signal") or text.startswith("exit status") or text.startswith("[recovered]"):
            t.lines.append(text)
            return True
        m = _GO_GOROUTINE.match(text)
        if m:
            self.goroutines += 1
            self.go_frame_open = None
            t.lines.append(text)
            return True
        f = _GO_FILE.match(text)
        if f and self.go_frame_open is not None:
            func = self.go_frame_open
            self.go_frame_open = None
            if self.goroutines <= 1:
                self.cur.frames.append(Frame(func, f.group(1), f.group(2), "go"))
            t.lines.append(text)
            return True
        if text[0] not in " \t" and (text.endswith(")") or text.startswith("created by ")) and _GO_FUNC.match(text):
            self.go_frame_open = _go_func_name(text)
            t.lines.append(text)
            return True
        return False


def _go_func_name(text):
    name = text[len("created by "):] if text.startswith("created by ") else text
    name = name.split(" in goroutine ")[0]
    depth = 0
    for i in range(len(name) - 1, -1, -1):          # strip the trailing argument list
        if name[i] == ")":
            depth += 1
        elif name[i] == "(":
            depth -= 1
            if depth == 0:
                if i > 0 and name[i - 1] != ".":
                    return name[:i]
                break
    return name


def _loose_header(text):
    """Header of a `Caused by:` line that did not look like an exception type."""
    m = re.match(r"^([\w$.]+)(?:\s*\(0x[0-9a-fA-F]+\))?(?::\s?(.*))?$", text.strip())
    return (m.group(1), m.group(2) or "") if m else (text.strip()[:80], "")


# --------------------------------------------------------------------------- grouping

def is_lib(frame):
    path = (frame.file or "").replace("\\", "/")
    func = frame.func or ""
    if not path and not func:
        return True
    return bool(DEFAULT_LIB_PATH.search(path) or DEFAULT_LIB_FUNC.match(func))


def is_app(frame, prefixes):
    path = (frame.file or "").replace("\\", "/")
    func = frame.func or ""
    if prefixes:
        lib = is_lib(frame)
        for prefix in prefixes:
            p = prefix.replace("\\", "/")
            if path.startswith(p) or func.startswith(p):
                return True
            if not lib and ("/" + p) in path:
                return True
        return False
    return not is_lib(frame)


def app_frames(trace, prefixes, limit=3):
    """Application frames, innermost first: the root cause's own frames, then the enclosing ones."""
    seen, out = set(), []
    for exc in reversed(trace.chain):
        for frame in exc.frames:
            key = (frame.file, frame.line, frame.func)
            if key in seen:
                continue
            seen.add(key)
            if is_app(frame, prefixes):
                out.append(frame)
                if len(out) >= limit:
                    return out
    return out


def group_key(trace, prefixes):
    root = trace.chain[-1]
    frames = app_frames(trace, prefixes, 1)
    if frames:
        where = "%s in %s" % (frames[0].where(), frames[0].func)
    else:
        first = next((f for exc in reversed(trace.chain) for f in exc.frames), None)
        where = "(no application frame) %s" % (first.where() if first else "?")
    return (root.type, where)


class Group(object):
    __slots__ = ("key", "count", "first", "last", "first_line", "file", "fmt", "root", "surfaced",
                 "frames", "lib_only", "context", "sample")


def add_trace(groups, trace, prefixes, path, max_groups, sample_lines):
    key = group_key(trace, prefixes)
    group = groups.get(key)
    if group is None:
        if len(groups) >= max_groups:
            return False
        group = Group()
        group.key = key
        group.count = 0
        group.first = group.last = None
        group.first_line = trace.lineno
        group.file = path
        group.fmt = trace.fmt
        group.root = trace.chain[-1].line()
        group.surfaced = trace.chain[0].line() if len(trace.chain) > 1 else None
        group.frames = app_frames(trace, prefixes)
        group.lib_only = not group.frames
        group.context = trace.context
        lines = trace.lines[:sample_lines]
        group.sample = "\n".join(l[:300] for l in lines)
        if len(trace.lines) > sample_lines:
            group.sample += "\n... (%d more lines)" % (len(trace.lines) - sample_lines)
        groups[key] = group
    group.count += 1
    if trace.ts is not None:
        if group.first is None or trace.ts < group.first:
            group.first = trace.ts
        if group.last is None or trace.ts > group.last:
            group.last = trace.ts
    return True


def scan(paths, prefixes, tz=UTC, year=None, since=None, until=None, max_groups=5000, sample_lines=40):
    """Scan the logs; returns (groups dict, stats dict)."""
    groups = {}
    stats = {"lines": 0, "traces": 0, "formats": {}, "time_filtered": 0, "overflow": 0, "no_timestamp": 0}

    def take(trace, path):
        if since is not None or until is not None:
            if trace.ts is None or (since is not None and trace.ts < since) or (until is not None and trace.ts >= until):
                stats["time_filtered"] += 1
                return
        if trace.ts is None:
            stats["no_timestamp"] += 1
        stats["traces"] += 1
        stats["formats"][trace.fmt] = stats["formats"].get(trace.fmt, 0) + 1
        if not add_trace(groups, trace, prefixes, path, max_groups, sample_lines):
            stats["overflow"] += 1

    for path in paths:
        scanner = TraceScanner()
        stream = open_log(path)
        try:
            for lineno, raw in enumerate(stream, 1):
                stats["lines"] += 1
                text = raw.rstrip("\r\n")[:MAX_LINE]
                for trace in scanner.feed(text, lineno, tz, year):
                    take(trace, path)
                if text[:1] == "{":
                    rec = _parse_json(text, tz, year)
                    if rec is not None and rec.stack:
                        for extra in rec.stack.split("\n"):
                            for trace in scanner.feed(extra.rstrip("\r")[:MAX_LINE], lineno, tz, year):
                                take(trace, path)
                        for trace in scanner.feed("", lineno, tz, year):
                            take(trace, path)
            for trace in scanner.finish():
                take(trace, path)
        finally:
            if stream is not sys.stdin:
                stream.close()
    return groups, stats


def build_report(groups, stats, paths, prefixes, top=10, min_count=1, mask=False):
    clean = redact if mask else (lambda text: text)
    ordered = sorted((g for g in groups.values() if g.count >= min_count),
                     key=lambda g: (-g.count, g.first if g.first is not None else 0, g.key))
    shown = []
    for index, g in enumerate(ordered[:top], 1):
        shown.append({
            "rank": index,
            "exception": g.key[0],
            "count": g.count,
            "first_seen": fmt_ts(g.first) if g.first is not None else None,
            "last_seen": fmt_ts(g.last) if g.last is not None else None,
            "format": g.fmt,
            "root_cause": clean(g.root),
            "surfaced_as": clean(g.surfaced) if g.surfaced else None,
            "top_app_frames": [{"file": f.file, "line": int(f.line) if f.line else None, "function": f.func}
                               for f in g.frames],
            "application_frame_found": not g.lib_only,
            "group_location": g.key[1],
            "log_message": clean(g.context) if g.context else None,
            "first_line": g.first_line,
            "file": g.file.replace("\\", "/"),
            "sample": clean(g.sample),
        })
    return {
        "files": [p.replace("\\", "/") for p in paths],
        "lines": stats["lines"],
        "traces": stats["traces"],
        "formats": dict(sorted(stats["formats"].items())),
        "group_count": len(groups),
        "shown": len(shown),
        "app_prefixes": list(prefixes),
        "time_filtered": stats["time_filtered"],
        "no_timestamp": stats["no_timestamp"],
        "overflow_traces": stats["overflow"],
        "groups": shown,
    }


def _cell(text):
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def render_markdown(report):
    out = ["# Stack trace groups", ""]
    out.append("- Files: " + ", ".join("`%s`" % f for f in report["files"]))
    formats = ", ".join("%s %d" % (k, v) for k, v in report["formats"].items()) or "none"
    out.append("- Traces found: %d in %d groups (formats: %s)" % (report["traces"], report["group_count"], formats))
    if report["app_prefixes"]:
        out.append("- Application code: frames matching " + ", ".join("`%s`" % p for p in report["app_prefixes"]))
    else:
        out.append("- Application code: every frame not in a library, vendor or runtime path (see --app-prefix)")
    if report["time_filtered"]:
        out.append("- Outside the --since/--until window: %d traces skipped" % report["time_filtered"])
    if report["no_timestamp"]:
        out.append("- Traces with no timestamp nearby: %d" % report["no_timestamp"])
    if report["overflow_traces"]:
        out.append("- %d traces beyond --max-groups were not grouped" % report["overflow_traces"])
    out.append("")
    if not report["groups"]:
        out.append("No stack traces found.")
        return "\n".join(out) + "\n"
    out.append("| # | Count | Exception | First seen | Last seen | Top application frame |")
    out.append("|--:|--:|---|---|---|---|")
    for g in report["groups"]:
        top = g["top_app_frames"][0] if g["top_app_frames"] else None
        where = "`%s:%s` in `%s`" % (top["file"], top["line"], _cell(top["function"])) if top else "none (library frames only)"
        out.append("| %d | %d | `%s` | %s | %s | %s |" % (
            g["rank"], g["count"], _cell(g["exception"]), g["first_seen"] or "-", g["last_seen"] or "-", where))
    out.append("")
    for g in report["groups"]:
        out.append("## %d. %s x%d" % (g["rank"], g["exception"], g["count"]))
        out.append("")
        out.append("- Root cause: `%s`" % _cell(g["root_cause"]))
        if g["surfaced_as"]:
            out.append("- Surfaced as: `%s`" % _cell(g["surfaced_as"]))
        if g["log_message"]:
            out.append("- Log message: `%s`" % _cell(g["log_message"]))
        out.append("- First at `%s:%d` (%s format)" % (g["file"], g["first_line"], g["format"]))
        if g["top_app_frames"]:
            out.append("- Application frames, innermost first:")
            for i, f in enumerate(g["top_app_frames"], 1):
                out.append("  %d. `%s:%s` in `%s`" % (i, f["file"], f["line"], _cell(f["function"])))
        else:
            out.append("- No application frame found: the trace contains only library or runtime frames. "
                       "Pass --app-prefix to say which paths or packages are yours.")
        out.append("")
        out.append("```text")
        out.append(g["sample"].replace("```", "'''"))
        out.append("```")
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def build_parser():
    p = argparse.ArgumentParser(
        prog="trace_group.py",
        description="Find stack traces in logs (Python, Java/Kotlin, Node, Go, .NET) and group them by "
                    "root-cause exception and top application frame.",
        epilog="Exit codes: 0 success, 2 usage or file error.")
    p.add_argument("logs", nargs="+", help="log file(s); '-' reads stdin; .gz is decompressed")
    p.add_argument("--app-prefix", action="append", default=[], metavar="PREFIX",
                   help="path or package prefix that marks your code, e.g. src/ or com.acme; repeatable. "
                        "Without it, every frame outside library/vendor/runtime paths counts as yours")
    p.add_argument("--top", type=int, default=10, help="groups to show (default 10)")
    p.add_argument("--min-count", type=int, default=1, help="hide groups seen fewer times than this")
    p.add_argument("--since", help="only traces at or after this time (ISO-8601)")
    p.add_argument("--until", help="only traces before this time")
    p.add_argument("--redact", action="store_true",
                   help="mask emails, tokens, JWTs, bearer headers, IPs and card-like numbers in output")
    p.add_argument("--tz", default="UTC", help="timezone for timestamps that carry none (default UTC)")
    p.add_argument("--year", type=int, help="year for syslog timestamps (default: this year)")
    p.add_argument("--max-groups", type=int, default=5000, help="cap on distinct groups kept (default 5000)")
    p.add_argument("--sample-lines", type=int, default=40, help="lines kept per sample (default 40)")
    p.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    p.add_argument("-o", "--out", metavar="FILE", help="write the report to FILE instead of stdout")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        tz = TzSpec(args.tz)
        since = parse_when(args.since, tz, args.year) if args.since else None
        until = parse_when(args.until, tz, args.year) if args.until else None
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if args.top < 1 or args.max_groups < 1:
        print("error: --top and --max-groups must be at least 1", file=sys.stderr)
        return 2
    try:
        groups, stats = scan(args.logs, args.app_prefix, tz, args.year, since, until, args.max_groups,
                             args.sample_lines)
    except OSError as exc:
        print("error: cannot read log: %s" % exc, file=sys.stderr)
        return 2
    report = build_report(groups, stats, args.logs, args.app_prefix, args.top, args.min_count, args.redact)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n" if args.json else render_markdown(report)
    if args.out:
        problem = write_report(args.out, text, args.logs)
        if problem:
            print("error: %s" % problem, file=sys.stderr)
            return 2
    else:
        emit(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
