"""Read content/curriculum.json into targets grouped by document pair."""

import json
from dataclasses import dataclass
from pathlib import Path

KINDS = ("exercise", "lab", "worked_example", "original")


@dataclass(frozen=True)
class Target:
    key: str  # "<lens id>:<index>", stable while the file's order is
    lens_id: int
    source: str
    problem_ref: str
    problem_url: str
    solution_url: str
    situation_based: bool
    notes: str
    kind: str
    kind_inferred: bool

    @property
    def is_relative(self) -> bool:
        return not self.problem_url.startswith(("http://", "https://"))


def infer_kind(entry: dict) -> tuple[str, bool]:
    """Kind from an explicit field, else from the markers the curriculum file uses."""
    if entry.get("kind") in KINDS:
        return entry["kind"], False
    notes = entry.get("notes", "")
    if entry.get("source", "").startswith("Original substitute"):
        return "original", True
    if "WORKED-LAB SUBSTITUTE" in notes:
        return "lab", True
    if "WORKED-EXAMPLE SUBSTITUTE" in notes:
        return "worked_example", True
    return "exercise", True


def load_curriculum(path: Path) -> tuple[list[dict], list[Target]]:
    lenses = json.loads(path.read_text())
    targets = []
    for lens in lenses:
        for i, p in enumerate(lens["problems"]):
            kind, inferred = infer_kind(p)
            targets.append(
                Target(
                    key=f"{lens['id']}:{i}",
                    lens_id=lens["id"],
                    source=p["source"],
                    problem_ref=p["problem_ref"],
                    problem_url=p["problem_url"],
                    solution_url=p["solution_url"],
                    situation_based=bool(p.get("situation_based")),
                    notes=p.get("notes", ""),
                    kind=kind,
                    kind_inferred=inferred,
                )
            )
    return lenses, targets


def group_by_document(targets: list[Target]) -> dict[tuple[str, str], list[Target]]:
    """Exercises sharing a problem/solution document pair go to the extractor together."""
    groups: dict[tuple[str, str], list[Target]] = {}
    for t in targets:
        if t.kind == "exercise":
            groups.setdefault((t.problem_url, t.solution_url), []).append(t)
    return groups
