---
name: logdet-trace
description: >-
  Explains a stack trace, or every stack trace in a log, by grouping them on root-cause exception
  and top application frame, mapping that frame to the source file and line in the repo, and
  proposing the likely cause with a confidence level and the evidence that would confirm it.
  Understands Python (including chained exceptions), Java and Kotlin ("Caused by"), Node, Go
  panics and .NET traces. Use when the user says "explain this stack trace", "what's this
  exception", "why does it crash here", "group these errors by cause", "where in our code does
  this fail", or pastes a traceback.
argument-hint: "[path to log file or trace]"
---

# logdet-trace: from a wall of frames to one line of code

A trace has one frame that matters: the first line of your own code on the path to the root
cause. `trace_group.py` finds that frame in every trace in a log, groups traces that fail the
same way, and leaves the library and runtime frames out.

## Inputs

- A log containing traces, or a file holding one pasted trace (save it first; do not retype it).
- Where the service's source lives, so frames can be mapped to files. Ask if it is not the
  current repository.
- The package or path prefixes that are "yours" (`src/`, `com.acme`, `app/`). Optional but
  better: without them, any frame outside library, vendor and runtime paths counts as yours.
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `logs-triage/traces.md`: groups with counts, first and last seen, root-cause line, top three
  application frames and one sample each.
- In chat: for the top groups, the mapped source location, a proposed cause, a confidence and
  what would confirm it.

## Steps

1. **Confirm authorization.** The logs must come from a system the user owns or has permission
   to examine. Do not help with traces from someone else's service.

2. **Group the traces:**
   ```
   python3 "${CLAUDE_SKILL_DIR}/trace_group.py" app.log --app-prefix src/ --app-prefix com.acme \
     --redact -o logs-triage/traces.md
   ```
   `--app-prefix` is repeatable and matches the start of a path or function name, or a
   directory inside the path (`src/` matches `/app/src/orders.py`). Library paths such as
   `site-packages`, `node_modules`, `vendor/`, `java.*`, `jdk.*`, `runtime.*` always win over a
   prefix that only matches by containment.

3. **Read the groups, not the log.** For each of the top groups note: count, first and last
   seen, the root-cause line (the innermost exception), what it surfaced as, and the log
   message printed just before it.

4. **Map the top frame to the source.** Open the file at the reported line (the frame path may
   be a container path such as `/app/src/...`; strip the deploy prefix and search for the
   suffix with Glob). Read about 20 lines around it. Compare with the root-cause line:
   - Does the code at that line plausibly raise that exception?
   - What value, state or dependency would make it raise? Read the caller frames the same way.
   - Is the file newer than the logs? Run `git log -1 --format=%cI -- <file>` and say if the
     deployed version may differ from the checkout.

5. **Propose the cause** in this shape:
   ```
   Group #N: <exception> at <file>:<line> (<count> times, first <time>)
   Likely cause:  <one sentence tied to a specific line or value>
   Confidence:    low | medium | high, and what it rests on
   Would confirm: <log line, input value, metric or test that would settle it>
   Would refute:  <what you would expect to see if you are wrong>
   ```

6. **Chained exceptions:** state both ends. "Surfaced as `DatabaseUnavailable`; the root cause is
   `OperationalError: too many clients`." The fix usually belongs to the root cause, the
   handling code is a separate question.

## Decision rules

- Library-only traces ("no application frame found") mean the prefixes are wrong or the failure
  is inside a dependency called with bad input. Rerun with the right `--app-prefix` before
  concluding anything.
- Traces embedded in JSON log fields (`stack`, `stack_trace`, `traceback`) are found too; Go
  goroutine dumps are reported from the first goroutine.
- Many groups with the same root cause and different top frames usually mean one broken
  dependency seen from several call sites: report it once.
- Counts are traces found, not requests affected. A retry loop inflates them; say so if the
  first and last seen span a retry period.
- A time window: `--since` and `--until`. Naive timestamps are UTC unless `--tz` says otherwise.

## What NOT to do

- Do not paste whole traces into the answer; quote the root-cause line and the top frames.
- Do not trace logs the user is not authorized to analyse, and do not send them to any service.
- Do not print secrets or personal data that appear in exception messages; use `--redact`.
- Do not claim a root cause without reading the code at the reported line.
- Do not edit the code as part of this skill; propose a fix only when the user asks.
