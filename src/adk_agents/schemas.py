"""Strict structured-response contracts for evidence extraction and grading.

All programs use the separate-head architecture with 3 distinct roles:

  - analyst  -> AnalystReport      (extract evidence only, no score)
  - grader   -> R2BGraderVerdict   (verify only: approve/reject + feedback)
  - head     -> R2BHeadScore       (score only, after approval)

The grader never scores; the head never verifies. This separation is what
makes multi-sample averaging of the Head meaningful (research gap #5):
only the Head is re-run, on the SAME approved evidence.

NOTE: These schemas intentionally do NOT use Pydantic's ``extra="forbid"``.
That config emits ``additionalProperties: false`` in the generated JSON
schema, which the genai SDK serializes as ``additional_properties``. The
Gemini Developer API (API key) rejects this field with 400
INVALID_ARGUMENT ("Unknown name 'additional_properties'"); Vertex AI
accepted it. The ``model_validator`` decorators on AnalystReport,
R2BGraderVerdict, and R2BHeadScore enforce the rubric contract at
validation time, so extra-field rejection at the schema level is not
required.

The AnalystReport validator reads the active-criteria set from a
module-level slot that ``set_active_criteria`` updates. Each program sets
its own criteria set at agent-build time.
"""
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

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
# Alchemist rubric (4 criteria, 5-band: 1-2 / 3-4 / 5-6 / 7-8 / 9-10, equal weights)
# ---------------------------------------------------------------------------

RUBRIC_CRITERIA_ALCHEMIST = (
    "Product/MVP & Innovation",
    "Market Potential",
    "Scalability & Readiness for the U.S. Market",
    "Team Strength",
)

RUBRIC_WEIGHTS_ALCHEMIST = {
    criterion: round(1.0 / 4.0, 4) for criterion in RUBRIC_CRITERIA_ALCHEMIST
}

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
    evidence: str = Field(min_length=1)
    notes: str = Field(min_length=1)
    # Cross-source consistency tag (Alchemist analyst only; no web search
    # tool is attached — see agent.py's google_search removal note).
    # 'verified' = corroborated by another of the applicant's own sources;
    # 'unverified' = appears in only one source (DO NOT penalize);
    # 'contradicted' = two of the applicant's own sources conflict on the
    # same fact (flag for grader). The Head scorer does not use this field.
    verification: Optional[Literal["verified", "unverified", "contradicted"]] = None


class WebVerificationEntry(BaseModel):
    """Per-criterion web verification result produced by the web_verifier agent.

    verification tags the analyst's evidence for a criterion:
    - verified: web search found supporting evidence
    - unverified: web search found nothing (NOT a penalty)
    - contradicted: web search found evidence contradicting the claim
    web_evidence states what was found (or, for 'unverified', what was searched).
    """
    verification: Literal["verified", "unverified", "contradicted"]
    web_evidence: str = Field(min_length=1)


class FellowshipV2WebVerificationReport(BaseModel):
    """Flat web verification report for all 9 Fellowship criteria.

    Produced by the dedicated web_verifier agent (runs between analyst and
    grader). Same explicit-named-field pattern as FellowshipV2AnalystReport
    so Gemini knows exactly what keys to fill. This schema is intentionally
    flat (not the complex AnalystReport) so it has NO output_schema conflict
    with google_search — the agent can use the google_search tool freely.
    """
    Originality: WebVerificationEntry
    Approach: WebVerificationEntry
    Personal_connection: WebVerificationEntry
    Concreteness: WebVerificationEntry
    Credibility_in_context: WebVerificationEntry
    Trajectory: WebVerificationEntry
    Program_fit: WebVerificationEntry
    Regional_relevance: WebVerificationEntry
    Communication_quality: WebVerificationEntry


class FellowshipV2AnalystReport(BaseModel):
    """Explicit schema with all 9 Fellowship criteria as named fields.

    This is sent to Gemini as response_schema so the model knows exactly
    what keys to fill. The generic dict[str, CriterionEvidence] in
    AnalystReport doesn't convey the required keys to the model.
    """
    Originality: CriterionEvidence
    Approach: CriterionEvidence
    Personal_connection: CriterionEvidence
    Concreteness: CriterionEvidence
    Credibility_in_context: CriterionEvidence
    Trajectory: CriterionEvidence
    Program_fit: CriterionEvidence
    Regional_relevance: CriterionEvidence
    Communication_quality: CriterionEvidence
    missing_sources: list[str] = Field(default_factory=list)

    def to_analyst_report(self) -> "AnalystReport":
        """Convert to the generic AnalystReport format for downstream agents."""
        return AnalystReport(
            criteria={
                "Originality": self.Originality,
                "Approach": self.Approach,
                "Personal connection": self.Personal_connection,
                "Concreteness": self.Concreteness,
                "Credibility in context": self.Credibility_in_context,
                "Trajectory": self.Trajectory,
                "Program fit": self.Program_fit,
                "Regional relevance": self.Regional_relevance,
                "Communication quality": self.Communication_quality,
            },
            missing_sources=self.missing_sources,
        )


class AnalystReport(BaseModel):
    """Exactly one evidence record for every approved rubric criterion."""
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


class R2BAnalystReport(BaseModel):
    """Explicit analyst schema for the R2B rubric with concrete field names."""
    # Written FIRST (field order drives generation order in structured
    # output) — same pattern as AlchemistAnalystReport.key_facts_cross_check.
    # R2B has no deck/text to draw on, so there's nothing to cross-check
    # against; this field instead makes the model do a full chronological
    # pass over the video in prose BEFORE committing to the 6 strict
    # per-criterion structured fields below. Confirmed live that without
    # this warm-up step, the analyst regularly produced incomplete/invalid
    # structured output (empty criteria dict, missing fields) on this
    # round's harder video-only extraction task; Alchemist's equivalent
    # scratchpad field never has this problem.
    video_notes: str = Field(
        min_length=1,
        description=(
            "STEP 1: Watch the full pitch video start to finish. Write a "
            "chronological walkthrough of what is said and shown — the "
            "problem, product/demo, market/business claims, team, and any "
            "numbers or dates mentioned, in the order they appear. Note "
            "explicitly if the same metric (revenue, users, timeline) is "
            "stated differently at two different points. Use this as your "
            "working notes before filling in the structured fields below."
        ),
    )
    Problem_Solution: CriterionEvidence
    Market_Potential: CriterionEvidence
    Product_MVP_Innovation: CriterionEvidence
    Team_Strength: CriterionEvidence
    Business_Model: CriterionEvidence
    Presentation_Clarity: CriterionEvidence
    missing_sources: list[str] = Field(default_factory=list)

    def to_analyst_report(self) -> "AnalystReport":
        """Convert to the generic AnalystReport format for downstream agents."""
        return AnalystReport(
            criteria={
                "Problem & Solution": self.Problem_Solution,
                "Market Potential": self.Market_Potential,
                "Product/MVP & Innovation": self.Product_MVP_Innovation,
                "Team Strength": self.Team_Strength,
                "Business Model": self.Business_Model,
                "Presentation & Clarity": self.Presentation_Clarity,
            },
            missing_sources=self.missing_sources,
        )


# ---------------------------------------------------------------------------
# R2B contracts: verify and score split across two agents.
# ---------------------------------------------------------------------------

class R2BGraderVerdict(BaseModel):
    """The R2B grader VERIFIES ONLY — it does not score.

    approve=true means the analyst evidence is grounded, real, and covers all
    6 criteria (with video evidence for criterion 6). approve=false carries
    actionable feedback for the analyst to revise.
    """
    approved: bool
    feedback: str = ""
    # Set true only when the grader has personally confirmed — by checking
    # the applicant's OWN sources against each other (no web search
    # available) — that the application contains a genuine, material
    # contradiction or fabrication, not just a thin/sloppy analyst write-up.
    # This is distinct from approved=false for an ordinary revisable evidence
    # gap: a real contradiction/fraud can't be fixed by asking the analyst to
    # revise, so it must end the review loop immediately with a score of 0
    # instead of looping to exhaustion. See R2BApprovalGate.
    disqualifying_issue_found: bool = False
    disqualifying_issue_type: Literal[
        "none", "contradiction", "fraud", "suspicious_application"
    ] = "none"
    disqualifying_issue_reason: str = ""

    @model_validator(mode="after")
    def enforce_decision_contract(self) -> "R2BGraderVerdict":
        if not self.approved and not self.feedback.strip():
            raise ValueError("rejected verdicts require actionable feedback")
        if self.approved and self.feedback.strip():
            # Approved with feedback is allowed (minor notes), but the grader
            # must not include scores — scoring is the Head's job.
            pass
        if self.disqualifying_issue_found:
            if self.approved:
                raise ValueError(
                    "disqualifying_issue_found requires approved=false"
                )
            if not self.disqualifying_issue_reason.strip():
                raise ValueError(
                    "disqualifying_issue_found requires disqualifying_issue_reason"
                )
        return self


class FellowshipV2HeadScore(BaseModel):
    """Explicit Head scorer schema with all 9 Fellowship criteria as named fields.

    Same purpose as R2BHeadScore but with explicit field names so Gemini
    knows exactly what keys to fill. Generic dict[str, int] produces empty
    results because the model doesn't know the criterion names.
    """
    Originality: int = Field(ge=1, le=10)
    Approach: int = Field(ge=1, le=10)
    Personal_connection: int = Field(ge=1, le=10)
    Concreteness: int = Field(ge=1, le=10)
    Credibility_in_context: int = Field(ge=1, le=10)
    Trajectory: int = Field(ge=1, le=10)
    Program_fit: int = Field(ge=1, le=10)
    Regional_relevance: int = Field(ge=1, le=10)
    Communication_quality: int = Field(ge=1, le=10)
    Originality_rationale: str = Field(min_length=1)
    Approach_rationale: str = Field(min_length=1)
    Personal_connection_rationale: str = Field(min_length=1)
    Concreteness_rationale: str = Field(min_length=1)
    Credibility_in_context_rationale: str = Field(min_length=1)
    Trajectory_rationale: str = Field(min_length=1)
    Program_fit_rationale: str = Field(min_length=1)
    Regional_relevance_rationale: str = Field(min_length=1)
    Communication_quality_rationale: str = Field(min_length=1)
    final_score: float = Field(ge=1, le=10)
    override: float = Field(default=0.0, ge=-1.0, le=1.0)
    override_reasoning: str = ""
    confidence: Literal["low", "medium", "high"]

    def to_head_score(self) -> "R2BHeadScore":
        """Convert to the generic R2BHeadScore format for averaging."""
        return R2BHeadScore(
            criterion_scores={
                "Originality": self.Originality,
                "Approach": self.Approach,
                "Personal connection": self.Personal_connection,
                "Concreteness": self.Concreteness,
                "Credibility in context": self.Credibility_in_context,
                "Trajectory": self.Trajectory,
                "Program fit": self.Program_fit,
                "Regional relevance": self.Regional_relevance,
                "Communication quality": self.Communication_quality,
            },
            criterion_rationale={
                "Originality": self.Originality_rationale,
                "Approach": self.Approach_rationale,
                "Personal connection": self.Personal_connection_rationale,
                "Concreteness": self.Concreteness_rationale,
                "Credibility in context": self.Credibility_in_context_rationale,
                "Trajectory": self.Trajectory_rationale,
                "Program fit": self.Program_fit_rationale,
                "Regional relevance": self.Regional_relevance_rationale,
                "Communication quality": self.Communication_quality_rationale,
            },
            final_score=self.final_score,
            override=self.override,
            override_reasoning=self.override_reasoning,
            confidence=self.confidence,
        )


class AlchemistAnalystReport(BaseModel):
    """Explicit schema with all 4 Alchemist criteria as named fields.

    Same purpose as FellowshipV2AnalystReport: sent to Gemini as
    response_schema so the model knows exactly what keys to fill. The
    generic dict[str, CriterionEvidence] in AnalystReport doesn't convey
    the required keys to the model.
    """
    # Written FIRST (field order drives generation order in structured
    # output), before any per-criterion evidence — forces the model to do
    # the cross-source comparison as an explicit step rather than notice
    # contradictions only incidentally while extracting evidence for 4
    # other things. Same rationale as the Head's rationale-before-score.
    key_facts_cross_check: str = Field(
        min_length=1,
        description=(
            "STEP 1: go through the deck slide by slide FIRST and read every "
            "chart, graph, table, or image-only slide (no selectable text) — "
            "these are the most common place exact revenue/ARR/user numbers "
            "live, and the easiest to skim past since there's no text to "
            "skim. Do the same for anything shown on screen in the video. "
            "STEP 2: using what you just read, list every checkable, "
            "specific fact given in more than one source (deck, application "
            "text, video, any applicant URL) — revenue/traction numbers, "
            "user or customer counts, launch/founding date, team size, "
            "business model. For EACH such fact, state what each source "
            "says about it, e.g. 'Revenue: deck chart on p.8 shows $1M ARR "
            "for 2025; application text says $800K.' If two sources "
            "disagree on the same fact, say so explicitly here. If nothing "
            "repeats across sources, say so."
        ),
    )
    Product_MVP_Innovation: CriterionEvidence
    Market_Potential: CriterionEvidence
    Scalability_US_Market: CriterionEvidence
    Team_Strength: CriterionEvidence
    missing_sources: list[str] = Field(default_factory=list)

    def to_analyst_report(self) -> "AnalystReport":
        """Convert to the generic AnalystReport format for downstream agents."""
        return AnalystReport(
            criteria={
                "Product/MVP & Innovation": self.Product_MVP_Innovation,
                "Market Potential": self.Market_Potential,
                "Scalability & Readiness for the U.S. Market": self.Scalability_US_Market,
                "Team Strength": self.Team_Strength,
            },
            missing_sources=self.missing_sources,
        )


class AlchemistHeadScore(BaseModel):
    """Explicit Head scorer schema with all 4 Alchemist criteria as named fields.

    Same purpose as FellowshipV2HeadScore but for the Alchemist rubric (4
    criteria instead of 9). The validator enforces 1-10 integer scores and
    requires rationale for every criterion before the score (rationale-before-
    score CoT). Includes a minor ±1.0 override with written reasoning.
    """
    Product_MVP_Innovation: int = Field(ge=1, le=10)
    Market_Potential: int = Field(ge=1, le=10)
    Scalability_US_Market: int = Field(ge=1, le=10)
    Team_Strength: int = Field(ge=1, le=10)
    Product_MVP_Innovation_rationale: str = Field(min_length=1)
    Market_Potential_rationale: str = Field(min_length=1)
    Scalability_US_Market_rationale: str = Field(min_length=1)
    Team_Strength_rationale: str = Field(min_length=1)
    final_score: float = Field(ge=1, le=10)
    override: float = Field(default=0.0, ge=-1.0, le=1.0)
    override_reasoning: str = ""
    confidence: Literal["low", "medium", "high"]
    # See R2BHeadScore disqualification fields — same contract.
    contradiction_found: bool = False
    contradiction_reason: str = ""
    disqualifying_issue_found: bool = False
    disqualifying_issue_type: Literal[
        "none", "contradiction", "fraud", "suspicious_application"
    ] = "none"
    disqualifying_issue_reason: str = ""

    def to_head_score(self) -> "R2BHeadScore":
        """Convert to the generic R2BHeadScore format for averaging."""
        return R2BHeadScore(
            criterion_scores={
                "Product/MVP & Innovation": self.Product_MVP_Innovation,
                "Market Potential": self.Market_Potential,
                "Scalability & Readiness for the U.S. Market": self.Scalability_US_Market,
                "Team Strength": self.Team_Strength,
            },
            criterion_rationale={
                "Product/MVP & Innovation": self.Product_MVP_Innovation_rationale,
                "Market Potential": self.Market_Potential_rationale,
                "Scalability & Readiness for the U.S. Market": self.Scalability_US_Market_rationale,
                "Team Strength": self.Team_Strength_rationale,
            },
            final_score=self.final_score,
            override=self.override,
            override_reasoning=self.override_reasoning,
            confidence=self.confidence,
            contradiction_found=self.contradiction_found,
            contradiction_reason=self.contradiction_reason,
            disqualifying_issue_found=self.disqualifying_issue_found,
            disqualifying_issue_type=self.disqualifying_issue_type,
            disqualifying_issue_reason=self.disqualifying_issue_reason,
        )


class R2BHeadScore(BaseModel):
    """The Head scorer SCORES ONLY — it does not verify.

    Scores approved evidence 1-10 per criterion using the band-then-integer
    method. Writes rationale BEFORE score (rationale-before-score CoT). Final
    score = average of criterion scores. A minor ±1 adjustment is allowed
    with reasoning. Confidence reflects evidence quality, not video length.

    Used by any program with uses_separate_head=True (R2B with 6 criteria,
    Fellowship V2 with 9 criteria). The validator reads the active-criteria
    slot via get_active_criteria() so the same schema enforces the right set
    regardless of program.
    """
    criterion_scores: dict[str, int]
    criterion_rationale: dict[str, str]
    final_score: float = Field(ge=1, le=10)
    override: float = Field(default=0.0, ge=-1.0, le=1.0)
    override_reasoning: str = ""
    confidence: Literal["low", "medium", "high"]
    # Set true only for a genuine, material contradiction between sources
    # (e.g. deck vs video vs website disagreeing on a real claim) — not
    # merely unverified or missing evidence. Kept for backward compatibility;
    # new Alchemist runs also use the broader disqualifying_issue_* fields.
    contradiction_found: bool = False
    contradiction_reason: str = ""
    # Confirmed issues that should score the whole application 0. This is
    # intentionally broader than contradiction so fraud/template-like or
    # materially suspicious applications do not receive ordinary rubric scores.
    disqualifying_issue_found: bool = False
    disqualifying_issue_type: Literal[
        "none", "contradiction", "fraud", "suspicious_application"
    ] = "none"
    disqualifying_issue_reason: str = ""

    @model_validator(mode="after")
    def enforce_score_contract(self) -> "R2BHeadScore":
        active = set(get_active_criteria())
        supplied_scores = set(self.criterion_scores)
        if supplied_scores != active:
            raise ValueError(
                "criterion_scores must match the active rubric; "
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
                "criterion_rationale must match the active rubric; "
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


class R2BHeadScoreNamed(BaseModel):
    """Explicit head-scoring schema for the R2B rubric with concrete field names."""
    Problem_Solution: int = Field(ge=1, le=10)
    Market_Potential: int = Field(ge=1, le=10)
    Product_MVP_Innovation: int = Field(ge=1, le=10)
    Team_Strength: int = Field(ge=1, le=10)
    Business_Model: int = Field(ge=1, le=10)
    Presentation_Clarity: int = Field(ge=1, le=10)
    Problem_Solution_rationale: str = Field(min_length=1)
    Market_Potential_rationale: str = Field(min_length=1)
    Product_MVP_Innovation_rationale: str = Field(min_length=1)
    Team_Strength_rationale: str = Field(min_length=1)
    Business_Model_rationale: str = Field(min_length=1)
    Presentation_Clarity_rationale: str = Field(min_length=1)
    final_score: float = Field(ge=1, le=10)
    override: float = Field(default=0.0, ge=-1.0, le=1.0)
    override_reasoning: str = ""
    confidence: Literal["low", "medium", "high"]
    contradiction_found: bool = False
    contradiction_reason: str = ""
    disqualifying_issue_found: bool = False
    disqualifying_issue_type: Literal[
        "none", "contradiction", "fraud", "suspicious_application"
    ] = "none"
    disqualifying_issue_reason: str = ""

    def to_head_score(self) -> "R2BHeadScore":
        """Convert to the generic R2BHeadScore format for downstream agents."""
        return R2BHeadScore(
            criterion_scores={
                "Problem & Solution": self.Problem_Solution,
                "Market Potential": self.Market_Potential,
                "Product/MVP & Innovation": self.Product_MVP_Innovation,
                "Team Strength": self.Team_Strength,
                "Business Model": self.Business_Model,
                "Presentation & Clarity": self.Presentation_Clarity,
            },
            criterion_rationale={
                "Problem & Solution": self.Problem_Solution_rationale,
                "Market Potential": self.Market_Potential_rationale,
                "Product/MVP & Innovation": self.Product_MVP_Innovation_rationale,
                "Team Strength": self.Team_Strength_rationale,
                "Business Model": self.Business_Model_rationale,
                "Presentation & Clarity": self.Presentation_Clarity_rationale,
            },
            final_score=self.final_score,
            override=self.override,
            override_reasoning=self.override_reasoning,
            confidence=self.confidence,
            contradiction_found=self.contradiction_found,
            contradiction_reason=self.contradiction_reason,
            disqualifying_issue_found=self.disqualifying_issue_found,
            disqualifying_issue_type=self.disqualifying_issue_type,
            disqualifying_issue_reason=self.disqualifying_issue_reason,
        )
