"""One Claude call per document pair, carrying every curriculum target that points at it."""

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.ingest.curriculum import Target
from app.llm import Llm, pdf_block, plain_document_block, text_block
from app.models import LlmCall, SourceDoc
from app.schemas import Extraction


def document_block(doc: SourceDoc, title: str) -> dict:
    if doc.kind == "pdf":
        return pdf_block(Path(doc.raw_path).read_bytes(), title)
    return plain_document_block(doc.text_layer, title)


def build_content(problem_doc: SourceDoc, solution_doc: SourceDoc, targets: list[Target], lens_names: dict[int, str]) -> list[dict]:
    same = problem_doc.id == solution_doc.id
    blocks = [document_block(problem_doc, "problem_doc" + (" (also the solution_doc)" if same else ""))]
    if not same:
        blocks.append(document_block(solution_doc, "solution_doc"))
    else:
        blocks.append(text_block("solution_doc", "Same document as problem_doc: statements and solutions are together."))
    targets_json = [
        {
            "id": t.key,
            "lens": f"{t.lens_id}: {lens_names[t.lens_id]}",
            "problem_ref": t.problem_ref,
            "curator_notes": t.notes,
        }
        for t in targets
    ]
    blocks.append(text_block("targets", json.dumps(targets_json, indent=2, ensure_ascii=False)))
    return blocks


def extract(
    db: Session, llm: Llm, problem_doc: SourceDoc, solution_doc: SourceDoc, targets: list[Target], lens_names: dict[int, str]
) -> tuple[Extraction, LlmCall]:
    content = build_content(problem_doc, solution_doc, targets, lens_names)
    return llm.call(db, prompt_name="extract_problems", purpose="extract", content=content, output=Extraction)
