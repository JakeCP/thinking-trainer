"""Mechanical checks on an extraction. No LLM here: these are what keep the LLM honest."""

import re

COVERAGE_THRESHOLD = 0.92
_MATH = re.compile(r"\$\$.*?\$\$|\$[^$]*\$", re.S)
_LATEX_CMD = re.compile(r"\\[A-Za-z]+")
_MARKER = re.compile(r"\[\[[^\]]*\]\]")
_WORD = re.compile(r"[A-Za-z]{3,}")


def prose_words(text: str) -> list[str]:
    """Alphabetic words outside math, LaTeX commands and [[markers]], lowercased."""
    text = _MARKER.sub(" ", text)
    text = _MATH.sub(" ", text)
    text = _LATEX_CMD.sub(" ", text)
    return [w.lower() for w in _WORD.findall(text)]


def coverage(transcription: str, text_layer: str) -> float | None:
    """Share of the transcription's prose words that also occur in the source's text layer.

    Catches paraphrase and invented text. Doesn't catch omissions: a sentence the
    extractor dropped doesn't lower the score.
    """
    words = prose_words(transcription)
    if not words:
        return None
    vocabulary = {w.lower() for w in _WORD.findall(text_layer)}
    return sum(w in vocabulary for w in words) / len(words)


def lens_term_hits(text: str, terms: list[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in terms if re.search(r"\b" + re.escape(t.lower()), lowered)]


def flag(code: str, detail: str) -> dict:
    return {"code": code, "detail": detail}


def check_problem(
    *,
    statement: str,
    solution: str | None,
    issues: list[dict],
    document_issues: list[dict],
    problem_text_layer: str,
    solution_text_layer: str,
    has_text_layer: bool,
    lens_terms: list[str],
) -> tuple[list[dict], float | None]:
    """Return (flags, coverage). Any flag keeps the problem out of rotation until reviewed."""
    flags = [flag(i["code"], i["detail"]) for i in issues + document_issues]

    if not has_text_layer:
        flags.append(flag("no_text_layer", "no usable text layer (scanned?), so the transcription can't be checked"))
        cov = None
    else:
        parts = [coverage(statement, problem_text_layer)]
        if solution:
            parts.append(coverage(solution, solution_text_layer))
        scores = [p for p in parts if p is not None]
        cov = min(scores) if scores else None
        if cov is not None and cov < COVERAGE_THRESHOLD:
            flags.append(flag("low_coverage", f"only {cov:.0%} of the transcribed words appear in the source; possible paraphrase or invented text"))

    if not statement.strip():
        flags.append(flag("empty_statement", "no statement was extracted"))
    if not solution or not solution.strip():
        flags.append(flag("unmatched", "no solution was extracted for this problem"))

    both = statement + "\n" + (solution or "")
    if "[[ILLEGIBLE" in both:
        flags.append(flag("illegible", "the transcription contains [[ILLEGIBLE]]"))
    if "[[FIGURE" in statement:
        flags.append(flag("figure", "the statement refers to a figure you won't see"))
    if hits := lens_term_hits(statement, lens_terms):
        flags.append(flag("names_lens", "statement contains: " + ", ".join(hits)))

    seen, unique = set(), []
    for f in flags:
        if (f["code"], f["detail"]) not in seen:
            seen.add((f["code"], f["detail"]))
            unique.append(f)
    return unique, cov
