import io
import re
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.models import Attempt, Card, Lens, Problem, ProblemLens
from app.render import render
from app.web.main import create_app

GRADE = {
    "verdict": "partially_correct",
    "parts": [],
    "where_wrong": "Method is right; in (b) you divided by $\\lambda$ instead of $\\mu$.",
    "alternative_method": False,
    "reasoning_shown": True,
    "legibility": "n/a",
    "solution_concern": None,
}


@pytest.fixture
def problem(db):
    db.merge(Lens(id=17, name="Queues: Little's law", question="How much waiting am I creating?", summary=""))
    p = Problem(
        ingest_key="k",
        problem_url="https://example.edu/ps3.pdf",
        solution_url="https://example.edu/ps3sol.pdf",
        source="MIT OCW 1.203J",
        source_label="Problem Set 3, problem 1",
        kind="exercise",
        status="ok",
        statement_original="Customers arrive at rate $\\lambda_1 = 2$ per hour. Find $W_q$.",
        solution="$W_q = \\rho/(\\mu-\\lambda)$",
    )
    p.lenses.append(ProblemLens(lens_id=17, target_key="17:0", problem_ref="PS3 #1", curator_notes="Printed 0.2696 should be 0.0812."))
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def client(settings, Session_, llm):
    return TestClient(create_app(settings, llm, Session_))


def test_full_practice_loop(client, db, problem, transport):
    page = client.get("/practice")
    assert page.url.path == f"/problems/{problem.id}"
    # Nothing on the problem page says which lens or course this is.
    for giveaway in ("Little", "Queues", "1.203J", "Problem Set 3", "0.2696"):
        assert giveaway not in page.text

    transport.queue("grade_problem", GRADE)
    shown_at = re.search(r'name="shown_at" value="([^"]+)"', page.text).group(1)
    r = client.post(f"/problems/{problem.id}/attempt", data={"answer": "W = 1/(mu - lambda)", "confidence": "70", "shown_at": shown_at})
    assert r.url.path.endswith("/grade")
    r = client.post(r.url.path)
    assert "Partially correct" in r.text and "Queues: Little" in r.text and "0.2696" in r.text
    assert 'value="2" class="primary"' in r.text  # suggested rating

    sent = transport.sent[-1]["content"]
    assert any("Printed 0.2696" in b.get("text", "") for b in sent)  # curator notes reach the grader

    attempt = db.scalar(select(Attempt))
    assert attempt.confidence == 70 and attempt.verdict == "partially_correct" and attempt.shown_at

    r = client.post(f"/attempts/{attempt.id}/rate", data={"rating": "2"})
    assert r.url.path == "/practice"  # nothing left: the done page
    assert "All done" in r.text
    db.expire_all()
    assert db.get(Card, problem.id) is not None


def test_unrated_attempt_blocks_the_next_problem(client, db, problem, transport):
    transport.queue("grade_problem", GRADE)
    client.post(f"/problems/{problem.id}/attempt", data={"answer": "x"})
    attempt = db.scalar(select(Attempt))
    assert client.get("/practice").url.path == f"/attempts/{attempt.id}/grade"


def test_grading_failure_is_recoverable(client, db, problem, transport):
    transport.queue("grade_problem", GRADE, stop_reason="refusal")
    r = client.post(f"/problems/{problem.id}/attempt", data={"answer": "x"}, follow_redirects=False)
    r = client.post(r.headers["location"])
    assert "Grading failed" in r.text and "Try again" in r.text
    transport.queue("grade_problem", GRADE)
    attempt = db.scalar(select(Attempt))
    assert "Partially correct" in client.post(f"/attempts/{attempt.id}/grade").text


def test_photo_upload_is_downscaled_and_sent(client, db, problem, transport, settings):
    buf = io.BytesIO()
    Image.new("RGB", (4000, 3000), "white").save(buf, "JPEG")
    transport.queue("grade_problem", GRADE)
    r = client.post(f"/problems/{problem.id}/attempt", files={"photos": ("work.jpg", buf.getvalue(), "image/jpeg")}, data={"answer": ""})
    client.post(r.url.path)
    attempt = db.scalar(select(Attempt))
    (name,) = attempt.photo_paths
    assert max(Image.open(settings.uploads_dir / name).size) == 1600
    assert any(b["type"] == "image" for b in transport.sent[-1]["content"])
    assert client.get(f"/uploads/{name}").status_code == 200
    assert client.get("/uploads/..%2Fapp.db").status_code == 404


def test_empty_answer_is_rejected(client, problem):
    assert client.post(f"/problems/{problem.id}/attempt", data={"answer": "  "}).status_code == 400


def test_problems_not_in_rotation_are_hidden(client, db, problem):
    problem.status = "needs_review"
    db.commit()
    assert client.get(f"/problems/{problem.id}").status_code == 404


def test_review_approve_with_redaction(client, db, problem):
    problem.status = "needs_review"
    problem.flags = [{"code": "names_lens", "detail": "statement contains: little's law"}]
    db.commit()
    assert "names_lens" in client.get("/review").text
    r = client.post(f"/review/{problem.id}", data={"action": "approve", "statement_served": "Redacted statement.", "solution": problem.solution})
    assert r.status_code == 200
    db.expire_all()
    p = db.get(Problem, problem.id)
    assert p.status == "ok" and p.statement == "Redacted statement." and p.statement_original.startswith("Customers")
    assert p.reviewed_at is not None


def test_review_cannot_approve_without_solution(client, db, problem):
    problem.status = "needs_review"
    db.commit()
    r = client.post(f"/review/{problem.id}", data={"action": "approve", "statement_served": problem.statement, "solution": ""})
    assert r.status_code == 400


def test_unchanged_statement_is_not_stored_as_override(client, db, problem):
    client.post(f"/review/{problem.id}", data={"action": "save", "statement_served": problem.statement_original, "solution": problem.solution})
    db.expire_all()
    assert db.get(Problem, problem.id).statement_served is None


def test_desk_toggle(client, db, problem):
    problem.status, problem.kind = "desk", "lab"
    db.commit()
    assert "PS3 #1" in client.get("/desk").text
    client.post(f"/desk/{problem.id}")
    db.expire_all()
    assert db.get(Problem, problem.id).desk_done_at is not None


def test_login_required_when_password_set(settings, Session_, llm):
    client = TestClient(create_app(replace(settings, app_password="hunter2"), llm, Session_))
    assert client.get("/").url.path == "/login"
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    r = client.post("/login", data={"password": "hunter2"})
    assert r.url.path == "/" and "Problem bank" in r.text
    assert client.get("/healthz").status_code == 200


def test_render_keeps_math_and_escapes_html():
    out = str(render("Let $x_1 * y_2$ and **bold** <script>alert(1)</script>\n\n| a | b |\n|---|---|\n| 1 | 2 |"))
    assert "$x_1 * y_2$" in out and "<strong>bold</strong>" in out
    assert "<script>" not in out and "<table>" in out


# --- without an API key ------------------------------------------------------


@pytest.fixture
def nokey(settings, Session_, llm):
    return TestClient(create_app(replace(settings, llm_enabled=False), llm, Session_))


def test_without_key_you_grade_yourself(nokey, db, problem, transport):
    assert "Running without an API key" in nokey.get("/").text
    r = nokey.post(f"/problems/{problem.id}/attempt", data={"answer": "W = 1/(mu - lambda)", "confidence": "70"})
    attempt = db.scalar(select(Attempt))
    assert r.url.path == f"/attempts/{attempt.id}"
    # Solution and your answer come before the verdict buttons; no rating until you've judged it.
    assert r.text.index("Official solution") < r.text.index("Were you right?")
    assert 'name="rating"' not in r.text
    assert nokey.get(f"/attempts/{attempt.id}/grade").url.path == f"/attempts/{attempt.id}"
    assert nokey.get("/practice").url.path == f"/attempts/{attempt.id}"

    r = nokey.post(f"/attempts/{attempt.id}/self-grade", data={"verdict": "partially_correct"})
    assert "(your call)" in r.text and 'value="2" class="primary"' in r.text
    db.expire_all()
    attempt = db.get(Attempt, attempt.id)
    assert (attempt.verdict, attempt.verdict_source) == ("partially_correct", "self")

    nokey.post(f"/attempts/{attempt.id}/rate", data={"rating": "2"})
    db.expire_all()
    assert db.get(Card, problem.id) is not None
    assert transport.sent == []  # never called Claude


def test_self_grade_rejects_unknown_verdicts_and_never_overwrites(nokey, db, problem):
    nokey.post(f"/problems/{problem.id}/attempt", data={"answer": "x"})
    attempt = db.scalar(select(Attempt))
    assert nokey.post(f"/attempts/{attempt.id}/self-grade", data={"verdict": "ungradeable"}).status_code == 400
    nokey.post(f"/attempts/{attempt.id}/self-grade", data={"verdict": "correct"})
    nokey.post(f"/attempts/{attempt.id}/self-grade", data={"verdict": "incorrect"})
    db.expire_all()
    assert db.get(Attempt, attempt.id).verdict == "correct"


def test_review_shows_source_text_for_copying(client, db, problem):
    from app.models import SourceDoc

    doc = SourceDoc(url="https://example.edu/ps3.pdf", kind="pdf", text_layer="Problem 1. Customers arrive at rate two per hour.")
    db.add(doc)
    db.flush()
    problem.status, problem.problem_doc_id, problem.solution_doc_id = "needs_review", doc.id, doc.id
    problem.statement_original, problem.solution = "", ""
    db.commit()
    page = client.get(f"/review/{problem.id}").text
    assert "Customers arrive at rate two per hour." in page
    assert page.count("Source text") == 1  # same document for both: shown once
