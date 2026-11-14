"""Structural checks: frontmatter, manifests, README coverage, license, changelog, line endings."""
import ast
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO_NAME = "log-detective-skill"
PREFIX = "logdet-"
TEXT_SUFFIXES = {".md", ".py", ".json", ".yml", ".yaml", ".txt", ".jsonl", ".log"}
SCRIPTS = {
    "logdet-triage": "log_cluster.py",
    "logdet-trace": "trace_group.py",
    "logdet-timeline": "timeline.py",
}
CORE_BEGIN = "# ---- begin shared core"
CORE_END = "# ---- end shared core ----"


def frontmatter(path: Path) -> dict:
    """Parse simple YAML frontmatter by hand: `key: value` and folded (>-) blocks."""
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    data, key = {}, None
    for line in text[4:end].split("\n"):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if m and not line.startswith((" ", "\t")):
            key, value = m.group(1), m.group(2).strip()
            data[key] = "" if value in {">-", ">", "|", "|-"} else value.strip('"').strip("'")
        elif key is not None:
            data[key] = (data[key] + " " + line.strip()).strip()
    return data


def skill_dirs():
    return sorted(p for p in (ROOT / "skills").iterdir() if p.is_dir() and not p.name.startswith("_"))


def agent_files():
    return sorted((ROOT / "agents").glob("*.md"))


def plugin_json() -> dict:
    return json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))


def line_count(path: Path) -> int:
    return path.read_text(encoding="utf-8").count("\n") + 1


def script_paths():
    return sorted(ROOT.glob("skills/*/*.py"))


def core_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(CORE_BEGIN)
    end = text.index(CORE_END) + len(CORE_END)
    block = text[start:end]
    return block[block.index("\n"):]       # ignore the begin line's wording


class SkillTests(unittest.TestCase):
    def test_five_skills(self):
        self.assertEqual([p.name for p in skill_dirs()], [
            "logdet-diff", "logdet-postmortem", "logdet-timeline", "logdet-trace", "logdet-triage"])

    def test_skill_frontmatter(self):
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                fm = frontmatter(d / "SKILL.md")
                self.assertEqual(fm.get("name"), d.name)
                self.assertTrue(d.name.startswith(PREFIX))
                self.assertRegex(d.name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
                self.assertTrue(fm.get("description"))
                self.assertLess(len(fm["description"]), 1024)
                self.assertIn("Use when", fm["description"])
                self.assertGreaterEqual(fm["description"].count('"'), 10, "5+ quoted trigger phrases")

    def test_skill_body(self):
        for d in skill_dirs():
            with self.subTest(skill=d.name):
                text = (d / "SKILL.md").read_text(encoding="utf-8")
                self.assertTrue(60 <= line_count(d / "SKILL.md") <= 200, f"{d.name}: {line_count(d / 'SKILL.md')} lines")
                for heading in ("## Inputs", "## Outputs", "## Steps", "## Decision rules", "## What NOT to do"):
                    self.assertIn(heading, text)
                self.assertIn("on Windows use `python`", text)
                self.assertRegex(text, r"authoriz|permission", "responsible-use rule")

    def test_referenced_scripts_exist(self):
        for d in skill_dirs():
            text = (d / "SKILL.md").read_text(encoding="utf-8")
            for rel in re.findall(r"\$\{CLAUDE_SKILL_DIR\}/([\w./-]+\.py)", text):
                with self.subTest(skill=d.name, script=rel):
                    self.assertTrue((d / rel).resolve().exists(), rel)

    def test_script_skills_reference_their_script(self):
        for skill, script in SCRIPTS.items():
            text = (ROOT / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("${CLAUDE_SKILL_DIR}/" + script, text)
        diff = (ROOT / "skills" / "logdet-diff" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("log_cluster.py", diff)
        self.assertIn("--baseline", diff)
        self.assertEqual(list((ROOT / "skills" / "logdet-postmortem").glob("*.py")), [], "post-mortem has no script")

    def test_every_script_has_tests(self):
        scripts = script_paths() + [ROOT / "examples" / "regenerate.py"]
        self.assertEqual(len(scripts), 4)
        for script in scripts:
            with self.subTest(script=script.name):
                name = "test_examples.py" if script.name == "regenerate.py" else f"test_{script.stem}.py"
                self.assertTrue((ROOT / "tests" / name).exists(), name)
                text = script.read_text(encoding="utf-8")
                self.assertIn("argparse", text)
                self.assertIn("from __future__ import annotations", text)
                self.assertNotRegex(text, r"^\s*match .*:\s*$", "no match statements (Python 3.9)")
                imports = set(re.findall(r"^(?:from|import) ([\w.]+)", text, re.M))
                self.assertFalse(imports & {"requests", "yaml", "httpx", "aiohttp", "tomllib", "numpy", "pandas"}, imports)

    def test_python_39_syntax(self):
        for path in script_paths() + sorted((ROOT / "tests").glob("*.py")) + [ROOT / "examples" / "regenerate.py"]:
            with self.subTest(file=path.name):
                try:
                    ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))
                except SyntaxError as exc:     # pragma: no cover
                    self.fail(f"{path.name} needs a newer Python than 3.9: {exc}")

    def test_scripts_do_not_use_forbidden_calls(self):
        for path in script_paths():
            text = path.read_text(encoding="utf-8")
            with self.subTest(script=path.name):
                self.assertNotIn("newline=", text.replace('newline="\\n"', "").replace("newline='\\n'", "")
                                 .replace("newline=None", ""), "write with write_bytes or newline=\"\\n\"")
                for banned in ("urllib.request", "socket", "subprocess", "http.client", "os.system"):
                    self.assertNotIn("import " + banned, text)

    def test_shared_core_is_identical_in_all_scripts(self):
        blocks = {p.name: core_block(p) for p in script_paths()}
        self.assertEqual(len(blocks), 3)
        reference = blocks["log_cluster.py"]
        for name, block in blocks.items():
            self.assertEqual(block, reference,
                             f"{name} differs from log_cluster.py: copy the shared core block into all three scripts")


class AgentTests(unittest.TestCase):
    def test_two_agents(self):
        self.assertEqual([f.stem for f in agent_files()], ["hypothesis-skeptic", "incident-scribe"])

    def test_agent_frontmatter_and_body(self):
        for f in agent_files():
            with self.subTest(agent=f.name):
                fm = frontmatter(f)
                self.assertEqual(fm.get("name"), f.stem)
                self.assertTrue(fm.get("description"))
                tools = {t.strip() for t in fm.get("tools", "").split(",")}
                self.assertEqual(tools, {"Read", "Grep", "Glob", "Bash"}, "agents are read-only")
                self.assertTrue(40 <= line_count(f) <= 120, f"{f.name}: {line_count(f)} lines")
                text = f.read_text(encoding="utf-8")
                for heading in ("## How you work", "## Output format", "## Hard rules"):
                    self.assertIn(heading, text)
                self.assertIn("Read-only", text)


class ManifestTests(unittest.TestCase):
    def test_plugin_json(self):
        data = plugin_json()
        self.assertEqual(data["name"], REPO_NAME)
        self.assertRegex(data["version"], r"^\d+\.\d+\.\d+$")
        self.assertEqual(data["license"], "MIT")
        self.assertEqual(data["author"]["name"], "Vivek Bhange")
        self.assertEqual(data["homepage"], f"https://github.com/vivekbhange-hub/{REPO_NAME}")
        self.assertEqual(data["repository"], f"https://github.com/vivekbhange-hub/{REPO_NAME}")
        self.assertTrue(6 <= len(data["keywords"]) <= 10)
        self.assertIn("claude-code", data["keywords"])
        self.assertIn("claude-skills", data["keywords"])

    def test_marketplace_json(self):
        data = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(data["name"], REPO_NAME)
        self.assertEqual(data["owner"]["name"], "Vivek Bhange")
        self.assertEqual(len(data["plugins"]), 1)
        plugin = data["plugins"][0]
        self.assertEqual(plugin["name"], REPO_NAME)
        self.assertEqual(plugin["source"], "./")
        self.assertEqual(plugin["category"], "development")
        self.assertTrue(plugin["homepage"].endswith("/" + REPO_NAME))


class DocsTests(unittest.TestCase):
    def setUp(self):
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")

    def test_readme_mentions_every_skill_and_agent(self):
        for d in skill_dirs():
            self.assertIn(d.name, self.readme)
        for f in agent_files():
            self.assertIn(f.stem, self.readme)
        for script in SCRIPTS.values():
            self.assertIn(script, self.readme)
        self.assertIn(f"/plugin marketplace add vivekbhange-hub/{REPO_NAME}", self.readme)
        self.assertIn(f"/plugin install {REPO_NAME}@{REPO_NAME}", self.readme)

    def test_readme_sections(self):
        for heading in ("## Try it in 60 seconds", "## How it works", "## Responsible use",
                        "## Principles and limits", "```mermaid", "examples/logs/"):
            self.assertIn(heading, self.readme)
        self.assertTrue(self.readme.startswith("# log-detective-skill\n"))
        self.assertIn("Don't read the logs. Interrogate them.", self.readme)

    def output_files_text(self):
        parts = []
        for path in sorted((ROOT / "examples" / "output").glob("*.md")):
            parts.append(path.read_text(encoding="utf-8"))
        return "\n".join(parts)

    def test_readme_excerpts_match_committed_outputs(self):
        blocks = re.findall(r"^```output\n(.*?)^```$", self.readme, re.M | re.S)
        self.assertGreaterEqual(len(blocks), 4, "the README quotes real output")
        available = set(self.output_files_text().split("\n"))
        for block in blocks:
            for line in block.split("\n"):
                if line.strip():
                    with self.subTest(line=line[:70]):
                        self.assertIn(line, available, "README quotes a line that is not in examples/output/")

    def test_readme_commands_reference_existing_paths(self):
        for block in re.findall(r"^```\n(.*?)^```$", self.readme, re.M | re.S):
            for rel in re.findall(r"\b((?:skills|examples)/[\w./-]+)", block):
                with self.subTest(path=rel):
                    self.assertTrue((ROOT / rel).exists(), rel)

    def test_readme_local_links_exist(self):
        for target in re.findall(r"\]\(([^)#\s]+)\)", self.readme):
            if re.match(r"https?:", target):
                continue
            with self.subTest(target=target):
                self.assertTrue((ROOT / target).exists(), target)

    def test_license(self):
        text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT", text)
        self.assertIn("Copyright (c) 2026 Vivek Bhange", text)

    def test_changelog_matches_version(self):
        version = plugin_json()["version"]
        self.assertEqual(version, "1.0.0")
        self.assertIn(f"## [{version}] - ", (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"),
                      "every released version needs a CHANGELOG entry")
        self.assertTrue((ROOT / "CONTRIBUTING.md").exists())

    def test_ci_and_gitattributes(self):
        self.assertIn("* text=auto eol=lf", (ROOT / ".gitattributes").read_text(encoding="utf-8"))
        ci = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        for needle in ("ubuntu-latest", "windows-latest", "macos-latest", '"3.9"', '"3.12"', '"3.13"',
                       "actions/checkout@v4", "actions/setup-python@v5",
                       "python -m unittest discover -s tests -v"):
            self.assertIn(needle, ci)
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
        for entry in (".DS_Store", "__pycache__/", "*.pyc", ".venv/", "*.tmp"):
            self.assertIn(entry, ignore)

    def test_text_files_use_lf(self):
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore", ".gitattributes"}:
                with self.subTest(file=str(path.relative_to(ROOT))):
                    self.assertNotIn(b"\r\n", path.read_bytes())

    def test_no_ai_attribution_in_the_repo(self):
        banned = ["Co-Authored" + "-By", "Generated" + " with", "Generated" + " by Claude"]
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            if path.suffix in TEXT_SUFFIXES or path.name in {"LICENSE", ".gitignore", ".gitattributes"}:
                text = path.read_text(encoding="utf-8", errors="replace")
                for phrase in banned:
                    with self.subTest(file=str(path.relative_to(ROOT)), phrase=phrase):
                        self.assertNotIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
