"""Strict structured-response contracts for evidence extraction and grading.

Fellowship and R2B use different grading contracts because their roles are
structured differently:

  Fellowship (original, unchanged):
    - analyst  -> AnalystReport
    - grader   -> GraderVerdict (verifies AND scores in one step)
    Single output_schema per role; the active-criteria slot is the fellowship
    default and never switches.

  R2B (3 distinct roles per spec):
    - analyst  -> AnalystReport      (extract evidence only, no score)
    - grader   -> R2BGraderVerdict   (verify only: approve/reject + feedback)
    - head     -> R2BHeadScore       (score only, after approval)
    The grader never scores; the head never verifies. This separation is what
    makes multi-sample averaging of the Head meaningful (research gap #5):
    only the Head is re-run, on the SAME approved evidence.

To support both programs without touching the fellowship grading path, the
AnalystReport validator reads the active-criteria set from a module-level
slot that ``set_active_criteria`` updates. Fellowship callers never call that
setter, so the default behavior is unchanged.
"""
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Fellowship rubric (unchanged — the default active set)
# ---------------------------------------------------------------------------

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

RUBRIC_WEIGHTS = {criterion: round(1.0 / len(RUBRIC_CRITERIA), 4) for criterion in RUBRIC_CRITERIA}

# ---------------------------------------------------------------------------
# R2B rubric (6 criteria, 3-band: 1-3 / 4-6 / 7-10, equal weights)
# ---------------------------------------------------------------------------

RUBRIC_CRITERIA_R2B = (
    "Problem & Solution",
    "Market Potential",
    "Product/MVP & Innovation",
    "Team Strength",
    "Business Model",
    "Presentation & Clarity",
)

RUBRIC_WEIGHTS_R2B = {criterion: round(1.0 / 6.0, 4) for criterion in RUBRIC_CRITERIA_R2B}

# ---------------------------------------------------------------------------
# Active criteria slot — the AnalystReport validator reads this indirection
# so a program switch (fellowship <-> R2B) changes which keys the contract
# enforces, without subclassing the model. Default = fellowship (unchanged).
# ---------------------------------------------------------------------------

_active_criteria: tuple[str, ...] = RUBRIC_CRITERIA


def set_active_criteria(criteria: tuple[str, ...]) -> None:
    """Switch the enforced criteria set. Called by the program config when
    building an agent for a non-default program (e.g. R2B). Fellowship callers
    never call this, so the module-default fellowship set stays active."""
    global _active_criteria
    _active_criteria = tuple(criteria)


def get_active_criteria() -> tuple[str, ...]:
    return _active_criteria


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
        required = set(get_active_criteria())
        if supplied != required:
            raise ValueError(
                "criteria must match the rubric; "
                f"missing={sorted(required - supplied)}, "
                f"unexpected={sorted(supplied - required)}"
            )
        return self


# ---------------------------------------------------------------------------
# Fellowship grader contract (original, unchanged): verify + score in one.
# ---------------------------------------------------------------------------

class GraderVerdict(BaseModel):
    """A rejection carries feedback; an approval carries a complete grade.

    This is the fellowship contract: the grader both verifies and scores in a
    single step. R2B does NOT use this — R2B splits verify and score across
    two agents (R2BGraderVerdict + R2BHeadScore).
    """
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
            if any(
                value is not None
                for value in (self.score, self.reasoning, self.confidence)
            ):
                raise ValueError(
                    "rejected verdicts must not include score, reasoning, or confidence"
                )
        return self


# ---------------------------------------------------------------------------
# R2B contracts: verify and score split across two agents.
# ---------------------------------------------------------------------------

class R2BGraderVerdict(BaseModel):
    """The R2B grader VERIFIES ONLY — it does not score.

    approve=true means the analyst evidence is grounded, real, and covers all
    6 criteria (with video evidence for criterion 6). approve=false carries
    actionable feedback for the analyst to revise.
    """
    model_config = ConfigDict(extra="forbid")
    approved: bool
    feedback: str = ""

    @model_validator(mode="after")
    def enforce_decision_contract(self) -> "R2BGraderVerdict":
        if not self.approved and not self.feedback.strip():
            raise ValueError("rejected verdicts require actionable feedback")
        if self.approved and self.feedback.strip():
            # Approved with feedback is allowed (minor notes), but the grader
            # must not include scores — scoring is the Head's job.
            pass
        return self


class R2BHeadScore(BaseModel):
    """The R2B head SCORES ONLY — it does not verify.

    Scores approved evidence 1-10 per criterion using the band-then-integer
    method. Writes rationale BEFORE score (rationale-before-score CoT). Final
    score = average of 6 criterion scores. A minor ±1 adjustment is allowed
    with reasoning. Confidence reflects evidence quality, not video length.
    """
    model_config = ConfigDict(extra="forbid")
    criterion_scores: dict[str, int]
    criterion_rationale: dict[str, str]
    final_score: float = Field(ge=1, le=10)
    override: float = Field(default=0.0, ge=-1.0, le=1.0)
    override_reasoning: str = ""
    confidence: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def enforce_score_contract(self) -> "R2BHeadScore":
        active = set(RUBRIC_CRITERIA_R2B)
        supplied_scores = set(self.criterion_scores)
        if supplied_scores != active:
            raise ValueError(
                "criterion_scores must match the R2B rubric; "
                f"missing={sorted(active - supplied_scores)}, "
                f"unexpected={sorted(supplied_scores - active)}"
            )
        for name, value in self.criterion_scores.items():
            if not isinstance(value, int) or value < 1 or value > 10:
                raise ValueError(
                    f"criterion_scores['{name}'] must be an integer 1-10"
                )
        supplied_rationale = set(self.criterion_rationale)
        if supplied_rationale != active:
            raise ValueError(
                "criterion_rationale must match the R2B rubric; "
                f"missing={sorted(active - supplied_rationale)}, "
                f"unexpected={sorted(supplied_rationale - active)}"
            )
        for name, text in self.criterion_rationale.items():
            if not text.strip():
                raise ValueError(
                    f"criterion_rationale['{name}'] must be non-empty"
                )
        if self.override != 0.0 and not self.override_reasoning.strip():
            raise ValueError("non-zero override requires override_reasoning")
        return self
