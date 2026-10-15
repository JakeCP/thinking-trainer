"""Structured-output schemas. The prompts in prompts/ describe what each field means."""

from typing import Literal

from pydantic import BaseModel


class Issue(BaseModel):
    code: str
    detail: str


class ExtractedProblem(BaseModel):
    target_ids: list[str]
    label: str
    parts: str
    statement: str
    solution: str | None
    issues: list[Issue]


class MissingTarget(BaseModel):
    target_id: str
    reason: str


class Extraction(BaseModel):
    problems: list[ExtractedProblem]
    missing_targets: list[MissingTarget]
    document_issues: list[Issue]


Verdict = Literal["correct", "partially_correct", "incorrect", "ungradeable"]


class PartVerdict(BaseModel):
    label: str
    verdict: Verdict


class Grade(BaseModel):
    verdict: Verdict
    parts: list[PartVerdict]
    where_wrong: str | None
    alternative_method: bool
    reasoning_shown: bool
    legibility: Literal["n/a", "ok", "partly_illegible", "illegible"]
    solution_concern: str | None
