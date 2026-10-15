"""Feature 1 schema. Feature 2/3 tables arrive in later migrations."""

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, UTCDateTime


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Lens(Base):
    __tablename__ = "lens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # from the curriculum file
    name: Mapped[str] = mapped_column(String)
    question: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text)
    prerequisites: Mapped[list] = mapped_column(JSON, default=list)
    resources: Mapped[list] = mapped_column(JSON, default=list)
    gaps: Mapped[str] = mapped_column(Text, default="")
    tell_words: Mapped[list] = mapped_column(JSON, default=list)  # from content/lens_terms.yaml


class SourceDoc(Base):
    __tablename__ = "source_doc"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(String, unique=True)
    kind: Mapped[str | None] = mapped_column(String)  # pdf | html
    sha256: Mapped[str | None] = mapped_column(String)
    raw_path: Mapped[str | None] = mapped_column(String)
    text_layer: Mapped[str] = mapped_column(Text, default="")
    has_text_layer: Mapped[bool] = mapped_column(Boolean, default=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Problem(Base):
    __tablename__ = "problem"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Identity for idempotent re-ingest: document pair + label as the extractor reported it,
    # or the curriculum target key for items that never went through extraction.
    ingest_key: Mapped[str] = mapped_column(String, unique=True)
    problem_doc_id: Mapped[int | None] = mapped_column(ForeignKey("source_doc.id"))
    solution_doc_id: Mapped[int | None] = mapped_column(ForeignKey("source_doc.id"))
    problem_url: Mapped[str] = mapped_column(String)
    solution_url: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)  # "MIT OCW 18.05, Spring 2022"
    source_label: Mapped[str] = mapped_column(String, default="")  # hidden until answered
    kind: Mapped[str] = mapped_column(String)  # exercise | lab | worked_example | original
    situation_based: Mapped[bool] = mapped_column(Boolean, default=False)
    parts: Mapped[str] = mapped_column(String, default="")
    statement_original: Mapped[str] = mapped_column(Text, default="")  # as extracted; never edited
    statement_served: Mapped[str | None] = mapped_column(Text)  # your override
    solution: Mapped[str] = mapped_column(Text, default="")
    # ok | needs_review | rejected | desk (labs, worked examples) | excluded
    status: Mapped[str] = mapped_column(String, index=True)
    flags: Mapped[list] = mapped_column(JSON, default=list)  # [{code, detail}]
    coverage: Mapped[float | None] = mapped_column(Float)
    extraction_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_call.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    desk_done_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    lenses: Mapped[list["ProblemLens"]] = relationship(back_populates="problem", cascade="all, delete-orphan")
    card: Mapped["Card | None"] = relationship(back_populates="problem", cascade="all, delete-orphan")

    @property
    def statement(self) -> str:
        return self.statement_served if self.statement_served is not None else self.statement_original

    @property
    def lens_ids(self) -> set[int]:
        return {pl.lens_id for pl in self.lenses}


class ProblemLens(Base):
    """Many-to-many: the same exam question can be listed under several lenses."""

    __tablename__ = "problem_lens"
    __table_args__ = (UniqueConstraint("problem_id", "target_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problem.id", ondelete="CASCADE"))
    lens_id: Mapped[int] = mapped_column(ForeignKey("lens.id"))
    target_key: Mapped[str] = mapped_column(String)  # "<lens id>:<index in that lens's problems>"
    problem_ref: Mapped[str] = mapped_column(Text)
    curator_notes: Mapped[str] = mapped_column(Text, default="")

    problem: Mapped[Problem] = relationship(back_populates="lenses")
    lens: Mapped[Lens] = relationship()


class Card(Base):
    """FSRS state. A problem has no card until its first rating."""

    __tablename__ = "card"

    problem_id: Mapped[int] = mapped_column(ForeignKey("problem.id", ondelete="CASCADE"), primary_key=True)
    due: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    fsrs: Mapped[dict] = mapped_column(JSON)  # fsrs.Card.to_dict()

    problem: Mapped[Problem] = relationship(back_populates="card")


class Attempt(Base):
    __tablename__ = "attempt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    problem_id: Mapped[int] = mapped_column(ForeignKey("problem.id", ondelete="CASCADE"), index=True)
    shown_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    answer_text: Mapped[str] = mapped_column(Text, default="")
    photo_paths: Mapped[list] = mapped_column(JSON, default=list)
    confidence: Mapped[int | None] = mapped_column(Integer)  # 0-100, entered before the grade
    verdict: Mapped[str | None] = mapped_column(String)
    feedback: Mapped[str | None] = mapped_column(Text)
    grade: Mapped[dict | None] = mapped_column(JSON)
    solution_concern: Mapped[str | None] = mapped_column(Text)
    grading_error: Mapped[str | None] = mapped_column(Text)
    grading_call_id: Mapped[int | None] = mapped_column(ForeignKey("llm_call.id"))
    rating: Mapped[int | None] = mapped_column(Integer)  # 1-4, yours; what FSRS consumes
    rated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    problem: Mapped[Problem] = relationship()


class LlmCall(Base):
    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String)
    prompt_name: Mapped[str] = mapped_column(String)
    prompt_sha: Mapped[str] = mapped_column(String)
    model: Mapped[str] = mapped_column(String)  # requested
    served_model: Mapped[str | None] = mapped_column(String)  # may differ after a fallback
    effort: Mapped[str | None] = mapped_column(String)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    stop_reason: Mapped[str | None] = mapped_column(String)
    request_text: Mapped[str] = mapped_column(Text, default="")  # text blocks only, no binaries
    response_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
