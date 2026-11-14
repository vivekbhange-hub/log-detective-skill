#!/usr/bin/env python3
"""log_cluster: turn a large log into ranked failure signatures without reading it line by line.

Streams plain-text logs, JSON lines, logfmt, syslog, nginx/apache access logs and the usual
Python, Java/Log4j, Go and Node formats. Multi-line records (stack traces) are joined to the line
before them. Every message is normalised to a signature (numbers, ids, UUIDs, IPs, hex, paths,
quoted values, timestamps, emails and tokens become placeholders) and records with the same
signature form one cluster. For each cluster it reports the level, count, first and last seen, a
rate-over-time sparkline and one raw sample.

  log_cluster.py app.log --level error --top 10
  log_cluster.py after.log --baseline before.log         # mark NEW / SPIKING / vanished
  log_cluster.py app.jsonl --since 2026-11-12T14:00:00Z --redact --json

Memory use depends on the number of distinct signatures (capped by --max-clusters), not on the size
of the file. Standard library only, deterministic output, Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
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


class Cluster(object):
    __slots__ = ("sig", "rank", "count", "first", "last", "rates", "sample", "sample_file", "sample_line",
                 "levels")

    def __init__(self, sig, sample, sample_file, sample_line):
        self.sig = sig
        self.rank = -1
        self.count = 0
        self.first = None
        self.last = None
        self.rates = RateCounter(256)
        self.sample = sample
        self.sample_file = sample_file
        self.sample_line = sample_line
        self.levels = {}


class Analysis(object):
    def __init__(self, paths):
        self.paths = list(paths)
        self.clusters = {}
        self.lines = 0
        self.records = 0
        self.counted = 0
        self.no_ts = 0
        self.level_skipped = 0
        self.time_skipped = 0
        self.overflow = 0
        self.min_ts = None
        self.max_ts = None
        self.formats = {}


def _counting(stream, analysis):
    for line in stream:
        analysis.lines += 1
        yield line


def make_sample(rec, limit):
    lines = rec.lines[:limit]
    text = "\n".join(line[:400] for line in lines)
    extra = len(rec.lines) - len(lines) + rec.truncated
    if extra > 0:
        text += "\n... (%d more lines)" % extra
    return text[:2000]


def analyze(paths, tz=UTC, year=None, min_level=None, since=None, until=None, max_clusters=5000,
            sample_lines=12):
    """Stream every file in `paths` and return an Analysis holding the clusters."""
    an = Analysis(paths)
    min_rank = LEVEL_RANK[min_level] if min_level else None
    for path in paths:
        stream = open_log(path)
        try:
            for rec in iter_records(_counting(stream, an), tz, year):
                an.records += 1
                an.formats[rec.fmt] = an.formats.get(rec.fmt, 0) + 1
                rank = LEVEL_RANK.get(rec.level, -1)
                if min_rank is not None and rank < min_rank:
                    an.level_skipped += 1
                    continue
                if rec.ts is None:
                    an.no_ts += 1
                    if since is not None or until is not None:
                        an.time_skipped += 1
                        continue
                elif (since is not None and rec.ts < since) or (until is not None and rec.ts >= until):
                    an.time_skipped += 1
                    continue
                sig = signature(rec)
                cluster = an.clusters.get(sig)
                if cluster is None:
                    if len(an.clusters) >= max_clusters:
                        an.overflow += 1
                        continue
                    cluster = Cluster(sig, make_sample(rec, sample_lines), path, rec.lineno)
                    an.clusters[sig] = cluster
                an.counted += 1
                cluster.count += 1
                cluster.levels[rec.level] = cluster.levels.get(rec.level, 0) + 1
                if rank > cluster.rank:
                    cluster.rank = rank
                if rec.ts is not None:
                    cluster.rates.add(rec.ts)
                    if cluster.first is None or rec.ts < cluster.first:
                        cluster.first = rec.ts
                    if cluster.last is None or rec.ts > cluster.last:
                        cluster.last = rec.ts
                    if an.min_ts is None or rec.ts < an.min_ts:
                        an.min_ts = rec.ts
                    if an.max_ts is None or rec.ts > an.max_ts:
                        an.max_ts = rec.ts
        finally:
            if stream is not sys.stdin:
                stream.close()
    return an


def span_minutes(an):
    if an.min_ts is None or an.max_ts is None:
        return 1.0
    return max((an.max_ts - an.min_ts) / 60.0, 1.0)


STATUS_ORDER = {"new": 0, "spiking": 1, "steady": 2, "down": 3}


def compare(cluster, base_cluster, cur_minutes, base_minutes, factor, min_count):
    """Status of a cluster against the baseline: new, spiking, down or steady, plus the rate ratio."""
    if base_cluster is None:
        return "new", None
    cur_rate = cluster.count / cur_minutes
    base_rate = base_cluster.count / base_minutes
    ratio = cur_rate / base_rate if base_rate > 0 else None
    if ratio is not None and cluster.count >= min_count and ratio >= factor:
        return "spiking", ratio
    if ratio is not None and base_cluster.count >= min_count and ratio <= 1.0 / factor:
        return "down", ratio
    return "steady", ratio


def _bucket_seconds(an, buckets):
    if an.min_ts is None:
        return None
    return max((int(an.max_ts) + 1 - int(an.min_ts)) / float(buckets), 1.0)


def build_report(an, top=20, buckets=24, base=None, factor=3.0, min_count=3, mask=False, ascii_only=False):
    """Turn an Analysis (and optional baseline Analysis) into a JSON-ready dict."""
    clean = redact if mask else (lambda text: text)
    entries = []
    cur_minutes = span_minutes(an)
    base_minutes = span_minutes(base) if base is not None else 1.0
    for cluster in an.clusters.values():
        status = ratio = None
        base_count = None
        if base is not None:
            base_cluster = base.clusters.get(cluster.sig)
            status, ratio = compare(cluster, base_cluster, cur_minutes, base_minutes, factor, min_count)
            base_count = base_cluster.count if base_cluster else 0
        entries.append((cluster, status, ratio, base_count))
    if base is not None:
        entries.sort(key=lambda e: (STATUS_ORDER[e[1]], -e[0].rank, -e[0].count, e[0].sig))
    else:
        entries.sort(key=lambda e: (-e[0].rank, -e[0].count, e[0].first or 0, e[0].sig))
    shown = []
    for index, (cluster, status, ratio, base_count) in enumerate(entries[:top], 1):
        rate = None
        if an.min_ts is not None and cluster.first is not None:
            rate = cluster.rates.histogram(an.min_ts, an.max_ts, buckets)
        item = {
            "rank": index,
            "signature": clean(cluster.sig),
            "level": LEVELS[cluster.rank] if cluster.rank >= 0 else "unknown",
            "count": cluster.count,
            "first_seen": fmt_ts(cluster.first) if cluster.first is not None else None,
            "last_seen": fmt_ts(cluster.last) if cluster.last is not None else None,
            "rate": rate,
            "sparkline": sparkline(rate, ascii_only) if rate else "",
            "sample": clean(cluster.sample),
            "sample_file": cluster.sample_file,
            "sample_line": cluster.sample_line,
        }
        if base is not None:
            item["status"] = status
            item["baseline_count"] = base_count
            item["rate_ratio"] = round(ratio, 1) if ratio is not None else None
        shown.append(item)
    report = {
        "files": [p.replace("\\", "/") for p in an.paths],
        "lines": an.lines,
        "records": an.records,
        "formats": dict(sorted(an.formats.items())),
        "counted": an.counted,
        "cluster_count": len(an.clusters),
        "shown": len(shown),
        "no_timestamp": an.no_ts,
        "level_filtered": an.level_skipped,
        "time_filtered": an.time_skipped,
        "overflow_records": an.overflow,
        "range": {"start": fmt_ts(an.min_ts) if an.min_ts is not None else None,
                  "end": fmt_ts(an.max_ts) if an.max_ts is not None else None},
        "buckets": buckets if an.min_ts is not None else 0,
        "bucket_seconds": _bucket_seconds(an, buckets),
        "clusters": shown,
    }
    if base is not None:
        vanished = [c for c in base.clusters.values() if c.sig not in an.clusters]
        vanished.sort(key=lambda c: (-c.rank, -c.count, c.sig))
        report["baseline"] = {
            "files": [p.replace("\\", "/") for p in base.paths],
            "records": base.records,
            "counted": base.counted,
            "cluster_count": len(base.clusters),
            "range": {"start": fmt_ts(base.min_ts) if base.min_ts is not None else None,
                      "end": fmt_ts(base.max_ts) if base.max_ts is not None else None},
        }
        report["spike_factor"] = factor
        report["min_count"] = min_count
        report["summary"] = {
            "new": sum(1 for e in entries if e[1] == "new"),
            "spiking": sum(1 for e in entries if e[1] == "spiking"),
            "steady": sum(1 for e in entries if e[1] == "steady"),
            "down": sum(1 for e in entries if e[1] == "down"),
            "vanished": len(vanished),
        }
        report["vanished"] = [{
            "signature": clean(c.sig),
            "level": LEVELS[c.rank] if c.rank >= 0 else "unknown",
            "count": c.count,
            "first_seen": fmt_ts(c.first) if c.first is not None else None,
            "last_seen": fmt_ts(c.last) if c.last is not None else None,
            "sample": clean(c.sample),
            "sample_file": c.sample_file,
            "sample_line": c.sample_line,
        } for c in vanished[:top]]
    return report


def _cell(text):
    return text.replace("|", "\\|").replace("`", "'").replace("\n", " ")


def _fence(text):
    return text.replace("```", "'''")


def render_markdown(report, min_level=None):
    out = ["# Log clusters", ""]
    out.append("- Files: " + ", ".join("`%s`" % f for f in report["files"]))
    formats = ", ".join("%s %d" % (k, v) for k, v in report["formats"].items()) or "none"
    out.append("- Records: %d from %d lines (formats: %s)" % (report["records"], report["lines"], formats))
    if report["range"]["start"]:
        out.append("- Time range: %s to %s" % (report["range"]["start"], report["range"]["end"]))
        out.append("- Rate histogram: %d buckets of about %ds each" % (report["buckets"], round(report["bucket_seconds"])))
    if min_level:
        out.append("- Level filter: %s and above (%d records below it skipped)" % (min_level, report["level_filtered"]))
    if report["time_filtered"]:
        out.append("- Outside the --since/--until window: %d records skipped" % report["time_filtered"])
    if report["no_timestamp"]:
        out.append("- Records with no parseable timestamp: %d (counted, but not placed on the histogram)"
                   % report["no_timestamp"])
    out.append("- Clusters: %d (showing %d)" % (report["cluster_count"], report["shown"]))
    if report["overflow_records"]:
        out.append("- %d records had signatures beyond --max-clusters and were not clustered"
                   % report["overflow_records"])
    base = report.get("baseline")
    if base:
        out.append("- Baseline: %s (%d records, %d clusters)" % (
            ", ".join("`%s`" % f for f in base["files"]), base["records"], base["cluster_count"]))
        s = report["summary"]
        out.append("- Versus baseline: %d new, %d spiking (rate x%g or more), %d steady, %d down, %d vanished"
                   % (s["new"], s["spiking"], report["spike_factor"], s["steady"], s["down"], s["vanished"]))
    out.append("")
    clusters = report["clusters"]
    if not clusters:
        out.append("No records matched.")
        out.append("")
    else:
        if base:
            out.append("| # | Status | Level | Count | Before | First seen | Last seen | Rate | Signature |")
            out.append("|--:|---|---|--:|--:|---|---|---|---|")
        else:
            out.append("| # | Level | Count | First seen | Last seen | Rate | Signature |")
            out.append("|--:|---|--:|---|---|---|---|")
        for c in clusters:
            first, last = c["first_seen"] or "-", c["last_seen"] or "-"
            rate = "`%s`" % c["sparkline"] if c["sparkline"] else "-"
            sig = "`%s`" % _cell(c["signature"])
            if base:
                ratio = c.get("rate_ratio")
                status = c["status"].upper() + (" (x%g)" % ratio if ratio and c["status"] in ("spiking", "down") else "")
                out.append("| %d | %s | %s | %d | %d | %s | %s | %s | %s |" % (
                    c["rank"], status, c["level"], c["count"], c["baseline_count"], first, last, rate, sig))
            else:
                out.append("| %d | %s | %d | %s | %s | %s | %s |" % (
                    c["rank"], c["level"], c["count"], first, last, rate, sig))
        out.append("")
        out.append("## Samples")
        out.append("")
        for c in clusters:
            out.append("### %d. %s x%d" % (c["rank"], c["level"], c["count"]))
            out.append("")
            out.append("`%s:%d`" % (c["sample_file"].replace("\\", "/"), c["sample_line"]))
            out.append("")
            out.append("```text")
            out.append(_fence(c["sample"]))
            out.append("```")
            out.append("")
    if base and report["vanished"]:
        out.append("## Vanished (in the baseline, not in this log)")
        out.append("")
        out.append("| Level | Count before | Last seen | Signature |")
        out.append("|---|--:|---|---|")
        for v in report["vanished"]:
            out.append("| %s | %d | %s | `%s` |" % (v["level"], v["count"], v["last_seen"] or "-", _cell(v["signature"])))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def build_parser():
    p = argparse.ArgumentParser(
        prog="log_cluster.py",
        description="Cluster a log into ranked failure signatures: count, first/last seen, rate over time "
                    "and one raw sample per signature. Streams the file; never prints the whole log.",
        epilog="Exit codes: 0 success, 1 only with --fail-on-new when a new error cluster exists, "
               "2 usage or file error.")
    p.add_argument("logs", nargs="+", help="log file(s); '-' reads stdin; .gz is decompressed")
    p.add_argument("--level", choices=LEVELS, help="only records at this level or worse")
    p.add_argument("--since", help="only records at or after this time (ISO-8601, e.g. 2026-11-12T14:00:00Z)")
    p.add_argument("--until", help="only records before this time")
    p.add_argument("--top", type=int, default=20, help="clusters to show (default 20)")
    p.add_argument("--redact", action="store_true",
                   help="mask emails, tokens, JWTs, bearer headers, IPs and card-like numbers in output")
    p.add_argument("--baseline", metavar="LOG", help="a log from before: mark clusters NEW or SPIKING, list vanished ones")
    p.add_argument("--spike-factor", type=float, default=3.0,
                   help="rate ratio versus the baseline that counts as spiking (default 3)")
    p.add_argument("--min-count", type=int, default=3,
                   help="minimum occurrences for spiking/down to be reported (default 3)")
    p.add_argument("--fail-on-new", action="store_true",
                   help="with --baseline: exit 1 if a NEW cluster at error level or worse exists")
    p.add_argument("--buckets", type=int, default=24, help="histogram buckets per cluster (default 24)")
    p.add_argument("--tz", default="UTC", help="timezone for timestamps that carry none (UTC, +05:30, Europe/Berlin)")
    p.add_argument("--year", type=int, help="year for syslog timestamps, which have none (default: this year)")
    p.add_argument("--max-clusters", type=int, default=5000, help="cap on distinct signatures kept (default 5000)")
    p.add_argument("--sample-lines", type=int, default=12, help="lines kept per sample (default 12)")
    p.add_argument("--ascii", action="store_true", help="draw histograms with ASCII characters")
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
    if args.top < 1 or args.buckets < 1 or args.max_clusters < 1:
        print("error: --top, --buckets and --max-clusters must be at least 1", file=sys.stderr)
        return 2
    if args.fail_on_new and not args.baseline:
        print("error: --fail-on-new needs --baseline", file=sys.stderr)
        return 2
    try:
        an = analyze(args.logs, tz, args.year, args.level, since, until, args.max_clusters, args.sample_lines)
        base = None
        if args.baseline:
            base = analyze([args.baseline], tz, args.year, args.level, None, None, args.max_clusters,
                           args.sample_lines)
    except OSError as exc:
        print("error: cannot read log: %s" % exc, file=sys.stderr)
        return 2
    report = build_report(an, args.top, args.buckets, base, args.spike_factor, args.min_count, args.redact,
                          args.ascii)
    text = json.dumps(report, indent=2, ensure_ascii=False) + "\n" if args.json else \
        render_markdown(report, args.level)
    if args.out:
        problem = write_report(args.out, text, list(args.logs) + ([args.baseline] if args.baseline else []))
        if problem:
            print("error: %s" % problem, file=sys.stderr)
            return 2
    else:
        emit(text)
    if args.fail_on_new and base is not None:
        for c in an.clusters.values():
            if c.rank >= LEVEL_RANK["error"] and c.sig not in base.clusters:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
