"""Spaced repetition per problem (FSRS), served interleaved across lenses."""

import random
from datetime import datetime, timedelta, timezone

import fsrs
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Attempt, Card, Problem

# FSRS's default learning steps (1 and 10 minutes) suit flashcards. A whole problem
# whose solution you've just read shouldn't come back in a minute, so we go straight
# to day-scale intervals.
SCHEDULER = fsrs.Scheduler(learning_steps=(), relearning_steps=(), desired_retention=0.9)

VERDICT_TO_RATING = {"correct": 3, "partially_correct": 2, "incorrect": 1, "ungradeable": 1}
RATING_LABELS = {1: "Again", 2: "Hard", 3: "Good", 4: "Easy"}


def suggested_rating(verdict: str | None) -> int | None:
    return VERDICT_TO_RATING.get(verdict) if verdict else None


def review(db: Session, problem: Problem, rating: int, now: datetime) -> Card:
    card = problem.card
    fcard = fsrs.Card.from_dict(card.fsrs) if card else fsrs.Card()
    fcard, _ = SCHEDULER.review_card(fcard, fsrs.Rating(rating), review_datetime=now)
    if card is None:
        card = Card(problem_id=problem.id, due=fcard.due, fsrs=fcard.to_dict())
        db.add(card)
        problem.card = card
    else:
        card.due, card.fsrs = fcard.due, fcard.to_dict()
    return card


def local_day_start(now: datetime, tz) -> datetime:
    local = now.astimezone(tz)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def new_introduced_today(db: Session, now: datetime, tz) -> int:
    """Problems whose first-ever rating happened today (local time)."""
    first_rated = (
        select(Attempt.problem_id, func.min(Attempt.rated_at).label("first"))
        .where(Attempt.rated_at.is_not(None))
        .group_by(Attempt.problem_id)
        .subquery()
    )
    return db.scalar(select(func.count()).select_from(first_rated).where(first_rated.c.first >= local_day_start(now, tz))) or 0


def last_lenses(db: Session) -> set[int]:
    last = db.scalar(select(Attempt).order_by(Attempt.submitted_at.desc()).limit(1))
    return last.problem.lens_ids if last else set()


def queue_counts(db: Session, now: datetime, tz, new_per_day: int) -> dict:
    due = db.scalar(select(func.count()).select_from(Card).join(Problem).where(Problem.status == "ok", Card.due <= now)) or 0
    unseen = db.scalar(select(func.count()).select_from(Problem).outerjoin(Card).where(Problem.status == "ok", Card.problem_id.is_(None))) or 0
    new_left = max(0, min(unseen, new_per_day - new_introduced_today(db, now, tz)))
    return {"due": due, "new_available": new_left, "unseen": unseen}


def next_problem(db: Session, now: datetime, tz, new_per_day: int, rng: random.Random | None = None) -> Problem | None:
    """Due reviews (oldest due first), then new problems in random order.

    Random order for new problems matters: the curriculum file is sorted by lens,
    so taking them in file order would announce the lens. Whatever the source,
    the pick avoids repeating any lens of the previous problem when it can.
    """
    rng = rng or random.Random()
    due = list(
        db.scalars(
            select(Problem)
            .join(Card)
            .where(Problem.status == "ok", Card.due <= now)
            .order_by(Card.due)
            .options(selectinload(Problem.lenses))
        )
    )
    new: list[Problem] = []
    if new_introduced_today(db, now, tz) < new_per_day:
        new = list(
            db.scalars(
                select(Problem).outerjoin(Card).where(Problem.status == "ok", Card.problem_id.is_(None)).options(selectinload(Problem.lenses))
            )
        )
        rng.shuffle(new)
    candidates = due + new
    if not candidates:
        return None
    avoid = last_lenses(db)
    return next((p for p in candidates if not (p.lens_ids & avoid)), candidates[0])


def unrated_attempt(db: Session) -> Attempt | None:
    """A submitted attempt you haven't rated yet; the next problem waits for it."""
    return db.scalar(select(Attempt).where(Attempt.rated_at.is_(None)).order_by(Attempt.submitted_at.desc()).limit(1))
