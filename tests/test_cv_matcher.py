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
    assert result["factors"]["sector"] is None
    assert result["factors"]["availability"] is None


def test_sector_match_and_missing_thematic_not_inferred():
    matched = cv_matcher.score_capability_match(
        {"required_thematic_areas": ["evaluation", "MEL"]},
        {
            "similarity": 0.8,
            "metadata": {"thematic_expertise": ["MEL", "evaluation", "WASH"]},
        },
    )
    assert matched["factors"]["sector"] == 100.0
    assert matched["status"] == "SATISFIED"

    missing_meta = cv_matcher.score_capability_match(
        {"required_thematic_areas": ["renewable energy"]},
        {"similarity": 0.9, "metadata": {"geographic_experience": ["Kenya"]}},
    )
    assert missing_meta["factors"]["sector"] is None
    assert any("thematic_expertise" in u for u in missing_meta["unknown"])
    assert missing_meta["status"] == "PARTIAL"

    gap = cv_matcher.score_capability_match(
        {"sector": ["renewable energy"]},
        {"similarity": 0.9, "metadata": {"thematic_expertise": ["WASH"]}},
    )
    assert gap["factors"]["sector"] == 0.0
    assert "sector:renewable energy" in gap["gaps"]


def test_explicit_skills_do_not_infer_missing_tools():
    result = cv_matcher.score_capability_match(
        {"required_skills": ["NVivo", "SPSS"]},
        {"similarity": 0.7, "metadata": {"key_skills": "SPSS, KoboToolbox"}},
    )
    assert result["factors"]["skills"] == 50.0
    assert "skill:NVivo" in result["gaps"]

    unknown = cv_matcher.score_capability_match(
        {"required_skills": ["NVivo"]},
        {"similarity": 0.7, "metadata": {}},
    )
    assert unknown["factors"]["skills"] is None
    assert any("key_skills" in u for u in unknown["unknown"])


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
    monkeypatch.setattr(cv_matcher, "get_all_consultants", lambda: [])

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
    assert match["availability_flag"] == "Unknown"
    assert match["capability"]["factors"]["availability"] is None
    assert result["capability_summary"]["match_score"] is not None


def test_opportunity_context_overlays_sector_language_geography(monkeypatch):
    captured = {}

    def fake_search(**kwargs):
        captured["query"] = kwargs["query_text"]
        return [{
            "consultant_name": "Amina Hassan",
            "airtable_consultant_id": "rec1",
            "similarity": 0.8,
            "metadata": {
                "thematic_expertise": ["evaluation", "MEL"],
                "languages": ["English", "Somali"],
                "geographic_experience": ["Somalia"],
            },
        }]

    monkeypatch.setattr(cv_matcher, "search_consultants", fake_search)
    monkeypatch.setattr(cv_matcher, "log_agent_action", lambda **kwargs: None)
    monkeypatch.setattr(
        cv_matcher,
        "get_all_consultants",
        lambda: [{"id": "rec1", "availability_status": "Available"}],
    )

    result = cv_matcher.match_team_to_requirements(
        [{"role": "Evaluator"}],
        opportunity_context={
            "thematic_areas": ["evaluation"],
            "language_requirements": ["Somali"],
            "geographic_experience": ["Somalia"],
        },
    )
    cap = result["matched_team"]["Evaluator"]["capability"]
    assert cap["factors"]["sector"] == 100.0
    assert cap["factors"]["language"] == 100.0
    assert cap["factors"]["geography"] == 100.0
    assert cap["factors"]["availability"] == 100.0
    assert cap["match_score"] >= 80
    assert "evaluation" in captured["query"].lower()
    assert result["capability_summary"]["status"] == "SATISFIED"


def test_missing_availability_is_unknown_not_assumed_free(monkeypatch):
    monkeypatch.setattr(
        cv_matcher,
        "get_all_consultants",
        lambda: [{"id": "rec1"}],
    )
    ranked = cv_matcher.filter_by_availability([
        {"airtable_consultant_id": "rec1", "consultant_name": "Amina"}
    ])
    assert ranked[0]["availability_flag"] == "Unknown"
    assert ranked[0]["availability_percent"] is None
    assert "not inferred" in ranked[0]["availability_evidence"]


def test_busy_consultant_is_kept_not_dropped(monkeypatch):
    monkeypatch.setattr(
        cv_matcher,
        "get_all_consultants",
        lambda: [{"id": "rec1", "availability_status": "Busy"}],
    )
    ranked = cv_matcher.filter_by_availability([
        {
            "airtable_consultant_id": "rec1",
            "consultant_name": "Amina",
            "requirement": {"role": "Evaluator"},
            "similarity": 0.7,
            "metadata": {},
        }
    ])
    assert len(ranked) == 1
    assert ranked[0]["availability_flag"] == "Busy"
    assert ranked[0]["capability"]["factors"]["availability"] == 20.0
    assert "availability:Busy" in ranked[0]["capability"]["gaps"]


def test_unrecognized_availability_status_is_unknown(monkeypatch):
    monkeypatch.setattr(
        cv_matcher,
        "get_all_consultants",
        lambda: [{"id": "rec1", "availability_status": "Sabbatical"}],
    )
    ranked = cv_matcher.filter_by_availability([
        {"airtable_consultant_id": "rec1", "consultant_name": "Amina"}
    ])
    assert ranked[0]["availability_flag"] == "Unknown"
    assert ranked[0]["availability_percent"] is None
