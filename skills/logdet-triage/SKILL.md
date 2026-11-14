---
name: logdet-triage
description: >-
  Turns a large log file into ranked failure signatures (what broke, how many times, since when,
  and its rate over time) with one raw sample each, without reading the log into the conversation.
  Handles plain text, JSON lines, logfmt, syslog, nginx/apache access logs and the usual Python,
  Java, Go and Node formats, and joins multi-line stack traces to their log line. Use when the
  user says "what's in this log", "why is production failing", "triage these logs", "find the
  errors in this log file", "summarise this 2 GB log", or "which errors are new".
argument-hint: "[path to log file]"
---

# logdet-triage: interrogate the log, do not read it

A production log is millions of lines and a handful of distinct events. This skill runs
`log_cluster.py` over the file, so the conversation only ever sees the ranked signatures, their
timing and one sample each, then turns that into a first hypothesis you can test.

## Inputs

- A log path (plain text, JSON lines, logfmt, syslog, access log; `.gz` works, `-` reads stdin).
  Several paths are clustered together.
- Optional: the time window of interest, the service name, the level to start from.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `logs-triage/clusters.md` (or whatever path the user prefers): the report from the script.
- In chat: the top 3 to 5 signatures with counts and first-seen times, a rate observation, and
  one hypothesis in the fixed shape from Step 6.

## Steps

1. **Confirm authorization.** Ask once: are these logs from a system the user owns, runs or has
   permission to analyse? Logs often contain personal data; if they are someone else's, stop.

2. **Do not open the file.** Do not `cat`, `head` or Read the log. If you need its size, use
   `ls -l` (Windows: `dir`) rather than counting lines.

3. **Run the clustering**, errors first:
   ```
   python3 "${CLAUDE_SKILL_DIR}/log_cluster.py" app.log --level error --top 15 --redact \
     -o logs-triage/clusters.md
   ```
   - `--redact` masks emails, tokens, JWTs, bearer headers, IPs and card-like numbers in what you
     will read. Use it whenever samples might be quoted in the chat, a ticket or a post-mortem.
   - Narrow a huge file with `--since 2026-11-12T14:00:00Z --until 2026-11-12T15:00:00Z`.
   - Naive timestamps are read as UTC. If the server logs local time, pass `--tz +01:00` or an
     IANA name. Syslog lines have no year: pass `--year`.

4. **Read the report, not the log.** Check the header first: record count, formats detected,
   "records with no parseable timestamp", and the level filter. A format mix you did not expect
   (for example text where you expected JSON) means the parser guessed; say so.

5. **Interpret the ranking.**
   - Count is not severity. A cluster with 30 occurrences that starts the minute everything
     else starts is more interesting than a steady 4,000-line warning.
   - Compare first-seen times. Clusters that begin in the same histogram bucket are probably one
     event; the earliest new one is the best lead, the loudest is often a symptom.
   - Read the rate sparkline: a flat line is background noise, a wall that starts at a clear
     moment is a change (deploy, config, dependency outage).
   - Signatures collapse numbers, ids, IPs, paths and quoted values, so two different bugs can
     share one (`KeyError: '<str>'`). If a signature looks too broad, say it and look at the sample.

6. **Write the hypothesis** in this shape and nothing longer:
   ```
   Hypothesis: <one sentence>
   Evidence:   cluster #N (count, first seen), cluster #M (...)
   Confidence: low | medium | high, and why
   Would confirm: <the specific log line, metric or code path>
   Next: <logdet-trace / logdet-timeline / logdet-diff, or one command>
   ```

7. **Hand off.** A sample that is a stack trace: `logdet-trace`. Several services or an incident
   time: `logdet-timeline`. A deploy or environment comparison: `logdet-diff`.

## Decision rules

- Start with `--level error`. If nothing comes back, rerun with `--level warning`, then without a
  level. Lines with no stated level get a keyword-based level and are marked inferred in the
  JSON (`--json`); say so when your finding rests on one.
- Records with no timestamp are counted but cannot be placed on the histogram; a time filter
  skips them. If most records lack timestamps, say the timing evidence is weak.
- Need another raw line of a cluster? Search for a distinctive piece of its sample with
  `grep -n -m 5 "<text>" app.log` and read those few lines only.
- Several files from several services: cluster them together only to see the overall picture.
  For ordering across services use `logdet-timeline`.
- `--json` gives the same data for scripting (`rate` is the per-bucket count list).

## What NOT to do

- Do not read the raw log into the conversation, paste chunks of it, or ask the user to.
- Do not analyse logs the user is not authorized to access, and do not upload them anywhere.
- Do not print secrets or personal data from samples; use `--redact`, and refer to a field by
  name instead of quoting its value.
- Do not call a root cause from a single cluster, and do not rank by count alone.
- Do not invent lines, counts or times; everything you state comes from the report.
