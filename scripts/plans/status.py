"""What needs attention: `uv run just plans-status [initiative] [--all]`.

Plans are ordered by how urgently they need someone (blocked and drafts first); done and
superseded plans are hidden unless `--all`. Without a filter, open findings are listed too.
"""

import argparse
import sys
from pathlib import Path

from plans.lib import ROOT, Plan, initiatives, load_findings, load_plans, rel, resolve_ref

PRIORITY = {
    "blocked": 0,
    "draft": 1,
    "verify": 2,
    "review": 3,
    "testing": 4,
    "implementing": 5,
    "approved": 6,
    "done": 8,
    "superseded": 9,
}
NEXT = {
    "draft": "summarise the plan and ask the user to approve it",
    "approved": "implement (skill implement / subagent implementer)",
    "implementing": "resume implementing; read ## Deviations first",
    "testing": "layered tests (skill write-tests / subagent tester)",
    "review": "checklist + bug hunt (subagent reviewer)",
    "verify": "drive the running app (skill verify / subagent verifier)",
    "blocked": "report why to the user; do not work around it",
    "done": "nothing (changes need a new plan or a fast-lane fix)",
    "superseded": "nothing (follow superseded_by)",
}
TITLE_WIDTH = 50


def rows(plans: list[Plan]) -> list[tuple[int, str, str, str, str, str]]:
    status_by_ref = {plan.ref: plan.status for plan in plans}
    result = []
    for plan in plans:
        fm = plan.frontmatter
        if plan.error or fm is None or plan.status not in PRIORITY:
            result.append((0, plan.ref, "?", "?", plan.title, "invalid plan: run just plans-lint"))
            continue
        status = plan.status
        priority, action = PRIORITY[status], NEXT[status]
        if status in ("approved", "implementing"):
            depends = fm.get("depends_on", []) if isinstance(fm.get("depends_on"), list) else []
            pending = [
                ref
                for d in depends
                if (ref := resolve_ref(d, plan.initiative)) and status_by_ref.get(ref) != "done"
            ]
            if pending:
                priority, action = 0, f"blocked by unfinished depends_on: {', '.join(pending)}"
        title = (
            plan.title if len(plan.title) <= TITLE_WIDTH else plan.title[: TITLE_WIDTH - 1] + "…"
        )
        result.append(
            (priority, plan.ref, status, str(fm.get("min_implementer", "?")), title, action)
        )
    return sorted(result, key=lambda row: (row[0], row[1]))


def render(table: list[tuple[str, ...]]) -> str:
    headers = ("plan", "status", "tier", "title", "next")
    widths = [max(len(str(r[i])) for r in [headers, *table]) for i in range(len(headers))]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True))]
    lines.append("  ".join("-" * w for w in widths))
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(r, widths, strict=True)) for r in table]
    return "\n".join(lines)


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description="What needs attention in plans/")
    parser.add_argument("initiative", nargs="?", help="only this initiative")
    parser.add_argument("--all", action="store_true", help="include done and superseded plans")
    args = parser.parse_args(argv)

    plans = load_plans(root)
    if args.initiative:
        names = {d.name for d in initiatives(root)}
        if args.initiative not in names:
            print(f"✘ unknown initiative {args.initiative!r}", file=sys.stderr)
            return 1
        plans = [p for p in plans if p.initiative == args.initiative]

    all_rows = rows(plans)
    visible = [r for r in all_rows if args.all or r[2] not in ("done", "superseded")]
    hidden = len(all_rows) - len(visible)
    if visible:
        print(render([r[1:] for r in visible]))
    else:
        print("Nothing needs attention.")
    if hidden:
        print(f"\n({hidden} done/superseded hidden; use --all)")

    if not args.initiative:
        open_findings = [
            f for f in load_findings(root) if (f.frontmatter or {}).get("status") == "open"
        ]
        if open_findings:
            print("\nOpen findings (the user decides: deferred / planned / discarded):")
            for finding in open_findings:
                print(f"  - {rel(finding.path, root)}: {finding.title}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
