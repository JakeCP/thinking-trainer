import pytest

from app.config import ROOT
from app.llm import LlmError, load_prompt, strict_schema, text_block
from app.models import LlmCall
from app.schemas import Extraction, Grade

GOOD_GRADE = {
    "verdict": "incorrect",
    "parts": [],
    "where_wrong": "You treated the draws as independent.",
    "alternative_method": False,
    "reasoning_shown": True,
    "legibility": "n/a",
    "solution_concern": None,
}


@pytest.mark.parametrize("name", ["grade_problem", "extract_problems", "generate_situations"])
def test_prompt_files_parse(name):
    p = load_prompt(ROOT / "prompts", name)
    assert p.model.startswith("claude-") and p.max_tokens > 0 and len(p.system) > 200


def test_strict_schema_closes_every_object_and_inlines_refs():
    schema = strict_schema(Extraction)
    assert "$defs" not in str(schema) and "$ref" not in str(schema)

    def objects(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for v in node.values():
                yield from objects(v)
        elif isinstance(node, list):
            for v in node:
                yield from objects(v)

    found = list(objects(schema))
    assert len(found) >= 4
    for obj in found:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == set(obj["properties"])


def test_call_validates_and_logs(db, llm, transport):
    transport.queue("grade_problem", GOOD_GRADE)
    grade, call = llm.call(db, prompt_name="grade_problem", purpose="grade", content=[text_block("answer", "42")], output=Grade)
    assert grade.verdict == "incorrect"
    row = db.get(LlmCall, call.id)
    assert row.error is None and row.prompt_sha and "<answer>" in row.request_text and row.input_tokens == 100


@pytest.mark.parametrize(
    "payload,stop,message",
    [
        (GOOD_GRADE, "refusal", "declined"),
        (GOOD_GRADE, "max_tokens", "max_tokens"),
        ({"verdict": "maybe"}, "end_turn", "ValidationError"),
        ("not json", "end_turn", "JSONDecodeError"),
    ],
)
def test_failures_raise_with_logged_call(db, llm, transport, payload, stop, message):
    transport.queue("grade_problem", payload, stop)
    with pytest.raises(LlmError) as exc:
        llm.call(db, prompt_name="grade_problem", purpose="grade", content=[], output=Grade)
    assert message in str(exc.value)
    assert db.get(LlmCall, exc.value.call_id).error
