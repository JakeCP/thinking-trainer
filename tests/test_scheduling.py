import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import scheduling
from app.models import Attempt, Lens, Problem, ProblemLens

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def seed(db, lens_of: list[int]) -> list[Problem]:
    for lid in set(lens_of):
        db.merge(Lens(id=lid, name=f"L{lid}", question="", summary=""))
    problems = []
    for i, lid in enumerate(lens_of):
        p = Problem(ingest_key=f"k{i}", problem_url="u", solution_url="u", source="s", kind="exercise", status="ok", statement_original=f"P{i}", solution="S")
        p.lenses.append(ProblemLens(lens_id=lid, target_key=f"{lid}:{i}", problem_ref="r"))
        db.add(p)
        problems.append(p)
    db.commit()
    return problems


def answer(db, problem, rating, when):
    db.add(Attempt(problem_id=problem.id, answer_text="x", submitted_at=when, rating=rating, rated_at=when))
    scheduling.review(db, problem, rating, when)
    db.commit()


def test_first_review_is_days_away_not_minutes(db):
    (p,) = seed(db, [1])
    for rating in (1, 3):
        card = scheduling.review(db, p, rating, NOW)
        assert card.due - NOW >= timedelta(hours=12), rating


def test_new_problems_capped_per_day(db):
    problems = seed(db, [1, 2, 3, 4])
    for p in problems[:2]:
        answer(db, p, 3, NOW - timedelta(hours=1))
    assert scheduling.next_problem(db, NOW, UTC, new_per_day=2) is None
    assert scheduling.next_problem(db, NOW, UTC, new_per_day=3) is not None
    # Yesterday's introductions don't count against today.
    assert scheduling.next_problem(db, NOW + timedelta(days=1), UTC, new_per_day=2) is not None


def test_avoids_the_previous_lens_when_it_can(db):
    problems = seed(db, [1, 1, 1, 2])
    answer(db, problems[0], 3, NOW - timedelta(minutes=5))
    for seed_ in range(20):
        nxt = scheduling.next_problem(db, NOW, UTC, new_per_day=10, rng=random.Random(seed_))
        assert nxt.lens_ids == {2}


def test_falls_back_to_same_lens_when_nothing_else(db):
    problems = seed(db, [1, 1])
    answer(db, problems[0], 3, NOW - timedelta(minutes=5))
    assert scheduling.next_problem(db, NOW, UTC, new_per_day=10).id == problems[1].id


def test_due_reviews_come_back(db):
    (p,) = seed(db, [1])
    answer(db, p, 1, NOW - timedelta(days=30))
    assert scheduling.next_problem(db, NOW, UTC, new_per_day=0).id == p.id


def test_only_ok_problems_are_served(db):
    (p,) = seed(db, [1])
    p.status = "needs_review"
    db.commit()
    assert scheduling.next_problem(db, NOW, UTC, new_per_day=5) is None


def test_suggested_rating_follows_verdict():
    assert scheduling.suggested_rating("correct") == 3
    assert scheduling.suggested_rating("partially_correct") == 2
    assert scheduling.suggested_rating("incorrect") == 1
    assert scheduling.suggested_rating(None) is None
