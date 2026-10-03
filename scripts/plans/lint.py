"""Validate every initiative, plan and finding: `uv run just plans-lint`.

Exit 1 with one `where: message` line per problem. Rules: docs/harness/conventions/plans.md.
"""

import re
import sys
from pathlib import Path

from plans.lib import (
    EVIDENCE_FROM,
    FINDING_STATUSES,
    KEBAB,
    PLAN_FILE,
    PLANS_DIR,
    REQUIRED_SECTIONS,
    ROOT,
    STATUSES,
    TIERS,
    Plan,
    initiatives,
    is_empty,
    is_plan_file,
    load_findings,
    load_plans,
    load_registry,
    owner_module,
    reached,
    rel,
    resolve_ref,
)

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _scalar(frontmatter: dict[str, object], key: str) -> str | None:
    """The field as text; lists/maps become their repr so membership checks never crash."""
    value = frontmatter.get(key)
    return None if value is None else value if isinstance(value, str) else repr(value)


def lint(root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    modules = load_registry(root)

    def err(path: Path, message: str) -> None:
        errors.append(f"{rel(path, root)}: {message}")

    for path in sorted((root / PLANS_DIR).glob("*.md")):
        if is_plan_file(path):
            err(path, "plans live inside an initiative directory, not in plans/")
    for directory in initiatives(root):
        if not KEBAB.match(directory.name):
            err(directory, "initiative name must be kebab-case")
        if owner_module(directory.name, modules) is None:
            err(
                directory,
                "initiative must be named <module>-<topic> (module from docs/modules.json)",
            )
        if not (directory / "README.md").exists():
            err(directory, "initiative needs a README.md (copy plans/_INITIATIVE.md)")
        for nested in sorted(p for p in directory.iterdir() if p.is_dir()):
            err(nested, "plans are one level deep: no subdirectories inside an initiative")

    plans = load_plans(root)
    by_ref: dict[str, Plan] = {}
    for plan in plans:
        if not PLAN_FILE.match(plan.path.name):
            err(plan.path, "file name must be NNN-slug.md (kebab-case slug)")
            continue
        if plan.ref in by_ref:
            err(plan.path, f"number {plan.number} is already used in {plan.initiative}")
        by_ref[plan.ref] = plan

    for plan in plans:
        if PLAN_FILE.match(plan.path.name):
            _lint_plan(plan, by_ref, modules, err)

    errors.extend(_cycles(plans, by_ref, root))

    for finding in load_findings(root):
        where = finding.path
        if not KEBAB.match(finding.path.stem):
            err(where, "finding file name must be kebab-case")
        if finding.error:
            err(where, finding.error)
            continue
        fm = finding.frontmatter
        if fm is None:
            err(where, "missing frontmatter (copy plans/_FINDING.md)")
            continue
        status = _scalar(fm, "status")
        if status not in FINDING_STATUSES:
            err(where, f"status must be one of {', '.join(FINDING_STATUSES)}")
        if _scalar(fm, "module") not in modules:
            err(where, f"module {fm.get('module')!r} is not in docs/modules.json")
        if not _DATE.match(str(fm.get("found", ""))):
            err(where, "found must be a date YYYY-MM-DD")
        if status in ("planned", "resolved"):
            target = str(fm.get("plan") or "")
            if "/" not in target or resolve_ref(target, "") not in by_ref:
                err(where, f"status {status} needs plan: <initiative>/NNN of an existing plan")
    return errors


def _lint_plan(plan: Plan, by_ref: dict[str, Plan], modules: set[str], err) -> None:  # noqa: ANN001
    if plan.error:
        err(plan.path, plan.error)
        return
    fm = plan.frontmatter
    if fm is None:
        err(plan.path, "missing frontmatter (copy plans/_TEMPLATE.md)")
        return
    status = _scalar(fm, "status")
    if status not in STATUSES:
        err(plan.path, f"status must be one of {', '.join(STATUSES)}")
    module = _scalar(fm, "module")
    if module not in modules:
        err(plan.path, f"module {fm.get('module')!r} is not in docs/modules.json")
    elif module != owner_module(plan.initiative, modules):
        err(plan.path, f"module {module!r} does not own initiative {plan.initiative!r}")
    if _scalar(fm, "min_implementer") not in TIERS:
        err(plan.path, f"min_implementer must be one of {', '.join(TIERS)}")
    depends = fm.get("depends_on", [])
    if not isinstance(depends, list):
        err(plan.path, "depends_on must be a list")
        depends = []
    if "superseded_by" in fm and status != "superseded":
        err(plan.path, "superseded_by is only allowed with status: superseded")
    for dependency in depends:
        target = resolve_ref(dependency, plan.initiative)
        if target is None or target not in by_ref:
            err(plan.path, f"depends_on {dependency!r} does not resolve to an existing plan")
        elif target == plan.ref:
            err(plan.path, "a plan cannot depend on itself")
        elif reached(status, "implementing") and by_ref[target].status != "done":
            err(plan.path, f"depends_on {target} must be done before status {status}")
    present = [s for s in plan.section_order if s in REQUIRED_SECTIONS]
    missing = [s for s in REQUIRED_SECTIONS if s not in plan.sections]
    if missing:
        err(plan.path, f"missing sections: {', '.join(missing)}")
    if present != [s for s in REQUIRED_SECTIONS if s in present]:
        err(plan.path, f"sections out of order; expected {', '.join(REQUIRED_SECTIONS)}")
    for section, from_status in EVIDENCE_FROM.items():
        if reached(status, from_status) and is_empty(plan.sections.get(section)):
            err(plan.path, f"## {section} must have content from status {from_status}")
    if status not in ("draft", "superseded") and is_empty(plan.sections.get("Out of scope")):
        err(plan.path, "## Out of scope must have content once the plan leaves draft")


def _cycles(plans: list[Plan], by_ref: dict[str, Plan], root: Path) -> list[str]:
    graph: dict[str, list[str]] = {}
    for plan in plans:
        depends = (plan.frontmatter or {}).get("depends_on", [])
        refs = (
            [resolve_ref(d, plan.initiative) for d in depends] if isinstance(depends, list) else []
        )
        graph[plan.ref] = [r for r in refs if r in by_ref and r != plan.ref]
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str, trail: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            cycle = " -> ".join([*trail[trail.index(node) :], node])
            errors.append(f"{rel(by_ref[node].path, root)}: dependency cycle {cycle}")
            return
        visiting.add(node)
        for target in graph.get(node, []):
            visit(target, [*trail, node])
        visiting.discard(node)
        visited.add(node)

    for node in sorted(graph):
        visit(node, [])
    return errors


def main() -> int:
    errors = lint()
    for line in errors:
        print(f"✘ {line}", file=sys.stderr)
    if errors:
        print(
            f"\n{len(errors)} problem(s). Format: docs/harness/conventions/plans.md",
            file=sys.stderr,
        )
        return 1
    print(f"✔ plans OK ({len(load_plans())} plans, {len(load_findings())} findings)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
