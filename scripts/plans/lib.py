"""Shared parsing for the plan tooling. Rules: docs/harness/conventions/plans.md."""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PLANS_DIR = "plans"
FINDINGS_DIR = "findings"

STATUSES = (
    "draft",
    "approved",
    "implementing",
    "testing",
    "review",
    "verify",
    "blocked",
    "done",
    "superseded",
)
PIPELINE = ("draft", "approved", "implementing", "testing", "review", "verify", "done")
TIERS = ("small", "mid", "high")
FINDING_STATUSES = ("open", "deferred", "planned", "resolved", "discarded")

REQUIRED_SECTIONS = (
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
# A section must have content once the plan reaches this status.
EVIDENCE_FROM = {
    "Deviations": "testing",
    "Test coverage": "review",
    "Review findings": "verify",
    "Verification": "done",
}

KEBAB = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
PLAN_FILE = re.compile(r"^(\d{3})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BACKTICKED = re.compile(r"`([^`]+)`")
# A backticked token followed by a change marker is a path even without `/` or `.` (justfile).
_MARKED = re.compile(r"`([^`\s]+)`\s*\((?:create|modify|delete)\)")
_FILES_ENTRY = re.compile(r"^(?:-\s+)?files:", re.IGNORECASE)
_STEP = re.compile(r"^\d+\.\s")
_FENCE = re.compile(r"^\s*(```|~~~)")


class FrontmatterError(ValueError):
    pass


@dataclass
class Document:
    path: Path
    frontmatter: dict[str, Any] | None
    title: str
    sections: dict[str, str] = field(default_factory=dict)
    section_order: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class Plan(Document):
    initiative: str = ""
    number: str = ""

    @property
    def ref(self) -> str:
        return f"{self.initiative}/{self.number}"

    @property
    def status(self) -> str | None:
        value = (self.frontmatter or {}).get("status")
        return value if isinstance(value, str) else None


def rel(path: Path, root: Path = ROOT) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def parse_document(path: Path) -> Document:
    text = path.read_text(encoding="utf-8")
    frontmatter: dict[str, Any] | None = None
    error: str | None = None
    body = text
    match = _FRONTMATTER.match(text)
    if match:
        body = text[match.end() :]
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            error = f"invalid YAML frontmatter: {exc.__class__.__name__}"
        else:
            if isinstance(loaded, dict):
                frontmatter = {k: _normalize(v) for k, v in loaded.items()}
            else:
                error = "frontmatter must be a YAML mapping"
    title = ""
    sections: dict[str, str] = {}
    order: list[str] = []
    current: str | None = None
    lines: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and not title and line.startswith("# "):
            title = line[2:].strip()
            continue
        if not in_fence and line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(lines)
            current = line[3:].strip()
            order.append(current)
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        sections[current] = "\n".join(lines)
    return Document(path, frontmatter, title, sections, order, error)


def _normalize(value: Any) -> Any:
    return value.isoformat() if isinstance(value, date) else value


def is_empty(section: str | None) -> bool:
    """A section is empty when it only holds HTML comments and whitespace."""
    return section is None or not _COMMENT.sub("", section).strip()


def load_registry(root: Path = ROOT) -> set[str]:
    data = json.loads((root / "docs/modules.json").read_text(encoding="utf-8"))
    return {module["name"] for module in data["modules"]}


def owner_module(initiative: str, modules: set[str]) -> str | None:
    """The longest registry module equal to the initiative or prefixing it as `name-`."""
    candidates = [m for m in modules if initiative == m or initiative.startswith(f"{m}-")]
    return max(candidates, key=len, default=None)


def initiatives(root: Path = ROOT) -> list[Path]:
    base = root / PLANS_DIR
    return sorted(
        p
        for p in base.iterdir()
        if p.is_dir() and p.name != FINDINGS_DIR and not p.name.startswith(".")
    )


def is_plan_file(path: Path) -> bool:
    return path.suffix == ".md" and not path.name.startswith("_") and path.name != "README.md"


def load_plans(root: Path = ROOT) -> list[Plan]:
    plans: list[Plan] = []
    for directory in initiatives(root):
        for path in sorted(directory.glob("*.md")):
            if not is_plan_file(path):
                continue
            doc = parse_document(path)
            match = PLAN_FILE.match(path.name)
            plans.append(
                Plan(
                    path=doc.path,
                    frontmatter=doc.frontmatter,
                    title=doc.title,
                    sections=doc.sections,
                    section_order=doc.section_order,
                    error=doc.error,
                    initiative=directory.name,
                    number=match.group(1) if match else "",
                )
            )
    return plans


def load_findings(root: Path = ROOT) -> list[Document]:
    base = root / PLANS_DIR / FINDINGS_DIR
    if not base.is_dir():
        return []
    return [parse_document(p) for p in sorted(base.glob("*.md")) if is_plan_file(p)]


def resolve_ref(ref: object, initiative: str) -> str | None:
    """`"002"` → same initiative; `"catalog-perfumes/2"` → another one. Numbers are zero-padded."""
    text = str(ref).strip()
    if "/" in text:
        other, number = text.rsplit("/", 1)
    else:
        other, number = initiative, text
    if not number.isdigit() or not other:
        return None
    return f"{other}/{int(number):03d}"


def declared_files(plan: Document) -> list[str]:
    """Backticked paths on `Files:` entries inside `## Steps`.

    An entry starts at a line whose text begins with `Files:` (optionally as a `- ` bullet) and
    continues on the following wrapped lines until the next bullet (`- Do:`), numbered step or
    blank line. A `Files:` mention anywhere else (e.g. in a `- Do:` line) declares nothing.
    """
    files: list[str] = []
    entry_lines: list[str] = []
    in_entry = False
    for line in plan.sections.get("Steps", "").splitlines():
        stripped = line.strip()
        if _FILES_ENTRY.match(stripped):
            in_entry = True
        elif in_entry and (not stripped or stripped.startswith("- ") or _STEP.match(stripped)):
            in_entry = False
        if in_entry:
            entry_lines.append(line)
    for line in entry_lines:
        marked = set(_MARKED.findall(line))
        for token in _BACKTICKED.findall(line):
            token = token.strip()
            looks_like_path = "/" in token or "." in token or token in marked
            if " " in token or not looks_like_path:
                continue
            path = token.removeprefix("./")
            if path not in files:
                files.append(path)
    return files


def reached(status: str | None, target: str) -> bool:
    """True when `status` is at or beyond `target` in the pipeline."""
    if status not in PIPELINE or target not in PIPELINE:
        return False
    return PIPELINE.index(status) >= PIPELINE.index(target)
