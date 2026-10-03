import pytest

from commits import load_scopes, validate

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


def test_merge_and_revert_messages_from_git_are_allowed():
    assert errors_for("Merge pull request #1 from someone/feat/catalog") == []
    assert errors_for('Revert "feat(catalog): list perfumes by brand"') == []
