"""Tests for the adapter generator (`scripts/harness/sync.py`, `uv run just harness-check`)."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

import pytest

from harness import sync
from harness.adapters import AGENTS, SKILLS

REPO = Path(__file__).resolve().parents[2]


def test_generation_is_deterministic() -> None:
    assert sync.build_outputs() == sync.build_outputs()


def test_outputs_cover_every_agent_profile_and_skill() -> None:
    outputs = sync.build_outputs()
    expected = {
        f".claude/agents/{name}.md" for name in ("implementer", "tester", "reviewer", "verifier")
    }
    expected |= {
        f".codex/agents/{name}.toml"
        for name in (
            "implementer-small",
            "implementer",
            "implementer-high",
            "tester",
            "tester-high",
            "reviewer",
            "reviewer-medium",
            "verifier",
        )
    }
    for name in ("plan", "implement", "write-tests", "review", "verify", "fix"):
        expected |= {f".claude/skills/{name}/SKILL.md", f".agents/skills/{name}/SKILL.md"}
    assert set(outputs) == expected


def test_every_output_carries_the_marker() -> None:
    for rel, content in sync.build_outputs().items():
        assert sync.MARKER in content, rel


def test_skills_are_identical_in_both_trees() -> None:
    outputs = sync.build_outputs()
    for entry in SKILLS:
        name = entry["name"]
        assert (
            outputs[f".claude/skills/{name}/SKILL.md"] == outputs[f".agents/skills/{name}/SKILL.md"]
        )


def test_every_codex_profile_parses_as_toml() -> None:
    outputs = sync.build_outputs()
    profiles = {p["name"]: (agent, p) for agent in AGENTS for p in agent["codex_profiles"]}
    for name, (agent, profile) in profiles.items():
        data = tomllib.loads(outputs[f".codex/agents/{name}.toml"])
        assert data["name"] == name
        assert data["model"] == profile["model"]
        assert data["model_reasoning_effort"] == profile["effort"]
        assert data["developer_instructions"].startswith(agent["body"])
        assert data["developer_instructions"].endswith(f"Profile note: {profile['note']}\n")


def test_committed_codex_files_parse_as_toml() -> None:
    files = sorted((REPO / ".codex").rglob("*.toml"))
    assert files
    for path in files:
        tomllib.loads(path.read_text(encoding="utf-8"))


def test_claude_agent_frontmatter() -> None:
    outputs = sync.build_outputs()
    reviewer = outputs[".claude/agents/reviewer.md"]
    assert reviewer.startswith("---\nname: reviewer\ndescription: >\n  ")
    assert "\nmodel: opus\ntools: Read, Grep, Glob, Bash, Edit\n---\n" in reviewer
    assert "\nmodel: inherit\n---\n" in outputs[".claude/agents/implementer.md"]
    for content in outputs.values():
        if content.startswith("---"):
            frontmatter = content.split("\n---\n", 1)[0]
            assert all(len(line) <= 90 for line in frontmatter.splitlines())


def test_triple_quote_in_a_body_is_rejected() -> None:
    agent = copy.deepcopy(AGENTS[0])
    agent["body"] += "\n'''"
    with pytest.raises(ValueError, match="'''"):
        sync.codex_profile(agent, agent["codex_profiles"][0])


def test_the_repository_is_in_sync() -> None:
    assert sync.sync(REPO, sync.build_outputs(), check=True) == []


def test_check_reports_missing_different_and_obsolete(tmp_path: Path) -> None:
    outputs = {"a/x.md": f"<!-- {sync.MARKER} -->\nx\n", "b/y.md": "y\n"}
    (tmp_path / "b").mkdir()
    (tmp_path / "b/y.md").write_text("edited by hand\n")
    obsolete = tmp_path / ".claude/agents/old.md"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(f"<!-- {sync.MARKER} -->\n")
    handwritten = tmp_path / ".claude/skills/new-module/SKILL.md"
    handwritten.parent.mkdir(parents=True)
    handwritten.write_text("hand-written, no marker\n")

    drift = sync.sync(tmp_path, outputs, check=True)

    assert drift == ["+ a/x.md", "~ b/y.md", "- .claude/agents/old.md"]
    assert not (tmp_path / "a/x.md").exists()
    assert (tmp_path / "b/y.md").read_text() == "edited by hand\n"
    assert obsolete.exists()


def test_sync_writes_and_deletes_only_marked_obsolete_files(tmp_path: Path) -> None:
    outputs = {".claude/agents/new.md": f"<!-- {sync.MARKER} -->\nnew\n"}
    obsolete = tmp_path / ".codex/agents/old.toml"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_text(f"# {sync.MARKER}\n")
    handwritten = tmp_path / ".claude/skills/db-change/SKILL.md"
    handwritten.parent.mkdir(parents=True)
    handwritten.write_text("hand-written\n")

    drift = sync.sync(tmp_path, outputs, check=False)

    assert drift == ["+ .claude/agents/new.md", "- .codex/agents/old.toml"]
    assert (tmp_path / ".claude/agents/new.md").read_text() == outputs[".claude/agents/new.md"]
    assert not obsolete.exists()
    assert handwritten.read_text() == "hand-written\n"
    assert sync.sync(tmp_path, outputs, check=True) == []


def test_main_check_fails_on_drift(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert sync.main(["--check"], root=tmp_path) == 1
    assert "+ .claude/agents/tester.md" in capsys.readouterr().err
    assert sync.main([], root=tmp_path) == 0
    assert sync.main(["--check"], root=tmp_path) == 0
    tester = tmp_path / ".claude/agents/tester.md"
    tester.write_text(tester.read_text() + "hand edit\n")
    assert sync.main(["--check"], root=tmp_path) == 1
    assert "~ .claude/agents/tester.md" in capsys.readouterr().err


def test_main_rejects_unknown_arguments() -> None:
    assert sync.main(["--force"]) == 2
