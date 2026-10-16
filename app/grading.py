"""Grade an attempt against the official solution."""

import io
import uuid
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy.orm import Session

from app.llm import Llm, LlmError, image_block, text_block
from app.models import Attempt
from app.schemas import Grade

MAX_PHOTO_SIDE = 1600


def save_photo(data: bytes, uploads_dir: Path) -> str:
    """Downscale and re-encode a phone photo; returns the stored file name."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img).convert("RGB")  # phones store rotation in EXIF
    img.thumbnail((MAX_PHOTO_SIDE, MAX_PHOTO_SIDE))
    uploads_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(uploads_dir / name, "JPEG", quality=85)
    return name


def build_content(attempt: Attempt, uploads_dir: Path) -> list[dict]:
    problem = attempt.problem
    blocks = [text_block("problem", problem.statement), text_block("official_solution", problem.solution or "(none)")]
    notes = "\n".join(f"- {pl.curator_notes}" for pl in problem.lenses if pl.curator_notes)
    if notes:
        blocks.append(text_block("curator_notes", notes))
    blocks.append(text_block("answer", attempt.answer_text.strip() or "(no typed answer)"))
    for name in attempt.photo_paths:
        blocks.append(image_block((uploads_dir / name).read_bytes()))
    if attempt.photo_paths:
        blocks.append({"type": "text", "text": f"The {len(attempt.photo_paths)} image(s) above are photos of their handwritten work."})
    return blocks


def grade(db: Session, llm: Llm, attempt: Attempt, uploads_dir: Path) -> Attempt:
    """Fill in the attempt's grade. On failure, records the error and leaves verdict empty."""
    try:
        result, call = llm.call(db, prompt_name="grade_problem", purpose="grade", content=build_content(attempt, uploads_dir), output=Grade)
    except LlmError as e:
        attempt.grading_error = str(e)
        attempt.grading_call_id = e.call_id
        db.commit()
        return attempt
    attempt.grade = result.model_dump()
    attempt.verdict = result.verdict
    attempt.verdict_source = "llm"
    attempt.feedback = result.where_wrong
    attempt.solution_concern = result.solution_concern
    attempt.grading_error = None
    attempt.grading_call_id = call.id
    db.commit()
    return attempt
