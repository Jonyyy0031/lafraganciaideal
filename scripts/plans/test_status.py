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


def test_every_status_has_its_priority_in_the_documented_order(make_repo: MakeRepo) -> None:
    statuses = [
        "superseded", "done", "approved", "implementing", "testing", "review", "verify",
        "draft", "blocked",
    ]  # fmt: skip
    root = make_repo(
        {f"catalog-brands/{i:03d}-x.md": plan_text(status=s) for i, s in enumerate(statuses, 1)}
    )

    assert [r[2] for r in rows(load_plans(root))] == [
        "blocked", "draft", "verify", "review", "testing", "implementing", "approved", "done",
        "superseded",
    ]  # fmt: skip


def test_an_implementing_plan_with_unfinished_dependencies_is_bumped(make_repo: MakeRepo) -> None:
    root = make_repo(
        {
            "catalog-brands/001-a.md": plan_text(status="draft"),
            "catalog-brands/002-b.md": plan_text(status="implementing", depends='["001"]'),
            "catalog-brands/003-c.md": plan_text(status="blocked"),
        }
    )

    first, second = rows(load_plans(root))[:2]

    assert (first[0], first[1]) == (0, "catalog-brands/002")
    assert (second[0], second[1]) == (0, "catalog-brands/003")


def test_a_long_title_is_truncated_to_fifty_characters(make_repo: MakeRepo) -> None:
    text = plan_text().replace("# 001 — A plan", "# " + "t" * 80)
    root = make_repo({"catalog-brands/001-a.md": text})

    title = rows(load_plans(root))[0][4]

    assert len(title) == 50 and title.endswith("…")


def test_rows_carry_tier_and_next_action(make_repo: MakeRepo) -> None:
    root = make_repo({"catalog-brands/001-a.md": plan_text(status="testing", tier="high")})

    row = rows(load_plans(root))[0]

    assert row[3] == "high" and "write-tests" in row[5]


OPEN = "---\nstatus: {s}\nmodule: catalog\nfound: 2026-10-02\n---\n\n# {s} finding\n"


def test_filtering_by_initiative_hides_other_initiatives_and_findings(
    make_repo: MakeRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {
            "catalog-brands/001-a.md": plan_text(),
            "platform-x/001-b.md": plan_text(module="platform"),
        },
        {"catalog-thing.md": OPEN.format(s="open")},
    )

    assert main(["platform-x"], root) == 0
    out = capsys.readouterr().out
    assert "platform-x/001" in out
    assert "catalog-brands/001" not in out and "Open findings" not in out


def test_nothing_needs_attention_when_only_done_plans_exist(
    make_repo: MakeRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo({"catalog-brands/001-a.md": plan_text(status="done")})

    assert main([], root) == 0
    out = capsys.readouterr().out
    assert "Nothing needs attention." in out and "1 done/superseded hidden" in out


def test_only_open_findings_are_listed(
    make_repo: MakeRepo, capsys: pytest.CaptureFixture[str]
) -> None:
    root = make_repo(
        {"catalog-brands/001-a.md": plan_text()},
        {"catalog-a.md": OPEN.format(s="open"), "catalog-b.md": OPEN.format(s="discarded")},
    )

    main([], root)

    out = capsys.readouterr().out
    assert "open finding" in out and "discarded finding" not in out
