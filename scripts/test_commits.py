import subprocess

import pytest

import commits
from commits import load_scopes, main, validate

SCOPES = load_scopes()


def errors_for(message: str) -> list[str]:
    return validate(message, SCOPES)


@pytest.mark.parametrize(
    "message",
    [
        "chore(infra): add development services with docker compose",
        "feat(orders): order state machine with cancellation",
        "fix(payments)!: reject webhooks with an invalid signature",
        "docs(platform): plan 001 for the local environment\n\nLonger body explaining why.",
        "ci(ci): pin actions by commit sha",
        "docs(harness): roles and workflow for the plan pipeline",
    ],
)
def test_valid_messages_pass(message: str):
    assert errors_for(message) == []


def test_scopes_include_registry_modules_and_cross_cutting():
    assert {"catalog", "orders", "platform", "infra", "docs", "harness"} <= SCOPES


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("changes", "Conventional Commits"),
        ("feat: add catalog", "scope is required"),
        ("feat(unknown): add catalog", "unknown scope 'unknown'"),
        ("feature(catalog): add catalog", "unknown type 'feature'"),
        ("fix(catalog): wip", "say WHAT changed"),
        ("chore(repo): changes", "say WHAT changed"),
        ("feat(catalog): " + "x" * 100, "header is longer than 100"),
    ],
)
def test_invalid_headers_are_rejected(message: str, expected: str):
    errors = errors_for(message)
    assert any(expected in error for error in errors), errors


@pytest.mark.parametrize(
    "trailer",
    [
        "Co-Authored-By: Claude <noreply@anthropic.com>",
        "co-authored-by: someone <a@b.c>",
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)",
        "Generated with Codex",
    ],
)
def test_ai_attribution_is_rejected(trailer: str):
    errors = errors_for(f"feat(catalog): list perfumes by brand\n\n{trailer}")
    assert any("AI attribution" in error for error in errors), errors


def test_git_comment_lines_are_ignored():
    message = "feat(catalog): list perfumes by brand\n# Please enter the commit message\n"
    assert errors_for(message) == []


def test_a_header_of_exactly_one_hundred_characters_is_accepted():
    header = "feat(catalog): " + "x" * (100 - len("feat(catalog): "))

    assert len(header) == 100 and errors_for(header) == []


def test_an_empty_message_is_rejected():
    assert any("Conventional Commits" in error for error in errors_for("# only a comment\n"))


@pytest.mark.parametrize("subject", ["Update.", "stuff", "Cambios", "misc"])
def test_generic_subjects_are_rejected(subject: str):
    assert any("say WHAT changed" in e for e in errors_for(f"chore(repo): {subject}"))


def test_an_empty_scope_is_reported_as_missing():
    assert any("scope is required" in error for error in errors_for("feat(): add catalog"))


def test_ai_attribution_is_rejected_even_in_a_merge_message():
    errors = errors_for("Merge branch 'x'\n\nCo-Authored-By: Claude <noreply@anthropic.com>")

    assert any("AI attribution" in error for error in errors)


def test_main_validates_a_message_file(tmp_path, capsys):
    good = tmp_path / "good"
    good.write_text("docs(harness): plan tooling for the pipeline\n")
    bad = tmp_path / "bad"
    bad.write_text("feat(unknown): add catalog\n")

    assert main(["--file", str(good)]) == 0
    assert main(["--file", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "unknown scope 'unknown'" in err and "CONTRIBUTING.md" in err


def test_main_requires_exactly_one_source(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        main([])

    assert exit_info.value.code == 2


def test_main_checks_every_commit_in_a_range(tmp_path, monkeypatch, capsys):
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)  # noqa: S603, S607

    git("init", "-q", "-b", "main")
    ident = ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty"]
    git(*ident, "-m", "chore(repo): first commit of the range base")
    git(*ident, "-m", "feat(catalog): list perfumes by brand")
    git(*ident, "-m", "wip")
    monkeypatch.setattr(commits, "ROOT", tmp_path)

    assert main(["--range", "HEAD~2..HEAD~1"]) == 0
    assert "1 commit message(s) follow the convention" in capsys.readouterr().out
    assert main(["--range", "HEAD~2..HEAD"]) == 1
    assert "'wip'" in capsys.readouterr().err


def test_merge_and_revert_messages_from_git_are_allowed():
    assert errors_for("Merge pull request #1 from someone/feat/catalog") == []
    assert errors_for('Revert "feat(catalog): list perfumes by brand"') == []
