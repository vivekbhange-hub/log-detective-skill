---
name: logdet-diff
description: >-
  Compares two logs, before and after a deploy or between two environments, and lists the new,
  vanished, spiking and quieter failure signatures with counts and rates, so a regression shows
  up as a short list instead of a pile of lines. Use when the user says "what changed after the
  deploy", "diff these two logs", "compare staging and production logs", "did the release add
  new errors", "is this error new", or "what got worse since yesterday".
argument-hint: "[after log] [before log]"
---

# logdet-diff: what is different in the logs since the change

The question after a release is not "what is in the log" but "what is in it now that was not
there before". `log_cluster.py --baseline` clusters the new log, matches every signature
against the old one and marks it NEW, SPIKING, STEADY or DOWN, and lists what vanished.

## Inputs

- The "after" log (the deploy, the new environment) and the "before" log (the baseline).
  Ideally comparable: similar traffic, similar duration, same service, same level of logging.
- Optional: the level to compare (`--level error` is the usual start).
- Commands below say `python3`; on Windows use `python` (or `py -3`).

## Outputs

- `logs-triage/diff.md`: the table of clusters with status, counts before and after, samples,
  and the vanished list.
- In chat: new error-level signatures first, then spikes, then vanished ones that matter, and a
  verdict on whether the change looks safe, with its caveats.

## Steps

1. **Confirm authorization.** Both logs must come from systems the user owns or may analyse.

2. **Check the two logs are comparable.** Ask how long each covers and whether traffic was
   similar. A 10-minute baseline against a 3-hour "after" log inflates nothing (the comparison
   uses per-minute rates), but a quiet baseline against a peak-hour log can invent spikes.

3. **Run the comparison:**
   ```
   python3 "${CLAUDE_SKILL_DIR}/../logdet-triage/log_cluster.py" after.log \
     --baseline before.log --level warning --redact -o logs-triage/diff.md
   ```
   Add `--spike-factor 2` to be more sensitive (default 3) or `--min-count 5` to ignore rare
   signatures. `--since` and `--until` apply to the after log only; the baseline is read whole.

4. **Read it in this order:**
   - NEW at error level or worse: the regressions to look at first. Read each sample.
   - SPIKING: the signature existed before but its rate rose by the factor shown (`x14`).
   - VANISHED: something that used to be logged is gone. Good news (a fixed warning) or bad
     news (the code path no longer runs, or logging was removed). Decide from the sample.
   - DOWN and STEADY: mention only what the user asked about.

5. **State what the diff cannot tell you.** A new signature may be a new log line rather than a
   new failure (a reworded message looks like one NEW plus one vanished). Say when a NEW and a
   VANISHED signature look like the same event renamed.

6. **Give a verdict** in one of three forms: "no new error-level signatures", "N new, none
   critical, here are the two to look at", or "regression: <signature> started with the deploy".
   For a regression, hand off to `logdet-trace` with the sample, or `logdet-timeline` for the
   ordering around the deploy.

## Decision rules

- CI use: `--fail-on-new` exits 1 when a NEW cluster at error level or worse exists, so a
  staging smoke test can gate a release. State that this is a heuristic gate.
- Rates are compared per minute of each log's own time range. Logs without timestamps fall back
  to raw counts over one minute, which makes rates meaningless; say so and compare counts.
- Signature matching is exact after normalisation. Messages that embed varying words (not
  numbers or ids) split into several signatures; widen by reading samples, not by guessing.
- `--json` gives `status`, `baseline_count`, `rate_ratio` and a `vanished` list for scripts.

## What NOT to do

- Do not declare a release safe because nothing is NEW at error level: spikes and vanished
  signatures matter too, and logs only cover what the code logs.
- Do not compare logs of unrelated services or wildly different traffic and call the result a
  regression.
- Do not analyse logs you are not authorized to access, and do not upload them.
- Do not quote personal data or secrets from samples; keep `--redact` on.
