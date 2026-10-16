import json

import pytest
from sqlalchemy import select

from app.ingest.pipeline import run
from app.models import Lens, Problem, SourceDoc
from tests.conftest import make_pdf

PSET = "https://example.edu/pset3_sol.pdf"
P1 = "Problem 1. A fair coin is flipped fifty times. Find the probability that the longest run of heads has length at least seven."
S1 = "Solution 1. Simulate many sequences of fifty flips and count how often the longest run reaches seven."
P2 = "Problem 2. A sample of four hundred voters is polled. How likely is the poll to land within two points of the truth?"
S2 = "Solution 2. The standard error of the proportion is about two and a half points, so roughly fifty eight percent."


def lens(id_, problems):
    return {"id": id_, "name": f"Lens {id_}", "question": "Q?", "summary": "S.", "prerequisites": [], "resources": [], "problems": problems, "gaps": ""}


def entry(ref, url=PSET, sol=None, **kw):
    return {"source": "Test course", "problem_ref": ref, "problem_url": url, "solution_url": sol or url, "situation_based": True, "notes": kw.pop("notes", ""), "verified": True, **kw}


@pytest.fixture
def curriculum(tmp_path):
    path = tmp_path / "curriculum.json"
    data = [
        lens(2, [entry("Problem 1"), entry("Problem 2", notes="Use part (a) only.")]),
        lens(7, [entry("Chapter 5 lab", url="https://example.edu/lab.html", notes="WORKED-LAB SUBSTITUTE. Recreate it.")]),
        lens(20, [{**entry("L20-S1: drift", url="substitute_exercises.html#L20-S1-problem", sol="substitute_exercises.html#L20-S1-solution"), "source": "Original substitute; not external"}]),
    ]
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def served_pdf(tmp_path, mock_http):
    data = make_pdf(tmp_path / "pset.pdf", [P1, S1, P2, S2])
    mock_http.routes[PSET] = (200, data, "application/pdf")
    return data


def extraction(*problems, missing=(), doc_issues=()):
    return {"problems": list(problems), "missing_targets": list(missing), "document_issues": list(doc_issues)}


def problem(target, label, statement, solution, issues=()):
    return {"target_ids": [target], "label": label, "parts": "", "statement": statement, "solution": solution, "issues": list(issues)}


def ingest(db, llm, settings, curriculum, mock_http, **kw):
    return run(db, llm, settings, curriculum, http_client=mock_http, **kw)


def by_status(db):
    out = {}
    for p in db.scalars(select(Problem)):
        out.setdefault(p.status, []).append(p)
    return out


def test_clean_extraction_goes_straight_into_rotation(db, llm, transport, settings, curriculum, mock_http, served_pdf):
    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", P1, S1), problem("2:1", "Problem 2", P2, S2)))
    report = ingest(db, llm, settings, curriculum, mock_http)

    groups = by_status(db)
    assert {p.source_label for p in groups["ok"]} == {"Problem 1", "Problem 2"}
    assert all(p.coverage == 1.0 for p in groups["ok"])
    assert [p.kind for p in groups["desk"]] == ["lab"]
    assert [p.kind for p in groups["excluded"]] == ["original"]
    assert report.pairs_extracted == 1 and report.inferred_kinds == {"lab": 1, "original": 1}
    assert db.get(Lens, 2).tell_words  # from content/lens_terms.yaml

    # The extractor saw the PDF itself, both targets, and the curator's note.
    content = transport.sent[0]["content"]
    assert content[0]["type"] == "document" and content[0]["source"]["media_type"] == "application/pdf"
    assert "Use part (a) only." in content[-1]["text"] and '"2:1"' in content[-1]["text"]


def test_paraphrase_and_missing_targets_go_to_review(db, llm, transport, settings, curriculum, mock_http, served_pdf):
    paraphrase = "Problem 1. Toss an unbiased penny repeatedly and estimate the chance of a lengthy streak occurring."
    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", paraphrase, S1), missing=[{"target_id": "2:1", "reason": "not in this PDF"}]))
    ingest(db, llm, settings, curriculum, mock_http)

    review = {p.source_label: p for p in by_status(db)["needs_review"]}
    assert {f["code"] for f in review["Problem 1"].flags} == {"low_coverage"}
    missing = review["Problem 2"]
    assert missing.flags == [{"code": "missing_target", "detail": "not in this PDF"}]
    assert missing.statement_original == ""


def test_fetch_failure_becomes_placeholders(db, llm, transport, settings, curriculum, mock_http):
    report = ingest(db, llm, settings, curriculum, mock_http)  # PSET not routed: 404
    review = by_status(db)["needs_review"]
    assert len(review) == 2 and all(p.flags[0]["code"] == "fetch_failed" for p in review)
    assert report.pairs_failed == 1 and transport.sent == []


def test_rerun_skips_extracted_pairs_and_retries_failed_ones(db, llm, transport, settings, curriculum, mock_http, tmp_path):
    ingest(db, llm, settings, curriculum, mock_http)  # fails: 404
    mock_http.routes[PSET] = (200, make_pdf(tmp_path / "p.pdf", [P1, S1, P2, S2]), "application/pdf")
    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", P1, S1), problem("2:1", "Problem 2", P2, S2)))
    ingest(db, llm, settings, curriculum, mock_http)

    groups = by_status(db)
    assert len(groups["ok"]) == 2 and "needs_review" not in groups  # placeholders replaced, not duplicated
    report = ingest(db, llm, settings, curriculum, mock_http)
    assert report.pairs_skipped == 1 and len(transport.sent) == 1


def test_reextract_keeps_reviewed_problems(db, llm, transport, settings, curriculum, mock_http, served_pdf):
    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", P1, S1, issues=[{"code": "figure", "detail": "fig 1"}]), problem("2:1", "Problem 2", P2, S2)))
    ingest(db, llm, settings, curriculum, mock_http)
    p1 = db.scalar(select(Problem).where(Problem.source_label == "Problem 1"))
    p1.statement_served, p1.status = "My redacted version.", "ok"
    from datetime import datetime, timezone

    p1.reviewed_at = datetime.now(timezone.utc)
    db.commit()

    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", "changed", S1), problem("2:1", "Problem 2", P2, S2)))
    ingest(db, llm, settings, curriculum, mock_http, re_extract=True)
    db.refresh(p1)
    assert p1.statement == "My redacted version." and p1.statement_original == P1


def test_cross_listed_problem_gets_both_lenses(db, llm, transport, settings, tmp_path, mock_http, served_pdf):
    path = tmp_path / "c.json"
    path.write_text(json.dumps([lens(12, [entry("Q3(a-c)")]), lens(14, [entry("Q3")])]))
    both = {"target_ids": ["12:0", "14:0"], "label": "Question 3", "parts": "a-e", "statement": P1, "solution": S1, "issues": []}
    transport.queue("extract_problems", extraction(both))
    run(db, llm, settings, path, http_client=mock_http)
    (p,) = db.scalars(select(Problem))
    assert p.lens_ids == {12, 14}


def test_originals_load_from_bundle_and_wait_for_review(db, llm, transport, settings, curriculum, mock_http, served_pdf, tmp_path):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "substitute_exercises.html").write_text(
        '<div id="L20-S1-problem">An allele at 50% in a population of 20.</div><div id="L20-S1-solution">It drifts.</div>'
    )
    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", P1, S1), problem("2:1", "Problem 2", P2, S2)))
    ingest(db, llm, settings, curriculum, mock_http, originals_dir=bundle)
    (orig,) = [p for p in by_status(db)["needs_review"] if p.kind == "original"]
    assert orig.statement_original.startswith("An allele") and orig.solution == "It drifts."
    assert orig.flags[0]["code"] == "original"


def test_html_saying_pdf_is_a_fetch_failure(db, llm, transport, settings, curriculum, mock_http):
    mock_http.routes[PSET] = (200, b"<html><body>Please log in</body></html>", "text/html")
    ingest(db, llm, settings, curriculum, mock_http)
    doc = db.scalar(select(SourceDoc).where(SourceDoc.url == PSET))
    assert doc.error == "expected a PDF, got HTML"
    assert all(p.flags[0]["code"] == "fetch_failed" for p in by_status(db)["needs_review"])


# --- without an API key ------------------------------------------------------


def test_without_key_fetches_only_and_waits_for_you(db, llm, transport, settings, curriculum, mock_http, served_pdf):
    report = ingest(db, llm, settings, curriculum, mock_http, extract_enabled=False)
    review = [p for p in by_status(db)["needs_review"] if p.kind == "exercise"]
    assert len(review) == 2 and report.pairs_fetched_only == 1 and transport.sent == []
    for p in review:
        assert p.flags[0]["code"] == "not_extracted"
        assert "longest run of heads" in p.problem_doc.text_layer  # there to copy from at /review


def test_hand_entered_problems_survive_a_later_extraction(db, llm, transport, settings, curriculum, mock_http, served_pdf):
    from datetime import datetime, timezone

    ingest(db, llm, settings, curriculum, mock_http, extract_enabled=False)
    mine = db.scalar(select(Problem).where(Problem.ingest_key == "target:2:0"))
    mine.statement_served, mine.solution, mine.status = "My copy of problem 1.", "My copy of solution 1.", "ok"
    mine.reviewed_at = datetime.now(timezone.utc)
    half_typed = db.scalar(select(Problem).where(Problem.ingest_key == "target:2:1"))
    half_typed.statement_served = "Started typing problem 2"
    db.commit()

    # Later, with a key: only the entry you haven't approved is sent for extraction.
    transport.queue("extract_problems", extraction(problem("2:1", "Problem 2", P2, S2)))
    ingest(db, llm, settings, curriculum, mock_http)
    targets_sent = transport.sent[0]["content"][-1]["text"]
    assert '"2:1"' in targets_sent and '"2:0"' not in targets_sent

    db.expire_all()
    assert db.get(Problem, mine.id).statement == "My copy of problem 1."
    assert db.get(Problem, mine.id).status == "ok"
    kept = db.get(Problem, half_typed.id)  # not deleted: it has your typing in it
    assert kept is not None and kept.flags[-1]["code"] == "superseded"
    assert db.scalar(select(Problem).where(Problem.source_label == "Problem 2", Problem.ingest_key.startswith("pair:"))).status == "ok"


def test_untouched_placeholders_are_replaced_by_extraction(db, llm, transport, settings, curriculum, mock_http, served_pdf):
    ingest(db, llm, settings, curriculum, mock_http, extract_enabled=False)
    transport.queue("extract_problems", extraction(problem("2:0", "Problem 1", P1, S1), problem("2:1", "Problem 2", P2, S2)))
    ingest(db, llm, settings, curriculum, mock_http)
    exercises = [p for p in db.scalars(select(Problem)) if p.kind == "exercise"]
    assert sorted(p.ingest_key.split("|")[-1] for p in exercises) == ["problem 1", "problem 2"]
