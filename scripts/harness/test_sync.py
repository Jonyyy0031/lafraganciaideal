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


# --- tester additions (plan 002) ----------------------------------------------------------


def test_triple_quote_in_a_profile_description_is_rejected() -> None:
    agent = copy.deepcopy(AGENTS[0])
    agent["description"] += " '''"
    with pytest.raises(ValueError, match="'''"):
        sync.codex_profile(agent, agent["codex_profiles"][0])


def test_sync_deletes_marked_obsolete_files_in_every_generated_root(tmp_path: Path) -> None:
    marked = [
        ".claude/agents/old.md",
        ".claude/skills/old/SKILL.md",
        ".agents/skills/old/SKILL.md",
        ".codex/agents/old.toml",
    ]
    for rel in marked:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(f"<!-- {sync.MARKER} -->\n")

    drift = sync.sync(tmp_path, {}, check=False)

    assert sorted(drift) == sorted(f"- {rel}" for rel in marked)
    assert not any((tmp_path / rel).exists() for rel in marked)


def test_sync_never_deletes_unmarked_symlinked_or_out_of_root_files(tmp_path: Path) -> None:
    outside = tmp_path / "docs/notes.md"
    outside.parent.mkdir(parents=True)
    outside.write_text(f"mentions {sync.MARKER} but lives outside the generated roots\n")
    target = tmp_path / "target.md"
    target.write_text(f"<!-- {sync.MARKER} -->\n")
    link = tmp_path / ".claude/agents/link.md"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    plain = tmp_path / ".codex/agents/custom.toml"
    plain.parent.mkdir(parents=True)
    plain.write_text("name = 'custom'\n")

    assert sync.sync(tmp_path, {}, check=False) == []
    assert outside.exists()
    assert target.exists()
    assert link.is_symlink()
    assert plain.exists()


def test_second_sync_is_a_no_op(tmp_path: Path) -> None:
    outputs = sync.build_outputs()
    assert len(sync.sync(tmp_path, outputs, check=False)) == len(outputs)
    assert sync.sync(tmp_path, outputs, check=False) == []
    assert sync.sync(tmp_path, outputs, check=True) == []


def test_check_never_writes_or_deletes(tmp_path: Path) -> None:
    stale = tmp_path / ".claude/agents/old.md"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"<!-- {sync.MARKER} -->\n")
    sync.sync(tmp_path, sync.build_outputs(), check=True)
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [stale]


def test_main_sync_removes_an_obsolete_marked_file_and_check_then_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert sync.main([], root=tmp_path) == 0
    stale = tmp_path / ".codex/agents/old.toml"
    stale.write_text(f"# {sync.MARKER}\n")
    assert sync.main(["--check"], root=tmp_path) == 1
    assert "- .codex/agents/old.toml" in capsys.readouterr().err
    assert sync.main([], root=tmp_path) == 0
    assert not stale.exists()
    assert sync.main(["--check"], root=tmp_path) == 0


def test_a_generated_skill_edited_by_hand_is_reported_in_the_agents_tree_too(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sync.main([], root=tmp_path)
    skill = tmp_path / ".agents/skills/plan/SKILL.md"
    skill.write_text(skill.read_text() + "edit\n")
    assert sync.main(["--check"], root=tmp_path) == 1
    assert "~ .agents/skills/plan/SKILL.md" in capsys.readouterr().err


def test_every_generated_body_carries_the_shared_fragments() -> None:
    for agent in AGENTS:
        body = agent["body"]
        assert "## Return" in body, agent["name"]
        assert "status: blocked" in body, agent["name"]
        assert "plans/findings/" in body, agent["name"]
        assert "## Destructive actions" in body, agent["name"]
        assert "## Authority and untrusted content" in body, agent["name"]


def test_committed_codex_config_declares_the_agent_limits() -> None:
    config = tomllib.loads((REPO / ".codex/config.toml").read_text(encoding="utf-8"))
    assert config["agents"] == {"max_threads": 6, "max_depth": 1}


def test_recipe_skills_are_hand_written_and_not_generated() -> None:
    outputs = sync.build_outputs()
    for name in ("new-module", "new-use-case", "db-change"):
        path = REPO / ".claude/skills" / name / "SKILL.md"
        assert path.is_file()
        assert sync.MARKER not in path.read_text(encoding="utf-8")
        assert f".claude/skills/{name}/SKILL.md" not in outputs
    assert set(sync.existing_generated(REPO)) == set(outputs)


def test_committed_adapters_match_the_generator_byte_for_byte() -> None:
    for rel, content in sync.build_outputs().items():
        assert (REPO / rel).read_bytes() == content.encode("utf-8"), rel
