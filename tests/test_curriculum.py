from collections import Counter

from app.config import ROOT
from app.ingest.curriculum import group_by_document, infer_kind, load_curriculum


def test_real_curriculum_shape():
    lenses, targets = load_curriculum(ROOT / "content" / "curriculum.json")
    assert len(lenses) == 20
    assert len(targets) == 91
    assert Counter(t.kind for t in targets) == {"exercise": 60, "original": 20, "lab": 9, "worked_example": 2}
    assert len(group_by_document(targets)) == 24
    assert all(t.is_relative for t in targets if t.kind == "original")
    assert len({t.key for t in targets}) == 91


def test_cross_listed_question_lands_in_one_group():
    _, targets = load_curriculum(ROOT / "content" / "curriculum.json")
    groups = group_by_document(targets)
    yale = next(g for (p, _), g in groups.items() if p.endswith("final_exam_2007_solutions_0.pdf"))
    assert {t.lens_id for t in yale} == {9, 12, 13, 14}
    assert sum("Poverty Traps" in t.problem_ref for t in yale) == 2


def test_explicit_kind_wins_over_markers():
    assert infer_kind({"kind": "exercise", "notes": "WORKED-LAB SUBSTITUTE"}) == ("exercise", False)
    assert infer_kind({"notes": "WORKED-LAB SUBSTITUTE. Recreate it."}) == ("lab", True)
    assert infer_kind({"source": "Original substitute; not external"}) == ("original", True)
