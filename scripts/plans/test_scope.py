import subprocess
from pathlib import Path

import pytest

from plans.conftest import MakeRepo, plan_text
from plans.scope import is_allowed, main

PLAN = "plans/catalog-brands/001-create.md"
STEPS = (
    "1. **Code**\n"
    "   - Files: `apps/api/src/fragancia_api/modules/catalog/contracts.py` (modify),\n"
    "     `apps/api/src/fragancia_api/modules/catalog/infrastructure/tables.py` (modify),\n"
    "     `docs/recipes/` (create), `apps/api/pyproject.toml` (modify), `justfile` (modify)\n"
    "   - Do: it.\n"
)


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)  # noqa: S603, S607


def write(root: Path, relative: str, text: str = "x\n") -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def repo(make_repo: MakeRepo) -> Path:
    root = make_repo({"catalog-brands/001-create.md": plan_text(sections={"Steps": STEPS})})
    git(root, "init", "-q", "-b", "main")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base")
    git(root, "switch", "-q", "-c", "feat/x")
    return root


def run(root: Path, *extra: str) -> int:
    return main([PLAN, *extra], root)


def test_declared_and_companion_files_are_in_scope(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for relative in (
        "apps/api/src/fragancia_api/modules/catalog/contracts.py",  # declared
        "docs/recipes/new.md",  # under a declared directory
        "justfile",  # declared with a change marker
        "apps/api/migrations/versions/0003_x.py",  # tables.py declared
        "apps/api/openapi.json",  # contracts.py declared
        "uv.lock",  # pyproject.toml declared
        "apps/api/src/fragancia_api/container.py",  # hot file
        "plans/catalog-brands/README.md",  # initiative README
        "plans/findings/catalog-x.md",  # finding
    ):
        write(repo, relative)
    assert run(repo) == 0
    out = capsys.readouterr().out
    assert "✔ Every change is inside the plan" in out
    assert "Hot files" in out and "container.py" in out


def test_a_file_outside_the_plan_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write(repo, "apps/api/src/fragancia_api/modules/orders/thing.py")
    assert run(repo) == 1
    assert "orders/thing.py" in capsys.readouterr().err


def test_committed_staged_and_renamed_changes_count(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(repo, "README.md", "readme\n")
    git(repo, "add", "README.md")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c1")
    git(repo, "mv", "README.md", "OTHER.md")  # staged rename: both paths are changes
    assert run(repo) == 1
    err = capsys.readouterr().err
    assert '"README.md"' in err and '"OTHER.md"' in err


def test_declared_but_unchanged_is_reported(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(repo) == 0
    assert "Declared but unchanged" in capsys.readouterr().out


@pytest.mark.parametrize("base", ["-x", "missing-branch"])
def test_bad_base_is_a_usage_error(repo: Path, base: str) -> None:
    assert run(repo, f"--base={base}") == 2


def test_missing_plan_is_a_usage_error(repo: Path) -> None:
    assert main(["plans/nope/001-x.md"], repo) == 2


def test_is_allowed_rules() -> None:
    declared = ["a/contracts.py"]
    assert is_allowed("apps/api/openapi.json", declared, PLAN)
    assert not is_allowed("apps/api/migrations/versions/0009.py", declared, PLAN)
    assert not is_allowed("uv.lock", declared, PLAN)
