"""Read original substitutes from the local substitute_exercises.html bundle.

Deterministic: the curriculum points at element ids (#L01-S1-problem), so no LLM is involved.
"""

from pathlib import Path

from bs4 import BeautifulSoup


def _element_text(originals_dir: Path, ref: str) -> str:
    filename, _, anchor = ref.partition("#")
    path = originals_dir / filename
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    el = soup.find(id=anchor)
    if el is None:
        raise LookupError(f"no element with id {anchor!r} in {path}")
    return el.get_text("\n", strip=True)


def load_original(originals_dir: Path, problem_ref: str, solution_ref: str) -> tuple[str, str]:
    return _element_text(originals_dir, problem_ref), _element_text(originals_dir, solution_ref)
