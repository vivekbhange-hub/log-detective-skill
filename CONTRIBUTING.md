# Contributing

Thanks for helping. This pack is small on purpose, so a few rules keep it that way.

## Ground rules

- Helper scripts use the Python standard library only, run on Python 3.9+, make no network
  calls, stream their input (no reading a whole log into memory) and produce the same bytes for
  the same input. No `match` statements, no `X | Y` type unions at runtime.
- Every script change comes with a test in `tests/`. Run the suite from the repo root:

  ```
  python -m unittest discover -s tests -v
  ```

- If your change alters what the demo produces, regenerate the committed outputs and include them
  in the same pull request:

  ```
  python examples/regenerate.py
  python examples/regenerate.py --check
  ```

- Write files with LF line endings (`.gitattributes` enforces this in git). In scripts, write
  with `write_bytes` or `open(..., "w", newline="\n")`.
- Skills keep the frontmatter format checked by `tests/test_repo.py`: `name` equals the folder
  name, the description says what it does and lists trigger phrases, and the body has "Inputs",
  "Outputs", "Steps", "Decision rules" and "What NOT to do" sections.
- Numbers quoted in the README as tool output must be copied from `examples/output/`. The test
  suite checks every line of a README block fenced as `output` against those files.

## The shared parsing core

`log_cluster.py`, `trace_group.py` and `timeline.py` each carry a copy of the same block between
the lines `# ---- begin shared core` and `# ---- end shared core ----`: timestamp parsing, format
detection, record joining, normalisation, redaction and rate counting. Skill folders must stay
self-contained, so the block is duplicated rather than imported.

When you change it, edit one copy, paste the same text into the other two and run the tests.
`tests/test_repo.py` fails if the three copies differ.

## Fixtures

Test data and examples must be fictional. Use `example.com`, `example.org` or the `.example`
domain, IP addresses from RFC 5737 (`203.0.113.0/24`), invented emails and obviously fake tokens.
The one card number in the fixtures is a public test number. Never commit a real log, even an
edited one. `tests/test_examples.py` rejects IPv4 addresses outside `203.0.113.0/24` and emails
outside the reserved domains.

## Parsing and signature rules

Changes to what counts as a record start, a level, a timestamp or a placeholder need a test for
the case that motivated them and a test for a nearby case that must not change. The main failure
modes are merging two different events into one signature and splitting one event into many; err
toward leaving a value visible and letting the sample show the difference.

## Conduct

Be kind and specific. This project follows the spirit of the
[Contributor Covenant](https://www.contributor-covenant.org/version/2/1/code_of_conduct/).

## Releasing

Bump `version` in `.claude-plugin/plugin.json` and add a matching `## [x.y.z] - YYYY-MM-DD`
section to `CHANGELOG.md`; the test suite checks that they agree.
