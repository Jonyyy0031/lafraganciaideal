import pytest

from plans.conftest import SECTIONS, MakeRepo, plan_text
from plans.lib import declared_files, owner_module, parse_document, resolve_ref
from plans.lint import lint


def problems(make_repo: MakeRepo, plans: dict[str, str], findings: dict[str, str] | None = None):
    root = make_repo(plans, findings)
    return [line.split(": ", 1)[1] for line in lint(root)]


def test_a_valid_plan_passes(make_repo: MakeRepo) -> None:
    assert problems(make_repo, {"catalog-brands/001-create.md": plan_text()}) == []


@pytest.mark.parametrize(
    ("plans", "expected"),
    [
        ({"001-loose.md": plan_text()}, "plans live inside an initiative directory"),
        ({"Catalog_Brands/001-create.md": plan_text()}, "initiative name must be kebab-case"),
        ({"orders-checkout/001-x.md": plan_text()}, "must be named <module>-<topic>"),
        ({"catalog-brands/sub/001-x.md": plan_text()}, "one level deep"),
        ({"catalog-brands/1-create.md": plan_text()}, "file name must be NNN-slug.md"),
        (
            {"catalog-brands/001-a.md": plan_text(), "catalog-brands/001-b.md": plan_text()},
            "number 001 is already used",
        ),
        ({"catalog-brands/001-x.md": plan_text(status="started")}, "status must be one of"),
        ({"catalog-brands/001-x.md": plan_text(module="ghost")}, "is not in docs/modules.json"),
        (
            {"catalog-brands/001-x.md": plan_text(module="platform")},
            "does not own initiative 'catalog-brands'",
        ),
        ({"catalog-brands/001-x.md": plan_text(tier="huge")}, "min_implementer must be one of"),
        ({"catalog-brands/001-x.md": plan_text(depends='"002"')}, "depends_on must be a list"),
        (
            {"catalog-brands/001-x.md": plan_text(extra_frontmatter="superseded_by: 002")},
            "superseded_by is only allowed",
        ),
        ({"catalog-brands/001-x.md": plan_text(depends='["009"]')}, "does not resolve"),
        ({"catalog-brands/001-x.md": plan_text(depends='["001"]')}, "cannot depend on itself"),
        (
            {"catalog-brands/001-x.md": plan_text(omit=("Test coverage",))},
            "missing sections: Test coverage",
        ),
        (
            {
                "catalog-brands/001-x.md": plan_text(
                    order=(*SECTIONS[:5], *SECTIONS[6:], SECTIONS[5])
                )
            },
            "sections out of order",
        ),
        (
            {
                "catalog-brands/001-x.md": plan_text(
                    status="approved", sections={"Out of scope": ""}
                )
            },
            "Out of scope must have content",
        ),
    ],
)
def test_each_rule_reports_a_precise_problem(
    make_repo: MakeRepo, plans: dict[str, str], expected: str
) -> None:
    assert any(expected in p for p in problems(make_repo, plans)), problems(make_repo, plans)


def test_missing_readme_and_frontmatter(make_repo: MakeRepo) -> None:
    root = make_repo({"catalog-brands/001-x.md": "# 001 — no frontmatter\n"})
    (root / "plans/catalog-brands/README.md").unlink()
    found = [line.split(": ", 1)[1] for line in lint(root)]
    assert any("needs a README.md" in p for p in found)
    assert any("missing frontmatter" in p for p in found)


def test_invalid_yaml_is_reported(make_repo: MakeRepo) -> None:
    text = plan_text().replace("status: draft", "status: [unclosed")
    assert any("invalid YAML" in p for p in problems(make_repo, {"catalog-brands/001-x.md": text}))


@pytest.mark.parametrize(
    ("status", "section"),
    [
        ("testing", "Deviations"),
        ("review", "Test coverage"),
        ("verify", "Review findings"),
        ("done", "Verification"),
    ],
)
def test_evidence_is_required_by_status(make_repo: MakeRepo, status: str, section: str) -> None:
    text = plan_text(status=status, sections={section: "<!-- only a comment -->"})
    found = problems(make_repo, {"catalog-brands/001-x.md": text})
    assert f"## {section} must have content from status" in " ".join(found)


def test_dependencies_must_be_done_once_implementing(make_repo: MakeRepo) -> None:
    plans = {
        "catalog-brands/001-a.md": plan_text(status="approved"),
        "catalog-brands/002-b.md": plan_text(status="implementing", depends='["001"]'),
    }
    found = problems(make_repo, plans)
    assert any("depends_on catalog-brands/001 must be done" in p for p in found)
    plans["catalog-brands/002-b.md"] = plan_text(status="approved", depends='["001"]')
    assert problems(make_repo, plans) == []  # approved plans may wait


def test_cycles_are_detected_even_in_draft(make_repo: MakeRepo) -> None:
    plans = {
        "catalog-brands/001-a.md": plan_text(depends='["002"]'),
        "catalog-brands/002-b.md": plan_text(depends='["catalog-brands/001"]'),
    }
    assert any("dependency cycle" in p for p in problems(make_repo, plans))


def test_findings_are_validated(make_repo: MakeRepo) -> None:
    good = "---\nstatus: open\nmodule: catalog\nfound: 2026-10-02\n---\n\n# x\n"
    bad = "---\nstatus: fixed\nmodule: ghost\nfound: yesterday\n---\n\n# x\n"
    planned = "---\nstatus: planned\nmodule: catalog\nfound: 2026-10-02\n---\n\n# x\n"
    found = problems(
        make_repo,
        {"catalog-brands/001-x.md": plan_text()},
        {"catalog-good.md": good, "Bad_Name.md": bad, "catalog-planned.md": planned},
    )
    joined = " ".join(found)
    assert "finding file name must be kebab-case" in joined
    assert "status must be one of open" in joined
    assert "found must be a date" in joined
    assert "status planned needs plan" in joined
    assert "catalog-good.md" not in " ".join(lint(make_repo({}, {})))


def test_owner_module_prefers_the_longest_name() -> None:
    modules = {"catalog", "catalog-x", "platform"}
    assert owner_module("catalog-x-photos", modules) == "catalog-x"
    assert owner_module("catalog-brands", modules) == "catalog"
    assert owner_module("catalogue", modules) is None


def test_resolve_ref() -> None:
    assert resolve_ref("2", "catalog-brands") == "catalog-brands/002"
    assert resolve_ref("platform-x/12", "catalog-brands") == "platform-x/012"
    assert resolve_ref("abc", "catalog-brands") is None


def test_declared_files_follow_wrapped_files_lines(tmp_path) -> None:  # noqa: ANN001
    path = tmp_path / "001-x.md"
    path.write_text(
        plan_text(
            sections={
                "Steps": "1. **A**\n   - Files: `a/b.py` (create), `README.md` (modify),\n"
                "     `justfile` (modify), `docs/` (create)\n   - Do: mention `not/this.py`\n\n"
                "2. **B**\n   - Files: `a/b.py` (modify)\n"
            }
        )
    )
    assert declared_files(parse_document(path)) == ["a/b.py", "README.md", "justfile", "docs/"]
