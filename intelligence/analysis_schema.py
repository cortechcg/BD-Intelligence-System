"""Pydantic contract for analyzer JSON — fields the pipeline already consumes.

Derived from intelligence/analyzer.ANALYSIS_SCHEMA plus actual readers:
bid_scorer, main, proposal_writer, cv_matcher, budget_calculator, compliance,
tender_reader. No new business fields.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class AnalysisSchemaError(ValueError):
    """JSON parsed as an object but did not match the extraction schema."""


class _ExtractionModel(BaseModel):
    model_config = ConfigDict(extra="allow")


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("boolean/null is not a number")
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            raise ValueError("empty string is not a number")
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    elif isinstance(value, (int, float)):
        number = float(value)
    else:
        raise ValueError(f"not a number: {type(value).__name__}")
    if not math.isfinite(number):
        raise ValueError("non-finite number")
    return number


def _optional_number(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return _finite_number(value)


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    raise ValueError("must be a boolean or null")


def _str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        raise ValueError("expected a list of strings")
    out: list[str] = []
    for item in value:
        if item is None:
            continue
        if isinstance(item, bool) or isinstance(item, (dict, list)):
            raise ValueError("list items must be strings")
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if not math.isfinite(float(item)):
                raise ValueError("list items must be strings")
            text = str(item).strip()
        else:
            text = str(item).strip()
        if text:
            out.append(text)
    return out


def _optional_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bool) or isinstance(value, (dict, list)):
        raise ValueError("must be a string or null")
    text = str(value).strip()
    return text or None


class OpportunityExtraction(_ExtractionModel):
    title: Optional[str] = None
    client: Optional[str] = None
    donor: Optional[str] = None
    reference_number: Optional[str] = None
    submission_deadline: Optional[str] = None
    project_location: list[str] = Field(default_factory=list)
    lots_or_sites: list[str] = Field(default_factory=list)
    target_groups: list[str] = Field(default_factory=list)
    project_duration: Optional[str] = None
    estimated_budget_usd: Optional[float] = None
    currency: Optional[str] = None

    @field_validator(
        "title",
        "client",
        "donor",
        "reference_number",
        "submission_deadline",
        "project_duration",
        "currency",
        mode="before",
    )
    @classmethod
    def _strings(cls, value: Any) -> Optional[str]:
        return _optional_str(value)

    @field_validator(
        "project_location",
        "lots_or_sites",
        "target_groups",
        mode="before",
    )
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _str_list(value)

    @field_validator("estimated_budget_usd", mode="before")
    @classmethod
    def _budget(cls, value: Any) -> Optional[float]:
        return _optional_number(value)


class RequirementsExtraction(_ExtractionModel):
    technical: list[str] = Field(default_factory=list)
    thematic_areas: list[str] = Field(default_factory=list)
    geographic_experience: list[str] = Field(default_factory=list)
    language_requirements: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    methodology_requirements: list[str] = Field(default_factory=list)

    @field_validator(
        "technical",
        "thematic_areas",
        "geographic_experience",
        "language_requirements",
        "certifications",
        "methodology_requirements",
        mode="before",
    )
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _str_list(value)


class TeamRequirementExtraction(_ExtractionModel):
    role: Optional[str] = None
    level: Optional[str] = None
    years_experience_minimum: Optional[float] = None
    required_skills: list[str] = Field(default_factory=list)
    required_geographic_experience: list[str] = Field(default_factory=list)
    required_education: Optional[str] = None
    estimated_days_of_effort: Optional[float] = None
    must_be_local: Optional[bool] = None
    local_country: Optional[str] = None

    @field_validator("role", "level", "required_education", "local_country", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Optional[str]:
        return _optional_str(value)

    @field_validator("required_skills", "required_geographic_experience", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _str_list(value)

    @field_validator("years_experience_minimum", "estimated_days_of_effort", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Optional[float]:
        return _optional_number(value)

    @field_validator("must_be_local", mode="before")
    @classmethod
    def _bool(cls, value: Any) -> Optional[bool]:
        return _optional_bool(value)


class DeliverableExtraction(_ExtractionModel):
    name: Optional[str] = None
    description: Optional[str] = None
    timeline: Optional[str] = None

    @field_validator("name", "description", "timeline", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Optional[str]:
        return _optional_str(value)


class EvaluationCriterionExtraction(_ExtractionModel):
    criterion: Optional[str] = None
    weight_percent: Optional[float] = None
    description: Optional[str] = None

    @field_validator("criterion", "description", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Optional[str]:
        return _optional_str(value)

    @field_validator("weight_percent", mode="before")
    @classmethod
    def _weight(cls, value: Any) -> Optional[float]:
        return _optional_number(value)


class AssignmentFrameworkExtraction(_ExtractionModel):
    criterion: Optional[str] = None
    description: Optional[str] = None

    @field_validator("criterion", "description", mode="before")
    @classmethod
    def _strings(cls, value: Any) -> Optional[str]:
        return _optional_str(value)


class SubmissionRequirementsExtraction(_ExtractionModel):
    technical_proposal_page_limit: Optional[float] = None
    prescribed_proposal_sections: list[str] = Field(default_factory=list)
    cvs_required: Optional[bool] = None
    past_work_samples_required: Optional[float] = None
    references_required: Optional[float] = None
    financial_proposal_required: Optional[bool] = None

    @field_validator("prescribed_proposal_sections", mode="before")
    @classmethod
    def _sections(cls, value: Any) -> list[str]:
        return _str_list(value)

    @field_validator(
        "technical_proposal_page_limit",
        "past_work_samples_required",
        "references_required",
        mode="before",
    )
    @classmethod
    def _numbers(cls, value: Any) -> Optional[float]:
        return _optional_number(value)

    @field_validator("cvs_required", "financial_proposal_required", mode="before")
    @classmethod
    def _bools(cls, value: Any) -> Optional[bool]:
        return _optional_bool(value)


class BidAnalysisExtraction(_ExtractionModel):
    is_consultancy_contract: bool = True
    submission_type: Optional[str] = None
    cortech_fit_score: Optional[float] = None
    win_probability: Optional[float] = None
    bid_recommendation: Optional[str] = None
    effort_required: Optional[str] = None
    key_strengths: list[str] = Field(default_factory=list)
    key_gaps: list[str] = Field(default_factory=list)
    recommended_external_partners: list[str] = Field(default_factory=list)
    rationale: Optional[str] = None
    priority: Optional[str] = None

    @field_validator("is_consultancy_contract", mode="before")
    @classmethod
    def _consultancy_default(cls, value: Any) -> bool:
        # Missing/null → True (CURSOR.md). Garbage types fail, not fail-open.
        if value is None or value == "":
            return True
        if isinstance(value, bool):
            return value
        raise ValueError("is_consultancy_contract must be a boolean")

    @field_validator(
        "submission_type",
        "bid_recommendation",
        "effort_required",
        "rationale",
        "priority",
        mode="before",
    )
    @classmethod
    def _strings(cls, value: Any) -> Optional[str]:
        return _optional_str(value)

    @field_validator("cortech_fit_score", "win_probability", mode="before")
    @classmethod
    def _scores(cls, value: Any) -> Optional[float]:
        return _optional_number(value)

    @field_validator(
        "key_strengths",
        "key_gaps",
        "recommended_external_partners",
        mode="before",
    )
    @classmethod
    def _lists(cls, value: Any) -> list[str]:
        return _str_list(value)


def _object_or_empty(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _list_or_empty(value: Any, name: str) -> list:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    return value


class AnalysisPayload(_ExtractionModel):
    opportunity: OpportunityExtraction = Field(default_factory=OpportunityExtraction)
    requirements: RequirementsExtraction = Field(default_factory=RequirementsExtraction)
    team_requirements: list[TeamRequirementExtraction] = Field(default_factory=list)
    deliverables: list[DeliverableExtraction] = Field(default_factory=list)
    evaluation_criteria: list[EvaluationCriterionExtraction] = Field(default_factory=list)
    assignment_evaluation_framework: list[AssignmentFrameworkExtraction] = Field(
        default_factory=list
    )
    submission_requirements: SubmissionRequirementsExtraction = Field(
        default_factory=SubmissionRequirementsExtraction
    )
    bid_analysis: BidAnalysisExtraction = Field(default_factory=BidAnalysisExtraction)

    @model_validator(mode="before")
    @classmethod
    def _coerce_missing_objects(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("analysis payload must be an object")
        data = dict(value)
        # LLM-supplied provenance is untrusted and often invents pages.
        data.pop("extraction_provenance", None)
        data["opportunity"] = _object_or_empty(data.get("opportunity"), "opportunity")
        data["requirements"] = _object_or_empty(data.get("requirements"), "requirements")
        data["bid_analysis"] = _object_or_empty(data.get("bid_analysis"), "bid_analysis")
        data["submission_requirements"] = _object_or_empty(
            data.get("submission_requirements"), "submission_requirements"
        )
        data["team_requirements"] = _list_or_empty(
            data.get("team_requirements"), "team_requirements"
        )
        data["deliverables"] = _list_or_empty(data.get("deliverables"), "deliverables")
        data["evaluation_criteria"] = _list_or_empty(
            data.get("evaluation_criteria"), "evaluation_criteria"
        )
        data["assignment_evaluation_framework"] = _list_or_empty(
            data.get("assignment_evaluation_framework"),
            "assignment_evaluation_framework",
        )
        return data


def validate_analysis_object(payload: dict) -> dict:
    """Validate a parsed JSON object. Raises AnalysisSchemaError on failure."""
    if not isinstance(payload, dict):
        raise AnalysisSchemaError("analysis payload must be an object")
    try:
        model = AnalysisPayload.model_validate(payload)
    except ValidationError as exc:
        raise AnalysisSchemaError(_format_validation_error(exc)) from exc
    except ValueError as exc:
        raise AnalysisSchemaError(str(exc)) from exc
    return model.model_dump(mode="python")


def _format_validation_error(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(item) for item in error.get("loc") or ())
        msg = error.get("msg") or "invalid"
        parts.append(f"{loc}: {msg}" if loc else msg)
    text = "; ".join(parts) or str(exc)
    return text[:1500]
