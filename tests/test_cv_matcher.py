from intelligence import cv_matcher


def test_score_capability_match_survives_null_metadata():
    result = cv_matcher.score_capability_match(
        {
            "required_geographic_experience": None,
            "required_languages": None,
            "years_experience_minimum": None,
        },
        {"similarity": None, "metadata": None},
    )
    assert result["match_score"] == 0.0
    assert result["confidence"] in {"INFERRED", "VERIFIED"}


def test_match_team_to_requirements_null_lists_and_similarity(monkeypatch):
    monkeypatch.setattr(
        cv_matcher,
        "search_consultants",
        lambda **kwargs: [{
            "consultant_name": "Amina Hassan",
            "airtable_consultant_id": "rec1",
            "role_title": None,
            "similarity": None,
            "metadata": None,
        }],
    )
    monkeypatch.setattr(cv_matcher, "log_agent_action", lambda **kwargs: None)

    result = cv_matcher.match_team_to_requirements([
        {
            "role": "Team Lead",
            "required_skills": None,
            "required_geographic_experience": None,
            "years_experience_minimum": None,
            "required_education": None,
        }
    ])
    match = result["matched_team"]["Team Lead"]
    assert match["consultant_name"] == "Amina Hassan"
    assert match["similarity_score"] == 0


def test_filter_by_availability_null_percentage(monkeypatch):
    monkeypatch.setattr(
        cv_matcher,
        "get_all_consultants",
        lambda: [{"id": "rec1", "availability_percentage": None}],
    )
    ranked = cv_matcher.filter_by_availability([
        {"airtable_consultant_id": "rec1", "consultant_name": "Amina"}
    ])
    assert ranked[0]["availability_percent"] == 100
    assert ranked[0]["availability_flag"] == "Available"
