import pytest

from app.modules.ai.interpretation import Interpretation
from tests.evaluations.chat_language import check, main
from tests.fixtures.chat_language import CASES


@pytest.mark.parametrize(
    "case_id,fields,failures",
    [
        ("change_purpose", {"purposes": [{"purpose": "cafe"}]}, []),
        ("change_purpose", {}, ["missing_purpose_change"]),
        ("destination", {"purposes": [{"purpose": "cafe", "keyword": "성수동"}]}, []),
        (
            "keep",
            {"origin_name": "성수동", "headcount": 2},
            ["unrequested_condition_changes", "invented_origin"],
        ),
    ],
)
def test_evaluator_checks_effects_and_accepts_equivalent_null_omission(case_id, fields, failures):
    case = next(case for case in CASES if case["id"] == case_id)
    assert check(case, Interpretation(intent="recommendation", **fields)) == failures


def test_default_evaluation_does_not_construct_a_provider(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("Model calls require explicit --live")

    monkeypatch.setattr("sys.argv", ["chat_language"])
    monkeypatch.setattr("tests.evaluations.chat_language.open_ai_provider", unexpected)
    assert main() == 0
    assert '"live": false' in capsys.readouterr().out
