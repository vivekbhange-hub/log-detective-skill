---
name: logdet-timeline
description: >-
  Reconstructs what happened around an incident across services by merging logs of different
  formats into one UTC-ordered timeline, windowed around a given moment, tagged by source, with
  the first occurrence of each error signature ("first bad line") and error-rate spikes marked.
  Use when the user says "what happened at 14:03", "build a timeline of the outage", "correlate
  these logs", "which service failed first", "line up the web, worker and db logs", or "when did
  this start".
argument-hint: "[log files] [--around TIME]"
---

# logdet-timeline: one clock for every service

Each service logs in its own format and time zone, so the cause is hard to see until the logs
share a timeline. `timeline.py` parses every timestamp to UTC, merges the files, cuts a window
around the incident and marks the lines worth reading.

## Inputs

- Two or more log files (JSON lines, text, logfmt, syslog, nginx/apache access logs, `.gz`).
  `label=path` names a source; otherwise the file name is the label.
- When it started, as the user knows it ("around 14:03 UTC"). If they give local time, ask for
  the zone and convert it.
- The zone of logs whose timestamps carry none (`--tz`). Default is UTC; say that out loud.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `logs-triage/timeline.md` and optionally `logs-triage/timeline.json`.
- In chat: the first bad line with its source and time, the spike window, and the sequence of
  events across services in a few sentences, each with its `source:line` reference.

## Steps

1. **Confirm authorization.** The logs must belong to systems the user owns or may analyse.

2. **Build the timeline:**
   ```
   python3 "${CLAUDE_SKILL_DIR}/timeline.py" web.jsonl worker.log db.log nginx-access.log \
     --around 2026-11-12T14:03:00Z --window 10m --redact -o logs-triage/timeline.md
   ```
   - `--window` is the half-width: `10m` covers 10 minutes either side. `--before` and `--after`
     override each side; `--since` and `--until` replace `--around`.
   - `--level info` lists more than the default `warning`; the first-occurrence table and spike
     detection always use `--error-level` (default `error`).
   - `--tz +01:00` for naive timestamps in local time. Access logs carry their own offset.

3. **Check the sources table first.** Each file's format, record count and first/last time. A
   source whose range does not overlap the incident, or with many "no parseable timestamp"
   records, cannot support a claim about ordering; say so.

4. **Read "First bad lines".** Signatures are marked new when they did not appear before the
   window opened, and "seen before" when they did. Steady background errors (old timeouts, a
   flaky mail server) show up as seen-before and are usually not the cause. The headline
   "first bad line" is the earliest new signature.

5. **Read the spikes.** The spike table is error counts per bucket against a median and MAD
   baseline taken from outside the window when there is enough data. Note start, end and peak.
   The cause should sit at or just before the spike start.

6. **Tell the story in order**, one line per step, each with its reference, for example
   `14:02:57 db:18 first FATAL ... 14:03:01 web:151 checkout fails`. Separate what the lines
   show from what you infer, and mark inferences as such.

7. **Hand off** to `logdet-trace` for a trace in the window, to `logdet-diff` if a deploy sits
   at the start, or to `logdet-postmortem` to write it up.

## Decision rules

- Clock skew: if a downstream service seems to fail before its upstream by a constant amount,
  suspect skew or a zone error before suspecting causality, and say so.
- Repeats are folded when one signature repeats in one source within 60 s (`--fold-gap 0`
  disables). `(x40 until 14:09:27)` means 40 lines, not one.
- If more lines match than `--max-lines` allows, first occurrences are always kept; narrow the
  window rather than raising the cap.
- No `--around`: the whole time range of the logs is the window, so "new" means new to the
  logs, not new to the system.
- Use `--json` when another step needs the events as data.

## What NOT to do

- Do not read the raw logs into the conversation to "double check"; open the one referenced
  line range if you must, and redact it.
- Do not analyse logs you are not authorized to access, and do not upload them.
- Do not quote personal data or secrets; the report was produced with `--redact` for that reason.
- Do not infer causation from order alone, and do not hide gaps: a source with no lines in the
  window is a finding, not a blank.
