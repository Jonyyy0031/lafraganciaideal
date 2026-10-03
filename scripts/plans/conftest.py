"""Fixtures: a throwaway repository layout with a module registry and plans."""

import json
from collections.abc import Callable
from pathlib import Path

import pytest

SECTIONS = (
    "Context",
    "Out of scope",
    "Dependencies",
    "Steps",
    "Acceptance criteria",
    "Test layers required",
    "Deviations",
    "Test coverage",
    "Review findings",
    "Verification",
)


def plan_text(
    *,
    status: str = "draft",
    module: str = "catalog",
    tier: str = "mid",
    depends: str = "[]",
    extra_frontmatter: str = "",
    sections: dict[str, str] | None = None,
    omit: tuple[str, ...] = (),
    order: tuple[str, ...] = SECTIONS,
) -> str:
    bodies = {name: "Filled." for name in SECTIONS}
    bodies["Steps"] = "1. **Do it**\n   - Files: `apps/api/x.py` (create)\n   - Do: it."
    bodies.update(sections or {})
    parts = [
        "---",
        f"status: {status}",
        f"module: {module}",
        f"min_implementer: {tier}",
        f"depends_on: {depends}",
        *([extra_frontmatter] if extra_frontmatter else []),
        "---",
        "",
        "# 001 — A plan",
        "",
    ]
    for name in order:
        if name not in omit:
            parts += [f"## {name}", "", bodies[name], ""]
    return "\n".join(parts)


type MakeRepo = Callable[..., Path]


@pytest.fixture
def make_repo(tmp_path: Path) -> MakeRepo:
    def make(plans: dict[str, str], findings: dict[str, str] | None = None) -> Path:
        (tmp_path / "docs").mkdir(exist_ok=True)
        registry = {"modules": [{"name": n} for n in ("platform", "catalog", "catalog-x")]}
        (tmp_path / "docs/modules.json").write_text(json.dumps(registry))
        for relative, text in plans.items():
            path = tmp_path / "plans" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            readme = path.parent / "README.md"
            if path.parent.name != "plans" and not readme.exists():
                readme.write_text("# initiative\n")
        for name, text in (findings or {}).items():
            path = tmp_path / "plans/findings" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        return tmp_path

    return make
