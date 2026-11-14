# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-11-13

### Added

- Five skills: `logdet-triage`, `logdet-trace`, `logdet-timeline`, `logdet-diff` and
  `logdet-postmortem`.
- Two read-only agents: `incident-scribe` and `hypothesis-skeptic`.
- Helper scripts (standard library only, streaming): `log_cluster.py` (signature clustering with
  rates, `--redact` and `--baseline`), `trace_group.py` (Python, Java/Kotlin, Node, Go and .NET
  traces grouped by root cause and top application frame) and `timeline.py` (multi-file UTC
  timeline with first bad lines and error-rate spikes).
- Synthetic log fixtures for a made-up shop incident under `examples/logs/`, with reports under
  `examples/output/` checked byte-for-byte by the test suite.
- CI on Linux, Windows and macOS with Python 3.9, 3.12 and 3.13.
