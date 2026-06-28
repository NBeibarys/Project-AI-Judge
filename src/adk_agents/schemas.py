"""Strict structured-response contracts for evidence extraction and grading."""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

RUBRIC_CRITERIA = (
    "Originality",
    "Approach",
    "Personal connection",
    "Concreteness",
    "Credibility in context",
    "Trajectory",
    "Program fit",
    "Regional relevance",
    "Communication quality",
)


class CriterionEvidence(BaseModel):
    """Grounded evidence and the qualification needed to interpret it."""
    model_config = ConfigDict(extra="forbid")
    evidence: str = Field(min_length=1)
    notes: str = Field(min_length=1)


class AnalystReport(BaseModel):
    """Exactly one evidence record for every approved rubric criterion."""
    model_config = ConfigDict(extra="forbid")
    criteria: dict[str, CriterionEvidence]
    missing_sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_complete_rubric(self) -> "AnalystReport":
        """Fail closed if the model omits, renames, or invents dimensions."""
        supplied = set(self.criteria)
        required = set(RUBRIC_CRITERIA)
        if supplied != required:
            raise ValueError(
                "criteria must match the rubric; "
                f"missing={sorted(required - supplied)}, "
                f"unexpected={sorted(supplied - required)}"
            )
        return self


class GraderVerdict(BaseModel):
    """A rejection carries feedback; an approval carries a complete grade."""
    model_config = ConfigDict(extra="forbid")
    approved: bool
    feedback: str = ""
    score: Optional[float] = Field(default=None, ge=0, le=10)
    reasoning: Optional[str] = None
    confidence: Optional[Literal["low", "medium", "high"]] = None

    @model_validator(mode="after")
    def enforce_decision_contract(self) -> "GraderVerdict":
        """Prevent contradictory model output from reaching Sheets."""
        if self.approved:
            if self.score is None or not (self.reasoning or "").strip():
                raise ValueError("approved verdicts require score and reasoning")
            if self.confidence is None:
                raise ValueError("approved verdicts require confidence")
        else:
            if not self.feedback.strip():
                raise ValueError("rejected verdicts require actionable feedback")
            if any(value is not None for value in (self.score, self.reasoning, self.confidence)):
                raise ValueError("rejected verdicts must not include final-grade fields")
        return self
