import pytest

from plans.conftest import MakeRepo, plan_text
from plans.lib import load_plans
from plans.status import main, rows


def test_rows_are_ordered_by_urgency(make_repo: MakeRepo) -> None:
    root = make_repo(
        {
            "catalog-brands/001-a.md": plan_text(status="done"),
            "catalog-brands/002-b.md": plan_text(status="approved", depends='["001"]'),
            "catalog-brands/003-c.md": plan_text(status="review"),
            "catalog-brands/004-d.md": plan_text(status="draft"),
            "catalog-brands/005-e.md": plan_text(status="blocked"),
        }
    )
    order = [(r[1], r[2]) for r in rows(load_plans(root))]
    assert order == [
        ("catalog-brands/005", "blocked"),
        ("catalog-brands/004", "draft"),
        ("catalog-brands/003", "review"),
        ("catalog-brands/002", "approved"),
        ("catalog-brands/001", "done"),
    ]


def test_unfinished_dependencies_bump_a_plan_to_the_top(make_repo: MakeRepo) -> None:
    root = make_repo(
        {
            "catalog-brands/001-a.md": plan_text(status="testing"),
            "catalog-brands/002-b.md": plan_text(status="approved", depends='["001"]'),
        }
    )
    first = rows(load_plans(root))[0]
    assert first[1] == "catalog-brands/002"
    assert "blocked by unfinished depends_on: catalog-brands/001" in first[5]


def test_invalid_plans_point_to_lint(make_repo: MakeRepo) -> None:
    root = make_repo({"catalog-brands/001-a.md": "# no frontmatter\n"})
    assert "just plans-lint" in rows(load_plans(root))[0][5]


def test_done_plans_are_hidden_unless_all(
    make_repo: MakeRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "catalog-brands/001-a.md": plan_text(status="done"),
            "catalog-brands/002-b.md": plan_text(status="draft"),
        },
        {
            "catalog-thing.md": (
                "---\nstatus: open\nmodule: catalog\nfound: 2026-10-02\n---\n\n# Thing\n"
            )
        },
    )
    assert main([], root) == 0
    out = capsys.readouterr().out
    assert "catalog-brands/002" in out and "catalog-brands/001" not in out
    assert "1 done/superseded hidden" in out
    assert "catalog-thing.md: Thing" in out

    assert main(["--all"], root) == 0
    assert "catalog-brands/001" in capsys.readouterr().out


def test_unknown_initiative_fails(make_repo: MakeRepo) -> None:
    root = make_repo({"catalog-brands/001-a.md": plan_text()})
    assert main(["nope"], root) == 1
