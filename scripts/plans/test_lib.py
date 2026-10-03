from pathlib import Path

import pytest

from plans.conftest import MakeRepo, plan_text
from plans.lib import (
    declared_files,
    initiatives,
    is_empty,
    load_findings,
    load_plans,
    load_registry,
    parse_document,
    reached,
)


def test_frontmatter_title_and_sections_are_parsed(tmp_path: Path) -> None:
    path = tmp_path / "001-x.md"
    path.write_text(plan_text(status="testing"))

    doc = parse_document(path)

    assert doc.frontmatter is not None and doc.frontmatter["status"] == "testing"
    assert doc.title == "001 — A plan"
    assert doc.section_order[0] == "Context" and doc.section_order[-1] == "Verification"
    assert doc.error is None


def test_a_document_without_frontmatter_has_none_and_no_error(tmp_path: Path) -> None:
    path = tmp_path / "001-x.md"
    path.write_text("# Title\n\n## Context\n\nText.\n")

    doc = parse_document(path)

    assert doc.frontmatter is None and doc.error is None


def test_a_frontmatter_that_is_not_a_mapping_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "001-x.md"
    path.write_text("---\n- a\n- b\n---\n\n# T\n")

    assert parse_document(path).error == "frontmatter must be a YAML mapping"


def test_yaml_dates_are_normalized_to_iso_strings(tmp_path: Path) -> None:
    path = tmp_path / "f.md"
    path.write_text("---\nfound: 2026-10-02\n---\n\n# T\n")

    assert parse_document(path).frontmatter == {"found": "2026-10-02"}


@pytest.mark.parametrize(
    ("section", "empty"),
    [
        (None, True),
        ("", True),
        ("\n  \n", True),
        ("<!-- only a comment -->", True),
        ("<!--\nmulti\nline\n-->\n\n<!-- another -->", True),
        ("<!-- c --> text", False),
        ("Filled.", False),
    ],
)
def test_is_empty_ignores_comments_and_whitespace(section: str | None, empty: bool) -> None:
    assert is_empty(section) is empty


def test_registry_lists_module_names(make_repo: MakeRepo) -> None:
    root = make_repo({})

    assert load_registry(root) == {"platform", "catalog", "catalog-x"}


def test_plans_exclude_templates_readmes_findings_and_non_markdown(make_repo: MakeRepo) -> None:
    root = make_repo(
        {
            "catalog-brands/001-real.md": plan_text(),
            "catalog-brands/_TEMPLATE.md": plan_text(),
            "catalog-brands/notes.txt": "not markdown",
        },
        {"catalog-thing.md": "---\nstatus: open\nmodule: catalog\nfound: 2026-10-02\n---\n# T\n"},
    )

    assert [p.ref for p in load_plans(root)] == ["catalog-brands/001"]
    assert [d.name for d in initiatives(root)] == ["catalog-brands"]
    assert [f.path.name for f in load_findings(root)] == ["catalog-thing.md"]


def test_findings_without_a_findings_directory_are_empty(make_repo: MakeRepo) -> None:
    assert load_findings(make_repo({"catalog-brands/001-x.md": plan_text()})) == []


def steps(text: str, tmp_path: Path):  # noqa: ANN201
    path = tmp_path / "001-x.md"
    path.write_text(plan_text(sections={"Steps": text}))
    return declared_files(parse_document(path))


def test_declared_files_need_a_slash_a_dot_or_a_change_marker(tmp_path: Path) -> None:
    text = (
        "1. **A**\n   - Files: `bare`, `./a/b.py` (modify), `two words.py`, `Makefile` (create)\n"
    )

    assert steps(text, tmp_path) == ["a/b.py", "Makefile"]


def test_declared_files_ignore_files_lines_outside_steps(tmp_path: Path) -> None:
    path = tmp_path / "001-x.md"
    path.write_text(plan_text(sections={"Context": "- Files: `elsewhere/x.py` (modify)"}))

    assert declared_files(parse_document(path)) == [
        "apps/api/x.py"  # only the default Steps of plan_text
    ]


# Was a strict-xfail GAP (review finding 1 of plan 001); fixed in lib._FILES_ENTRY.
def test_a_do_line_mentioning_files_does_not_declare_paths(tmp_path: Path) -> None:
    text = "1. **A**\n   - Files: `a/b.py` (modify)\n   - Do: update the files: `c/d.py`\n"

    assert steps(text, tmp_path) == ["a/b.py"]


def test_do_lines_never_declare_paths_even_with_a_files_colon(tmp_path: Path) -> None:
    text = "1. **A**\n   - Do: copy files: `x/y.py` and `z.py` (modify)\n"

    assert steps(text, tmp_path) == []


def test_a_wrapped_files_continuation_starting_with_a_digit_keeps_its_paths(
    tmp_path: Path,
) -> None:
    text = (
        "1. **A**\n   - Files: `a/b.py` (create),\n"
        "     `0001_init.sql` (create), `c/d.py` (modify)\n"
    )

    assert steps(text, tmp_path) == ["a/b.py", "0001_init.sql", "c/d.py"]


def test_a_heading_inside_a_code_fence_does_not_split_sections(tmp_path: Path) -> None:
    body = "Text.\n\n```md\n## Not a section\n```\n\nMore."
    path = tmp_path / "001-x.md"
    path.write_text(plan_text(sections={"Context": body}))

    doc = parse_document(path)

    assert "Not a section" not in doc.section_order
    assert "## Not a section" in doc.sections["Context"] and "More." in doc.sections["Context"]
    assert doc.section_order[:2] == ["Context", "Out of scope"]


@pytest.mark.parametrize(
    ("status", "target", "expected"),
    [
        ("testing", "implementing", True),
        ("implementing", "implementing", True),
        ("approved", "implementing", False),
        ("done", "review", True),
        ("blocked", "draft", False),  # outside the pipeline
        ("superseded", "done", False),
        (None, "draft", False),
    ],
)
def test_reached_follows_the_pipeline_order(
    status: str | None, target: str, expected: bool
) -> None:
    assert reached(status, target) is expected
