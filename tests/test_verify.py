from app.ingest.verify import COVERAGE_THRESHOLD, check_problem, coverage, lens_term_hits, prose_words

LAYER = "A fair coin is flipped fifty times. Find the probability that the longest run of heads has length at least seven."


def test_verbatim_transcription_has_full_coverage():
    assert coverage("A fair coin is flipped fifty times.", LAYER) == 1.0


def test_math_is_ignored():
    # The text layer mangles math, so math tokens must not count either way.
    assert prose_words("Find $P(X \\geq 7)$ where $X \\sim \\mathrm{Bin}(50, 1/2)$.") == ["find", "where"]


def test_paraphrase_drops_below_threshold():
    paraphrase = "Suppose we toss an unbiased penny repeatedly; estimate the chance of a lengthy streak."
    assert coverage(paraphrase, LAYER) < COVERAGE_THRESHOLD


def test_lens_terms_match_word_starts_only():
    assert lens_term_hits("Use Bayes' rule and the base rate.", ["bayes", "base rate"]) == ["bayes", "base rate"]
    assert lens_term_hits("A confounding factor", ["confound"]) == ["confound"]
    assert lens_term_hits("an unrelated jdag", ["dag"]) == []


def _check(**kw):
    defaults = dict(
        statement="A fair coin is flipped fifty times.",
        solution="Find the probability that the longest run of heads has length at least seven.",
        issues=[],
        document_issues=[],
        problem_text_layer=LAYER,
        solution_text_layer=LAYER,
        has_text_layer=True,
        lens_terms=[],
    )
    return check_problem(**(defaults | kw))


def test_clean_problem_has_no_flags():
    flags, cov = _check()
    assert flags == [] and cov == 1.0


def test_each_failure_mode_flags():
    codes = lambda flags: {f["code"] for f in flags}
    assert codes(_check(solution=None)[0]) == {"unmatched"}
    assert codes(_check(has_text_layer=False)[0]) == {"no_text_layer"}
    assert "illegible" in codes(_check(statement="A fair coin [[ILLEGIBLE]] flipped.")[0])
    assert "figure" in codes(_check(statement="A fair coin. [[FIGURE: a histogram]]")[0])
    assert "names_lens" in codes(_check(statement="A fair coin is flipped fifty times.", lens_terms=["coin"])[0])
    assert codes(_check(issues=[{"code": "external_ref", "detail": "Table 2"}])[0]) == {"external_ref"}
