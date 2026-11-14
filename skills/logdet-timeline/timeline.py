#!/usr/bin/env python3
"""timeline: merge logs from several services into one time-ordered incident timeline.

Reads any mix of JSON lines, logfmt, syslog, nginx/apache access logs and plain-text logs (Python,
Java/Log4j, Go, Node). Timestamps are parsed from ISO-8601/RFC 3339, syslog, epoch seconds or
milliseconds and Apache `[10/Oct/2000:13:55:36 -0700]` form and converted to UTC; timestamps that
carry no zone use --tz. Every line is tagged with its source file.

  timeline.py web.jsonl worker.log db.log --around 2026-11-12T14:03:00Z --window 10m
  timeline.py web=web.jsonl nginx-access.log --since 2026-11-12T14:00:00Z --until 2026-11-12T14:20:00Z --json

It also reports the first occurrence of each error signature inside the window (marked new when the
signature was not seen before the window), and error-rate spikes: buckets whose error count is far
above the typical bucket (median + MAD by default, or mean + standard deviation).

Standard library only, streams the files, deterministic output, Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

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

MAX_EVENTS = 200000
MAX_SEEN = 50000


class Source(object):
    def __init__(self, label, path):
        self.label = label
        self.path = path
        self.records = 0
        self.no_ts = 0
        self.in_window = 0
        self.formats = {}
        self.first = None
        self.last = None


def split_source(arg):
    """`label=path` or just `path`."""
    name, sep, rest = arg.partition("=")
    if sep and name and rest and "/" not in name and "\\" not in name and not os.path.exists(arg):
        return name, rest
    base = os.path.basename(arg.replace("\\", "/")) or arg
    for suffix in (".gz", ".log", ".jsonl", ".json", ".txt", ".out"):
        if base.lower().endswith(suffix) and len(base) > len(suffix):
            base = base[:-len(suffix)]
    return base, arg


def make_sources(args):
    sources, used = [], set()
    for arg in args:
        label, path = split_source(arg)
        candidate, n = label, 1
        while candidate in used:
            n += 1
            candidate = "%s-%d" % (label, n)
        used.add(candidate)
        sources.append(Source(candidate, path))
    return sources


def fmt_duration(seconds):
    seconds = int(round(seconds))
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            parts.append("%d%s" % (seconds // size, unit))
            seconds %= size
    return "".join(parts) or "0s"


def nice_width(seconds):
    for step in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, 86400):
        if step >= seconds:
            return step
    return 86400


class Event(object):
    __slots__ = ("ts", "src", "lineno", "rank", "level", "msg", "sig", "nlines", "hint", "repeats", "until",
                 "mark")

    def __init__(self, rec, src, sig):
        self.ts = rec.ts
        self.src = src
        self.lineno = rec.lineno
        self.rank = LEVEL_RANK.get(rec.level, 1)
        self.level = rec.level
        self.msg = rec.msg[:240]
        self.sig = sig
        self.nlines = len(rec.lines) + rec.truncated
        self.hint = rec.hint
        self.repeats = 1
        self.until = rec.ts
        self.mark = None


def collect(sources, tz, year, lo, hi, min_rank, err_rank):
    """One streaming pass: events inside the window, error counts over all time, first occurrences."""
    events, firsts, seen_before = [], {}, set()
    errors = RateCounter(4096)
    span = [None, None]
    truncated = False
    for index, src in enumerate(sources):
        stream = open_log(src.path)
        try:
            for rec in iter_records(stream, tz, year):
                src.records += 1
                src.formats[rec.fmt] = src.formats.get(rec.fmt, 0) + 1
                if rec.ts is None:
                    src.no_ts += 1
                    continue
                ts = rec.ts
                if src.first is None or ts < src.first:
                    src.first = ts
                if src.last is None or ts > src.last:
                    src.last = ts
                if span[0] is None or ts < span[0]:
                    span[0] = ts
                if span[1] is None or ts > span[1]:
                    span[1] = ts
                rank = LEVEL_RANK.get(rec.level, 1)
                is_error = rank >= err_rank
                if is_error:
                    errors.add(ts)
                if ts < lo:
                    if is_error and len(seen_before) < MAX_SEEN:
                        seen_before.add(signature(rec))
                    continue
                if ts >= hi:
                    continue
                src.in_window += 1
                sig = None
                if is_error:
                    sig = signature(rec)
                    entry = firsts.get(sig)
                    if entry is None:
                        event = Event(rec, index, sig)
                        firsts[sig] = [event, 1]
                    else:
                        entry[1] += 1
                if rank >= min_rank:
                    if len(events) >= MAX_EVENTS:
                        truncated = True
                    else:
                        events.append(Event(rec, index, sig or signature(rec)))
        finally:
            if stream is not sys.stdin:
                stream.close()
    return events, firsts, seen_before, errors, span, truncated


def detect_spikes(errors, span, lo, hi, width, method, threshold, min_spike):
    """Error-rate spikes. Baseline = buckets outside the window if there are 8 or more, else all."""
    if span[0] is None or errors.data == {}:
        return [], {"bucket_seconds": width, "baseline_buckets": 0, "baseline_median": 0, "scale": 0}
    width = max(int(width), errors.res)
    counts = errors.rebucket(width)
    first = int(span[0]) // width * width
    last = int(span[1]) // width * width
    n = (last - first) // width + 1
    if n > 200000:
        raise ValueError("--bucket %ds would create %d buckets; use a larger value" % (width, n))
    starts = [first + i * width for i in range(n)]
    series = [counts.get(s, 0) for s in starts]
    outside = [c for s, c in zip(starts, series) if s + width <= lo or s >= hi]
    base = outside if len(outside) >= 8 else series
    if method == "zscore":
        center = statistics.mean(base)
        scale = statistics.pstdev(base) or 1.0
    else:
        center = statistics.median(base)
        mad = statistics.median([abs(c - center) for c in base])
        if mad > 0:
            scale = 1.4826 * mad
        else:
            mean_ad = statistics.mean([abs(c - center) for c in base])
            scale = 1.2533 * mean_ad if mean_ad > 0 else 1.0
    spikes, current = [], None
    for s, c in zip(starts, series):
        in_window = s + width > lo and s < hi
        score = (c - center) / scale
        if in_window and c >= min_spike and score >= threshold:
            if current is not None and s == current["end"]:
                current["end"] = s + width
                current["total"] += c
                if c > current["peak"]:
                    current["peak"], current["peak_score"] = c, score
            else:
                current = {"start": s, "end": s + width, "peak": c, "total": c, "peak_score": score}
                spikes.append(current)
        else:
            current = None
    info = {"bucket_seconds": width, "baseline_buckets": len(base), "baseline_median": round(center, 2),
            "scale": round(scale, 2), "method": method}
    return spikes, info


def fold(events, gap):
    """Merge repeats of the same (source, signature) that follow each other within `gap` seconds."""
    if gap <= 0:
        return events
    out, open_runs = [], {}
    for ev in events:
        key = (ev.src, ev.sig)
        run = open_runs.get(key)
        if run is not None and ev.mark is None and ev.ts - run.until <= gap:
            run.repeats += 1
            run.until = ev.ts
            continue
        open_runs[key] = ev
        out.append(ev)
    return out


def build_report(sources, events, firsts, seen_before, spikes, spike_info, lo, hi, args, truncated, mask):
    clean = redact if mask else (lambda text: text)
    events.sort(key=lambda e: (e.ts, e.src, e.lineno))
    first_list = []
    for sig, (event, count) in firsts.items():
        first_list.append((event, count, sig not in seen_before))
    first_list.sort(key=lambda t: (t[0].ts, t[0].src, t[0].lineno))
    marks = {}
    for event, count, new in first_list:
        marks[(event.src, event.lineno)] = "new" if new else "seen-before"
    for event in events:
        event.mark = marks.get((event.src, event.lineno))
    shown = fold(events, args.fold_gap)
    omitted = 0
    if args.max_lines and len(shown) > args.max_lines:
        keep = [e for e in shown if e.mark is not None]
        budget = max(args.max_lines - len(keep), 0)
        keep_ids = set(id(e) for e in keep)
        for e in shown:
            if budget > 0 and id(e) not in keep_ids:
                keep_ids.add(id(e))
                budget -= 1
        omitted = len(shown) - len(keep_ids)
        shown = [e for e in shown if id(e) in keep_ids]
    bad = [t for t in first_list if t[2]] or first_list
    report = {
        "sources": [{"label": s.label, "file": s.path.replace("\\", "/"), "records": s.records,
                     "records_in_window": s.in_window, "without_timestamp": s.no_ts,
                     "formats": dict(sorted(s.formats.items())),
                     "first": fmt_ts(s.first) if s.first is not None else None,
                     "last": fmt_ts(s.last) if s.last is not None else None} for s in sources],
        "window": {"start": fmt_ts(lo) if lo != float("-inf") else None,
                   "end": fmt_ts(hi) if hi != float("inf") else None,
                   "around": fmt_ts(args._around) if args._around is not None else None,
                   "before_seconds": args._around - lo if args._around is not None else None,
                   "after_seconds": hi - args._around if args._around is not None else None},
        "level_shown": args.level,
        "error_level": args.error_level,
        "events_in_window": len(events),
        "events_shown": len(shown),
        "events_omitted": omitted,
        "events_truncated": truncated,
        "spike": dict(spike_info, threshold=args.threshold, min_spike=args.min_spike),
        "spikes": [{"start": fmt_ts(s["start"]), "end": fmt_ts(s["end"]), "peak": s["peak"], "total": s["total"],
                    "score": round(s["peak_score"], 1)} for s in spikes],
        "first_bad_line": None,
        "first_occurrences": [],
        "events": [],
    }
    for event, count, new in first_list:
        report["first_occurrences"].append({
            "time": fmt_ts(event.ts, True), "source": sources[event.src].label,
            "file": sources[event.src].path.replace("\\", "/"), "line": event.lineno, "level": event.level,
            "message": clean(event.msg), "signature": clean(event.sig), "count_in_window": count,
            "new": new})
    if bad:
        first = bad[0][0]
        report["first_bad_line"] = {
            "time": fmt_ts(first.ts, True), "source": sources[first.src].label, "line": first.lineno,
            "message": clean(first.msg), "new": bad[0][2]}
    for event in shown:
        report["events"].append({
            "time": fmt_ts(event.ts, True), "source": sources[event.src].label,
            "file": sources[event.src].path.replace("\\", "/"), "line": event.lineno, "level": event.level,
            "message": clean(event.msg), "signature": clean(event.sig), "extra_lines": max(event.nlines - 1, 0),
            "root_exception": event.hint, "repeats": event.repeats,
            "until": fmt_ts(event.until, True) if event.repeats > 1 else None, "first_of_kind": event.mark})
    return report


LEVEL_TAG = {"debug": "DEBUG", "info": "INFO ", "warning": "WARN ", "error": "ERROR", "critical": "CRIT "}


def _cell(text):
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def render_markdown(report):
    out = ["# Incident timeline", ""]
    w = report["window"]
    if w["start"] or w["end"]:
        text = "%s to %s" % (w["start"] or "start of logs", w["end"] or "end of logs")
        if w["around"]:
            text += " (around %s, %s before and %s after)" % (
                w["around"], fmt_duration(w["before_seconds"]), fmt_duration(w["after_seconds"]))
        out.append("- Window: " + text)
    else:
        out.append("- Window: all timestamps in the logs")
    out.append("- Times are UTC. Showing %s and above; error level for first occurrences and spikes: %s."
               % (report["level_shown"], report["error_level"]))
    out.append("- Records in window shown: %d of %d%s" % (
        report["events_shown"], report["events_in_window"],
        " (repeats within the fold gap are merged)" if any(e["repeats"] > 1 for e in report["events"]) else ""))
    if report["events_omitted"]:
        out.append("- %d more lines not shown (--max-lines); first occurrences are always kept" % report["events_omitted"])
    if report["events_truncated"]:
        out.append("- Collection stopped at %d records; narrow the window" % MAX_EVENTS)
    skipped = sum(s["without_timestamp"] for s in report["sources"])
    if skipped:
        out.append("- %d records had no parseable timestamp and could not be placed" % skipped)
    out.append("")
    out.append("## Sources")
    out.append("")
    out.append("| Source | File | Format | Records | In window | First | Last |")
    out.append("|---|---|---|--:|--:|---|---|")
    for s in report["sources"]:
        fmts = ", ".join("%s %d" % (k, v) for k, v in s["formats"].items()) or "-"
        out.append("| %s | `%s` | %s | %d | %d | %s | %s |" % (
            s["label"], s["file"], fmts, s["records"], s["records_in_window"], s["first"] or "-", s["last"] or "-"))
    out.append("")
    out.append("## First bad lines")
    out.append("")
    fb = report["first_bad_line"]
    if fb is None:
        out.append("No %s-level records in the window." % report["error_level"])
    else:
        kind = "not seen before the window" if fb["new"] else "also seen before the window"
        out.append("The earliest %s-level signature in the window is `%s:%d` at %s (%s)." % (
            report["error_level"], fb["source"], fb["line"], fb["time"], kind))
        out.append("")
        out.append("| Time | Source | Line | Count in window | New | Signature |")
        out.append("|---|---|--:|--:|---|---|")
        for f in report["first_occurrences"]:
            out.append("| %s | %s | %d | %d | %s | `%s` |" % (
                f["time"], f["source"], f["line"], f["count_in_window"], "yes" if f["new"] else "no", _cell(f["signature"])))
    out.append("")
    out.append("## Error-rate spikes")
    out.append("")
    sp = report["spike"]
    if not sp.get("baseline_buckets"):
        out.append("No error-level records, so no spikes.")
    else:
        out.append("Buckets of %ds; %s baseline over %d buckets: typical %g errors per bucket (scale %g); "
                   "a spike needs score >= %g and at least %d errors." % (
                       sp["bucket_seconds"], "mean/std" if sp.get("method") == "zscore" else "median/MAD",
                       sp["baseline_buckets"], sp["baseline_median"], sp["scale"], sp["threshold"], sp["min_spike"]))
        out.append("")
        if report["spikes"]:
            out.append("| Start | End | Peak errors per bucket | Total errors | Peak score |")
            out.append("|---|---|--:|--:|--:|")
            for s in report["spikes"]:
                out.append("| %s | %s | %d | %d | %g |" % (s["start"], s["end"], s["peak"], s["total"], s["score"]))
        else:
            out.append("No bucket in the window stands out from the baseline.")
    out.append("")
    out.append("## Timeline")
    out.append("")
    if not report["events"]:
        out.append("No records at this level in the window.")
        return "\n".join(out) + "\n"
    out.append("```text")
    width = max(len(e["source"]) for e in report["events"])
    spike_marks = []
    for s in report["spikes"]:
        spike_marks.append((s["start"].replace("Z", ".000Z"), "-- error spike starts: peak %d per %ds bucket --"
                            % (s["peak"], report["spike"]["bucket_seconds"])))
        spike_marks.append((s["end"].replace("Z", ".000Z"), "-- error spike ends --"))
    spike_marks.sort()
    for e in report["events"]:
        while spike_marks and spike_marks[0][0] <= e["time"]:
            out.append("%s  %s" % (spike_marks[0][0], spike_marks[0][1]))
            spike_marks.pop(0)
        line = "%s  %-*s  %s  %s" % (e["time"], width, e["source"], LEVEL_TAG.get(e["level"], e["level"][:5].upper()),
                                     e["message"])
        if e["extra_lines"]:
            line += " (+%d lines%s)" % (e["extra_lines"], ": " + e["root_exception"] if e["root_exception"] else "")
        if e["repeats"] > 1:
            line += " (x%d until %s)" % (e["repeats"], e["until"][11:])
        line += "  [%s:%d]" % (os.path.basename(e["file"]), e["line"])
        if e["first_of_kind"] == "new":
            line += "  <- first of its kind, new in window"
        elif e["first_of_kind"] == "seen-before":
            line += "  <- first in window, seen before it"
        out.append(line)
    for when, text in spike_marks:
        out.append("%s  %s" % (when, text))
    out.append("```")
    return "\n".join(out).rstrip("\n") + "\n"


def build_parser():
    p = argparse.ArgumentParser(
        prog="timeline.py",
        description="Merge logs from several services into one UTC timeline, with first bad lines and "
                    "error-rate spikes.",
        epilog="Exit codes: 0 success, 2 usage or file error.")
    p.add_argument("logs", nargs="+", metavar="LOG", help="log files, optionally label=path; '-' is stdin")
    p.add_argument("--around", help="centre of the window, e.g. 2026-11-12T14:03:00Z")
    p.add_argument("--window", default="10m", help="half-width of the window around --around (default 10m)")
    p.add_argument("--before", help="override the window before --around (e.g. 5m)")
    p.add_argument("--after", help="override the window after --around")
    p.add_argument("--since", help="window start (instead of --around)")
    p.add_argument("--until", help="window end (instead of --around)")
    p.add_argument("--level", choices=LEVELS, default="warning", help="lowest level to list (default warning)")
    p.add_argument("--error-level", choices=LEVELS, default="error",
                   help="level that counts as bad for first occurrences and spikes (default error)")
    p.add_argument("--bucket", default="auto", help="spike bucket width, e.g. 30s (default: auto)")
    p.add_argument("--method", choices=("mad", "zscore"), default="mad", help="spike baseline (default mad)")
    p.add_argument("--threshold", type=float, default=3.5, help="spike score threshold (default 3.5)")
    p.add_argument("--min-spike", type=int, default=3, help="minimum errors in a bucket to call it a spike")
    p.add_argument("--fold-gap", type=float, default=60.0,
                   help="merge repeats of one signature within this many seconds; 0 disables (default 60)")
    p.add_argument("--max-lines", type=int, default=120, help="timeline lines to print; 0 means all (default 120)")
    p.add_argument("--tz", default="UTC", help="timezone for timestamps that carry none (default UTC)")
    p.add_argument("--year", type=int, help="year for syslog timestamps (default: this year)")
    p.add_argument("--redact", action="store_true",
                   help="mask emails, tokens, JWTs, bearer headers, IPs and card-like numbers in output")
    p.add_argument("--json", action="store_true", help="print JSON instead of markdown")
    p.add_argument("-o", "--out", metavar="FILE", help="write the report to FILE instead of stdout")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        tz = TzSpec(args.tz)
        args._around = parse_when(args.around, tz, args.year) if args.around else None
        if args._around is not None and (args.since or args.until):
            raise ValueError("use either --around or --since/--until, not both")
        lo, hi = float("-inf"), float("inf")
        if args._around is not None:
            half = parse_duration(args.window)
            lo = args._around - (parse_duration(args.before) if args.before else half)
            hi = args._around + (parse_duration(args.after) if args.after else half)
        else:
            if args.since:
                lo = parse_when(args.since, tz, args.year)
            if args.until:
                hi = parse_when(args.until, tz, args.year)
        if lo >= hi:
            raise ValueError("the window is empty (start is not before end)")
        bucket = None if args.bucket == "auto" else parse_duration(args.bucket)
        if bucket is not None and bucket < 1:
            raise ValueError("--bucket must be at least 1s")
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    sources = make_sources(args.logs)
    try:
        events, firsts, seen_before, errors, span, truncated = collect(
            sources, tz, args.year, lo, hi, LEVEL_RANK[args.level], LEVEL_RANK[args.error_level])
    except OSError as exc:
        print("error: cannot read log: %s" % exc, file=sys.stderr)
        return 2
    if bucket is None:
        if lo != float("-inf") and hi != float("inf"):
            bucket = nice_width((hi - lo) / 40.0)
        elif span[0] is not None:
            bucket = nice_width((span[1] - span[0]) / 60.0)
        else:
            bucket = 60
    try:
        spikes, spike_info = detect_spikes(errors, span, lo, hi, bucket, args.method, args.threshold, args.min_spike)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    report = build_report(sources, events, firsts, seen_before, spikes, spike_info, lo, hi, args, truncated,
                          args.redact)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n" if args.json else render_markdown(report)
    if args.out:
        problem = write_report(args.out, text, [split_source(a)[1] for a in args.logs])
        if problem:
            print("error: %s" % problem, file=sys.stderr)
            return 2
    else:
        emit(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
