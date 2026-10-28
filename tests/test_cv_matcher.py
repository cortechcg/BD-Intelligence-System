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


def test_embedding_error_is_not_treated_as_external_recruitment(monkeypatch):
    from database.supabase_client import EmbeddingError

    monkeypatch.setattr(
        cv_matcher,
        "search_consultants",
        lambda **kwargs: (_ for _ in ()).throw(EmbeddingError("invalid key", auth=True)),
    )
    monkeypatch.setattr(cv_matcher, "log_agent_action", lambda **kwargs: None)
    monkeypatch.setattr(cv_matcher, "get_all_consultants", lambda: [])

    result = cv_matcher.match_team_to_requirements([
        {"role": "Lead Consultant/Researcher"},
        {"role": "Analyst"},
    ])
    assert result["search_unavailable"] is True
    assert result["coverage_percent"] is None
    assert result["gaps"] == []
    assert result["matched_team"]["Lead Consultant/Researcher"]["consultant_name"] == (
        "CV SEARCH UNAVAILABLE"
    )
    assert result["capability_summary"]["status"] == "UNKNOWN"


_PEOPLE = [
    {"id": "c1", "full_name": "Amina Evaluator", "role_title": "Monitoring and Evaluation Specialist",
     "key_skills": "evaluation design, KoboToolbox, surveys", "thematic_expertise": ["WASH", "evaluation"],
     "geographic_experience": ["Somalia"], "languages": ["English", "Somali"], "cv_text": "Led endline evaluations in Somalia."},
    {"id": "c2", "full_name": "Omar Engineer", "role_title": "Water Engineer",
     "key_skills": "borehole design, water infrastructure", "thematic_expertise": ["WASH"],
     "geographic_experience": ["Kenya"], "languages": ["English"], "cv_text": "Water supply engineering."},
]


def test_embedding_failure_falls_back_to_flagged_lexical_match(monkeypatch):
    """Embeddings down must not leave a slot empty when CV text names the skills."""
    from database.supabase_client import EmbeddingError

    monkeypatch.setattr(cv_matcher, "search_consultants",
                        lambda **kw: (_ for _ in ()).throw(EmbeddingError("HTTP 401", auth=True)))
    monkeypatch.setattr(cv_matcher, "log_agent_action", lambda **kw: None)
    monkeypatch.setattr(cv_matcher, "get_all_consultants", lambda: _PEOPLE)
    result = cv_matcher.match_team_to_requirements([
        {"role": "Monitoring and Evaluation Specialist", "required_skills": ["evaluation", "KoboToolbox"],
         "required_geographic_experience": ["Somalia"]},
        {"role": "Water Engineer", "required_skills": ["water infrastructure", "borehole"]},
        {"role": "Astrophysicist", "required_skills": ["telescope"]},
    ])
    team = result["matched_team"]
    assert team["Monitoring and Evaluation Specialist"]["consultant_name"] == "Amina Evaluator"
    assert team["Water Engineer"]["consultant_name"] == "Omar Engineer"
    for role in ("Monitoring and Evaluation Specialist", "Water Engineer"):
        slot = team[role]
        assert slot["match_method"] == "lexical_fallback"
        assert slot["capability"]["confidence"] == "LOW"
        assert "LOWER CONFIDENCE" in slot["capability"]["why"]
    # No CV evidence at all: stays honestly unavailable, not invented.
    assert team["Astrophysicist"]["consultant_name"] == "CV SEARCH UNAVAILABLE"
    assert result["lexical_fallback"] is True and result["match_confidence"] == "LOW"
    assert result["search_unavailable"] is True


def test_lexical_fallback_counts_at_half_credit_and_inferred_in_the_score():
    from intelligence.bid_scorer import _team_capacity_factor

    f = _team_capacity_factor({"coverage_percent": 100, "gaps": [], "lexical_fallback": True})
    assert f["status"] == "INFERRED" and f["value"] == 50.0
