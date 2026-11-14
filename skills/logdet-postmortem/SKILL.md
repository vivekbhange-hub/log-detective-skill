---
name: logdet-postmortem
description: >-
  Writes a blameless incident post-mortem from a timeline and log clusters: summary, impact,
  timeline, root cause with a five-whys that stops where the evidence stops, what went well and
  badly, and action items with owners left blank, every factual claim cited to a log line or a
  cluster. Use when the user says "write the post-mortem", "write up this incident", "draft an
  RCA", "incident report for the outage", "blameless review of what happened", or "turn this
  timeline into a post-mortem".
argument-hint: "[path to timeline.md and clusters.md]"
---

# logdet-postmortem: the write-up that only says what the logs show

A post-mortem is useful when readers can trust it. This skill writes one from the outputs of
`logdet-timeline`, `logdet-triage` and `logdet-trace`, cites a source for every factual claim,
and says plainly where the evidence stops. It has no script of its own: it works on those files.

## Inputs

- `logs-triage/timeline.md` (from `logdet-timeline`); required.
- `logs-triage/clusters.md` and `logs-triage/traces.md` if available.
- From the user, not from the logs: customer or business impact figures, who was on call, what
  was done to stop it and when, any decisions. Ask; never guess.
- Commands below say `python3`; on Windows use `python` (or `py -3`) if a step needs the
  other skills' scripts.

## Outputs

- `docs/postmortems/YYYY-MM-DD-<slug>.md` (or the user's location), using the structure below.
- A list in chat of what you could not establish and what would settle it.

## Steps

1. **Confirm authorization and audience.** The logs must be the user's to analyse, and the
   document will be read by people who may not have access to them: quote nothing sensitive.
   If a timeline or cluster file is missing, run the matching skill first; do not reconstruct
   from memory.

2. **Gather the facts** from the files into a private list: first bad line, spike window, new
   signatures, trace groups, the moment errors stopped. Give each a citation tag such as
   `[timeline: db:18]`, `[cluster #3]`, `[trace #1]` or `[web.jsonl:151]`.

3. **Write the document** with these sections, in this order:
   - **Summary**: three sentences. What broke, for how long, what ended it.
   - **Impact**: only what the logs or the user establish (error counts, affected endpoints,
     duration). Mark figures that come from the user as such. No estimates of money or users
     unless the user supplies them.
   - **Timeline**: UTC, one line per event, each with its citation. Include detection and
     response events the user told you about, marked "(reported)".
   - **Root cause**: the mechanism in plain words, then the five whys.
   - **What went well / What went poorly**: two short lists, about process and tooling.
   - **Action items**: a table with Action, Type (prevent, detect, mitigate), Owner and Due.
     Leave Owner and Due blank. Do not assign people.
   - **Open questions**: what the evidence does not settle.

4. **Five whys that stop where evidence stops.** Each "why" needs a cited log line or trace. When
   the next answer would be a guess, write "Evidence ends here" and move the question to Open
   questions. Four honest whys beat five with one invented.

5. **Check the draft against the sources**: every sentence that states a time, count, service
   or error has a citation that exists in the files; every citation supports the sentence. Remove
   what fails. Run the `hypothesis-skeptic` agent on the root cause before finishing.

6. **Blameless language.** Describe systems and decisions, not people: "the deploy enabled the
   job without a connection limit", not "X forgot". Names of people appear only in a section the
   user dictated.

## Decision rules

- Correlation is not cause: "the worker started 80 seconds before the first FATAL" is a
  finding; "the worker caused it" needs a trace, a count or a rollback result behind it.
- Seen-before signatures in the timeline are background noise unless their rate changed.
- If the user wants a cause stated that the evidence does not support, write it as a
  hypothesis in Open questions with what would confirm it.
- Action items follow the causes: one prevent, one detect and one mitigate item at most per
  cause, concrete enough to be a ticket. Leave priority to the owners.
- Keep it to about two pages. Link to the reports in `logs-triage/` rather than pasting them.

## What NOT to do

- Do not state any fact without a citation, and do not cite a line you did not see in a file.
- Do not invent impact numbers, customer reactions, response times or names.
- Do not name or blame individuals, and do not fill in owners.
- Do not include secrets, personal data or raw customer identifiers; if the reports were made
  without `--redact`, redact before quoting.
- Do not analyse logs you are not authorized to use, or publish the post-mortem without the
  user's say-so.
