"""Ingest orchestration: curriculum → lenses, documents, problems.

Re-runnable. A document pair that has already been extracted is skipped unless
re_extract is set, and re-extraction never overwrites a problem you've reviewed.
Nothing in the curriculum is dropped silently: anything that can't be fetched,
found or ingested becomes a needs_review placeholder that says why.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ingest.curriculum import Target, group_by_document, load_curriculum
from app.ingest.extract import extract
from app.ingest.fetch import fetch
from app.ingest.originals import load_original
from app.ingest.verify import check_problem, flag
from app.llm import Llm, LlmError
from app.models import Attempt, Lens, Problem, ProblemLens

log = logging.getLogger("ingest")


@dataclass
class Report:
    lenses: int = 0
    pairs_extracted: int = 0
    pairs_skipped: int = 0
    pairs_failed: int = 0
    problems_ok: int = 0
    problems_review: int = 0
    desk_items: int = 0
    originals: int = 0
    inferred_kinds: dict[str, int] = field(default_factory=dict)

    def lines(self) -> list[str]:
        return [
            f"lenses: {self.lenses}",
            f"document pairs: {self.pairs_extracted} extracted, {self.pairs_skipped} already done, {self.pairs_failed} failed",
            f"problems from this run: {self.problems_ok} ok, {self.problems_review} need review",
            f"desk items (labs, worked examples): {self.desk_items}",
            f"original substitutes: {self.originals}",
            "kinds inferred from notes/source markers: " + (", ".join(f"{k}={v}" for k, v in sorted(self.inferred_kinds.items())) or "none"),
        ]


def load_lens_terms(content_dir: Path) -> dict[int, list[str]]:
    path = content_dir / "lens_terms.yaml"
    return {int(k): list(v) for k, v in (yaml.safe_load(path.read_text()) or {}).items()} if path.exists() else {}


def upsert_lenses(db: Session, lenses: list[dict], terms: dict[int, list[str]]) -> None:
    for entry in lenses:
        lens = db.get(Lens, entry["id"]) or Lens(id=entry["id"])
        lens.name, lens.question, lens.summary = entry["name"], entry["question"], entry["summary"]
        lens.prerequisites, lens.resources = entry.get("prerequisites", []), entry.get("resources", [])
        lens.gaps = entry.get("gaps", "")
        lens.tell_words = terms.get(entry["id"], [])
        db.add(lens)
    db.commit()


def _attach_targets(problem: Problem, targets: list[Target]) -> None:
    existing = {pl.target_key for pl in problem.lenses}
    for t in targets:
        if t.key not in existing:
            problem.lenses.append(ProblemLens(lens_id=t.lens_id, target_key=t.key, problem_ref=t.problem_ref, curator_notes=t.notes))


def _placeholder(db: Session, target: Target, status: str, flags: list[dict], *, kind: str | None = None) -> Problem:
    """A row for a curriculum entry that has no extracted text (yet)."""
    key = f"target:{target.key}"
    problem = db.scalar(select(Problem).where(Problem.ingest_key == key))
    if problem is None:
        problem = Problem(ingest_key=key, problem_url=target.problem_url, solution_url=target.solution_url, source=target.source)
        db.add(problem)
    if problem.reviewed_at is None:
        problem.kind = kind or target.kind
        problem.status = status
        problem.flags = flags
        problem.source_label = target.problem_ref
        problem.situation_based = target.situation_based
    _attach_targets(problem, [target])
    return problem


def _normalize_label(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", label.lower()).strip()


def ingest_pair(db: Session, llm: Llm, settings, targets: list[Target], lens_names, lens_terms, *, refetch: bool, re_extract: bool, report: Report, http_client=None) -> None:
    problem_url, solution_url = targets[0].problem_url, targets[0].solution_url
    pair_prefix = f"pair:{problem_url}|{solution_url}|"

    already = db.scalar(select(Problem.id).where(Problem.ingest_key.startswith(pair_prefix)).limit(1))
    if already and not re_extract and not refetch:
        report.pairs_skipped += 1
        return

    pdoc = fetch(db, problem_url, settings.cache_dir, refetch=refetch, client=http_client)
    sdoc = pdoc if solution_url == problem_url else fetch(db, solution_url, settings.cache_dir, refetch=refetch, client=http_client)
    failed = list({d.id: d for d in (pdoc, sdoc) if d.error}.values())
    if failed:
        report.pairs_failed += 1
        if already:  # keep what an earlier run extracted; just report
            log.warning("refetch failed for %s; keeping the earlier extraction", problem_url)
            return
        detail = "; ".join(f"{d.url}: {d.error}" for d in failed)
        for t in targets:
            _placeholder(db, t, "needs_review", [flag("fetch_failed", detail)])
            report.problems_review += 1
        db.commit()
        return

    try:
        extraction, call = extract(db, llm, pdoc, sdoc, targets, lens_names)
    except LlmError as e:
        report.pairs_failed += 1
        for t in targets:
            _placeholder(db, t, "needs_review", [flag("extraction_failed", f"{e} (llm_call {e.call_id})")])
            report.problems_review += 1
        db.commit()
        return
    report.pairs_extracted += 1

    by_key = {t.key: t for t in targets}
    covered: set[str] = set()
    produced: set[str] = set()
    doc_issues = [i.model_dump() for i in extraction.document_issues]
    for ep in extraction.problems:
        matched = [by_key[k] for k in ep.target_ids if k in by_key]
        issues = [i.model_dump() for i in ep.issues]
        if not matched:
            issues.append({"code": "unknown_target", "detail": f"extractor returned target ids {ep.target_ids}, none of which were requested"})
        terms = sorted({w for t in matched for w in lens_terms.get(t.lens_id, [])})
        flags, cov = check_problem(
            statement=ep.statement,
            solution=ep.solution,
            issues=issues,
            document_issues=doc_issues,
            problem_text_layer=pdoc.text_layer,
            solution_text_layer=sdoc.text_layer,
            has_text_layer=pdoc.has_text_layer and sdoc.has_text_layer,
            lens_terms=terms,
        )
        key = pair_prefix + _normalize_label(ep.label)
        produced.add(key)
        problem = db.scalar(select(Problem).where(Problem.ingest_key == key))
        if problem is None:
            problem = Problem(ingest_key=key, problem_url=problem_url, solution_url=solution_url)
            db.add(problem)
        if problem.reviewed_at is None:  # never clobber your review
            problem.problem_doc_id, problem.solution_doc_id = pdoc.id, sdoc.id
            first = matched[0] if matched else targets[0]
            problem.source = first.source
            problem.source_label = ep.label
            problem.kind = "exercise"
            problem.situation_based = any(t.situation_based for t in matched)
            problem.parts = ep.parts
            problem.statement_original = ep.statement
            problem.solution = ep.solution or ""
            problem.flags = flags
            problem.coverage = cov
            problem.extraction_call_id = call.id
            problem.status = "ok" if not flags else "needs_review"
        _attach_targets(problem, matched)
        covered.update(t.key for t in matched)
        if problem.status == "ok":
            report.problems_ok += 1
        else:
            report.problems_review += 1

    _retire_leftovers(db, pair_prefix, produced, covered)
    reasons = {m.target_id: m.reason for m in extraction.missing_targets}
    for t in targets:
        if t.key not in covered:
            _placeholder(db, t, "needs_review", [flag("missing_target", reasons.get(t.key, "the extractor returned nothing for this entry"))])
            report.problems_review += 1
    db.commit()


def _retire_leftovers(db: Session, pair_prefix: str, produced: set[str], covered: set[str]) -> None:
    """After a successful extraction, drop rows it superseded: placeholders for targets it
    now covers, and problems from an earlier extraction of this pair it didn't return.
    Rows you've reviewed or practised are kept and flagged instead of deleted."""
    stale = list(db.scalars(select(Problem).where(Problem.ingest_key.in_([f"target:{k}" for k in covered]))))
    stale += [
        p
        for p in db.scalars(select(Problem).where(Problem.ingest_key.startswith(pair_prefix)))
        if p.ingest_key not in produced
    ]
    for p in stale:
        if p.reviewed_at is None and p.card is None and not db.scalar(select(Attempt.id).where(Attempt.problem_id == p.id).limit(1)):
            db.delete(p)
        elif not any(f["code"] == "superseded" for f in p.flags):
            p.flags = p.flags + [flag("superseded", "a later extraction no longer returns this row; check it against the new one")]
            p.status = "needs_review"
    db.flush()


def run(
    db: Session,
    llm: Llm,
    settings,
    curriculum_path: Path,
    *,
    lens_ids: set[int] | None = None,
    refetch=False,
    re_extract=False,
    originals_dir: Path | None = None,
    http_client=None,
) -> Report:
    report = Report()
    lenses, targets = load_curriculum(curriculum_path)
    terms = load_lens_terms(settings.content_dir)
    upsert_lenses(db, lenses, terms)
    report.lenses = len(lenses)
    lens_names = {entry["id"]: entry["name"] for entry in lenses}

    if lens_ids:
        targets = [t for t in targets if t.lens_id in lens_ids]
    for t in targets:
        if t.kind_inferred and t.kind != "exercise":
            report.inferred_kinds[t.kind] = report.inferred_kinds.get(t.kind, 0) + 1

    for t in targets:
        if t.kind in ("lab", "worked_example"):
            _placeholder(db, t, "desk", [])
            report.desk_items += 1
        elif t.kind == "original":
            report.originals += 1
            ingest_original(db, t, originals_dir, terms)
    db.commit()

    for group in group_by_document(targets).values():
        log.info("pair %s (%d targets)", group[0].problem_url, len(group))
        ingest_pair(db, llm, settings, group, lens_names, terms, refetch=refetch, re_extract=re_extract, report=report, http_client=http_client)
    return report


def ingest_original(db: Session, target: Target, originals_dir: Path | None, terms: dict[int, list[str]]) -> None:
    """Original substitutes come from a local HTML bundle and always go to review first."""
    if originals_dir is None:
        _placeholder(db, target, "excluded", [flag("original_not_loaded", "pass --originals-dir to load the substitute_exercises.html bundle")], kind="original")
        return
    try:
        statement, solution = load_original(originals_dir, target.problem_url, target.solution_url)
    except (OSError, LookupError) as e:
        _placeholder(db, target, "needs_review", [flag("original_not_found", str(e))], kind="original")
        return
    problem = _placeholder(db, target, "needs_review", [], kind="original")
    if problem.reviewed_at is None:
        problem.statement_original, problem.solution = statement, solution
        flags = [flag("original", "an original substitute, not from a course: check it before approving")]
        if hits := [t for t in terms.get(target.lens_id, []) if re.search(r"\b" + re.escape(t), statement.lower())]:
            flags.append(flag("names_lens", "statement contains: " + ", ".join(hits)))
        problem.flags = flags
