"""Committee marks are the traced alignment, not a buyer award."""
from intelligence.committee_grade import reading_from_alignment
from intelligence.requirement_alignment import EVALUATION_ALIGNMENT_BAR


def test_a_missing_criterion_scores_zero_and_names_the_pass_mark():
    reading = reading_from_alignment({
        "criteria_score_percent": 40,
        "rows": [
            {
                "kind": "evaluation_criterion",
                "label": "Methodology",
                "weight_percent": 40,
                "status": "not_addressed",
            },
            {
                "kind": "evaluation_criterion",
                "label": "Team",
                "weight_percent": 60,
                "status": "addressed",
                "evidence": "The team paragraph names the role.",
            },
        ],
    })
    assert reading["bar_percent"] == EVALUATION_ALIGNMENT_BAR
    assert reading["at_bar"] is False
    assert "not a score awarded by the buyer" in reading["warning"]
    missing = reading["rows"][0]
    assert missing["mark"] == 0
    assert missing["weight"] == 40
    assert str(EVALUATION_ALIGNMENT_BAR) in missing["improvement"]
    assert reading["rows"][1]["mark"] == 60
    assert reading["rows"][1]["improvement"] == ""


def test_a_criterion_without_a_weight_is_not_given_a_mark():
    reading = reading_from_alignment({
        "rows": [{
            "kind": "evaluation_criterion",
            "label": "Approach",
            "weight_percent": None,
            "status": "addressed",
        }],
    })
    assert reading["rows"][0]["mark"] is None
    assert "no weight" in reading["rows"][0]["reason"]
