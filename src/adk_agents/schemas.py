"""Strict structured-response contracts for evidence extraction and grading.

All programs use the separate-head architecture with 3 distinct roles:

  - analyst  -> per-program named evidence schema (extract evidence only,
                no score)
  - grader   -> R2BGraderVerdict   (verify only: approve/reject + feedback)
  - head     -> per-program named score schema (score only, after approval)

The grader never scores; the head never verifies. This separation is what
makes multi-sample averaging of the Head meaningful (research gap #5):
only the Head is re-run, on the SAME approved evidence.

NOTE: These schemas intentionally do NOT use Pydantic's ``extra="forbid"``.
That config emits ``additionalProperties: false`` in the generated JSON
schema, which the genai SDK serializes as ``additional_properties``. The
Gemini Developer API (API key) rejects this field with 400
INVALID_ARGUMENT ("Unknown name 'additional_properties'"); Vertex AI
accepted it. Each named schema declares every criterion as a required,
bounded field, and R2BGraderVerdict's ``model_validator`` enforces the
verdict contract, so extra-field rejection at the schema level is not
required.
"""
from typing import ClassVar, Literal, Optional

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

# ---------------------------------------------------------------------------
# Alchemist rubric (4 criteria, 5-band: 1-2 / 3-4 / 5-6 / 7-8 / 9-10, equal weights)
# ---------------------------------------------------------------------------

RUBRIC_CRITERIA_ALCHEMIST = (
    "Product/MVP & Innovation",
    "Market Potential",
    "Scalability & Readiness for the U.S. Market",
    "Team Strength",
)

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


class FellowshipV2AnalystReport(BaseModel):
    """Explicit schema with all 9 Fellowship criteria as named fields.

    This is sent to Gemini as response_schema so the model knows exactly
    what keys to fill. A generic dict[str, CriterionEvidence] does not
    convey the required keys to the model.
    """
    # Written FIRST (field order drives generation order in structured
    # output) — same pattern as R2BAnalystReport.video_notes. Fellowship V2
    # has both a video AND application text (problem description, results,
    # how-they-heard, other info), so this is a full walkthrough of BOTH,
    # not just the video, before the model commits to the 9 strict
    # per-criterion structured fields below. Mirrors R2B's finding that a
    # narrative warm-up pass before structured extraction avoids the
    # compressed-evidence miscalibration seen with LOW Head thinking and no
    # media; unvalidated for Fellowship V2 specifically as of this change.
    video_notes: str = Field(
        min_length=1,
        description=(
            "STEP 1: Watch the full video start to finish AND read the "
            "entire written application (problem description, results, how "
            "they heard about Silkroad, and any other info shared). Write "
            "one comprehensive chronological/thematic walkthrough covering "
            "both — the problem, approach, personal motivation, results and "
            "impact, program fit, regional relevance, and video delivery "
            "(eye contact, pacing, scripted vs natural delivery) — in the "
            "order each source presents it. Note explicitly if the video "
            "and the written text state the same fact (a number, date, or "
            "claim) differently. Use this as your working notes before "
            "filling in the structured fields below."
        ),
    )
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
        # Approved with feedback is allowed (minor notes) and deliberately
        # unvalidated: the grader must not include scores, but that is a
        # prompt-level rule, not something this contract can check.
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

    Same purpose as R2BHeadScoreNamed, for the Fellowship V2 rubric.
    Explicit field names so Gemini knows exactly what keys to fill: a
    generic dict[str, int] produces empty results because the model
    doesn't know the criterion names.

    Field order matters here, not just the prompt text: structured-output
    generation follows field declaration order, so each criterion's
    rationale field is declared immediately before its score field
    (matching R2BHeadScoreNamed's/AlchemistHeadScore's pattern) — an
    earlier version of this schema listed all 9 scores first and all 9
    rationales after, which silently contradicted its own
    "rationale-before-score" claim in FELLOWSHIP_V2_HEAD_INSTRUCTION.
    """
    # Field name -> rubric criterion name. Consumed by workflow.py's
    # _convert_named_head to flatten this schema's output into the
    # criterion_scores/criterion_rationale dicts the averaging code uses.
    # Lives next to the field declarations so the two cannot drift apart.
    CRITERION_FIELDS: ClassVar[dict[str, str]] = {
        "Originality": "Originality",
        "Approach": "Approach",
        "Personal_connection": "Personal connection",
        "Concreteness": "Concreteness",
        "Credibility_in_context": "Credibility in context",
        "Trajectory": "Trajectory",
        "Program_fit": "Program fit",
        "Regional_relevance": "Regional relevance",
        "Communication_quality": "Communication quality",
    }

    Originality_rationale: str = Field(min_length=1)
    Originality: int = Field(ge=1, le=10)
    Approach_rationale: str = Field(min_length=1)
    Approach: int = Field(ge=1, le=10)
    Personal_connection_rationale: str = Field(min_length=1)
    Personal_connection: int = Field(ge=1, le=10)
    Concreteness_rationale: str = Field(min_length=1)
    Concreteness: int = Field(ge=1, le=10)
    Credibility_in_context_rationale: str = Field(min_length=1)
    Credibility_in_context: int = Field(ge=1, le=10)
    Trajectory_rationale: str = Field(min_length=1)
    Trajectory: int = Field(ge=1, le=10)
    Program_fit_rationale: str = Field(min_length=1)
    Program_fit: int = Field(ge=1, le=10)
    Regional_relevance_rationale: str = Field(min_length=1)
    Regional_relevance: int = Field(ge=1, le=10)
    Communication_quality_rationale: str = Field(min_length=1)
    Communication_quality: int = Field(ge=1, le=10)
    final_score: float = Field(ge=1, le=10)
    confidence: Literal["low", "medium", "high"]
    # See R2BHeadScoreNamed's disqualification fields — same contract.
    contradiction_found: bool = False
    contradiction_reason: str = ""
    disqualifying_issue_found: bool = False
    disqualifying_issue_type: Literal[
        "none", "fraud", "suspicious_application"
    ] = "none"
    disqualifying_issue_reason: str = ""


class AlchemistAnalystReport(BaseModel):
    """Explicit schema with all 4 Alchemist criteria as named fields.

    Same purpose as FellowshipV2AnalystReport: sent to Gemini as
    response_schema so the model knows exactly what keys to fill. A
    generic dict[str, CriterionEvidence] does not convey the required
    keys to the model.
    """
    # Written FIRST, before key_facts_cross_check (field order drives
    # generation order in structured output) — mirrors R2BAnalystReport's
    # video_notes pattern: a full read-through of the sources in the order
    # they present material, BEFORE the narrower, targeted cross-source
    # fact-comparison pass below. This is a broad narrative anchor;
    # key_facts_cross_check remains the separate, focused tool for
    # checkable-fact comparison and is unchanged. Unvalidated for Alchemist
    # specifically as of this change — see R2B's video_notes comment for
    # the empirical history this pattern is based on.
    source_walkthrough: str = Field(
        min_length=1,
        description=(
            "STEP 1: Read through the entire pitch deck slide by slide AND "
            "the full application text (both are mandatory sources). If a "
            "video was also submitted (optional for Alchemist), watch it "
            "too. Write one comprehensive walkthrough — in the order the "
            "sources present it — covering product/MVP details, market and "
            "business model claims, team background, and traction/revenue "
            "figures, and, if a video is present, what it adds beyond the "
            "deck and text. Use this as your working notes before filling "
            "in key_facts_cross_check and the structured fields below."
        ),
    )
    # Written SECOND (field order drives generation order in structured
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


class AlchemistHeadScore(BaseModel):
    """Explicit Head scorer schema with all 4 Alchemist criteria as named fields.

    Same purpose as FellowshipV2HeadScore but for the Alchemist rubric (4
    criteria instead of 9). Every criterion is a required integer 1-10
    field preceded by its required rationale (rationale-before-score CoT),
    so the field declarations are the contract.

    Field order matters here, not just the prompt text: structured-output
    generation follows field declaration order, so each criterion's
    rationale field is declared immediately before its score field
    (matching R2BHeadScoreNamed's pattern) — an earlier version of this
    schema listed all 4 scores first and all 4 rationales after, which
    silently contradicted its own "rationale-before-score" claim above.
    """
    # See FellowshipV2HeadScore.CRITERION_FIELDS.
    CRITERION_FIELDS: ClassVar[dict[str, str]] = {
        "Product_MVP_Innovation": "Product/MVP & Innovation",
        "Market_Potential": "Market Potential",
        "Scalability_US_Market": "Scalability & Readiness for the U.S. Market",
        "Team_Strength": "Team Strength",
    }

    Product_MVP_Innovation_rationale: str = Field(min_length=1)
    Product_MVP_Innovation: int = Field(ge=1, le=10)
    Market_Potential_rationale: str = Field(min_length=1)
    Market_Potential: int = Field(ge=1, le=10)
    Scalability_US_Market_rationale: str = Field(min_length=1)
    Scalability_US_Market: int = Field(ge=1, le=10)
    Team_Strength_rationale: str = Field(min_length=1)
    Team_Strength: int = Field(ge=1, le=10)
    final_score: float = Field(ge=1, le=10)
    confidence: Literal["low", "medium", "high"]
    # See R2BHeadScoreNamed's disqualification fields — same contract.
    contradiction_found: bool = False
    contradiction_reason: str = ""
    disqualifying_issue_found: bool = False
    disqualifying_issue_type: Literal[
        "none", "contradiction", "fraud", "suspicious_application"
    ] = "none"
    disqualifying_issue_reason: str = ""


class R2BHeadScoreNamed(BaseModel):
    """Explicit head-scoring schema for the R2B rubric with concrete field names."""
    # See FellowshipV2HeadScore.CRITERION_FIELDS.
    CRITERION_FIELDS: ClassVar[dict[str, str]] = {
        "Problem_Solution": "Problem & Solution",
        "Market_Potential": "Market Potential",
        "Product_MVP_Innovation": "Product/MVP & Innovation",
        "Team_Strength": "Team Strength",
        "Business_Model": "Business Model",
        "Presentation_Clarity": "Presentation & Clarity",
    }

    Problem_Solution_rationale: str = Field(min_length=1)
    Problem_Solution: int = Field(ge=1, le=10)
    Market_Potential_rationale: str = Field(min_length=1)
    Market_Potential: int = Field(ge=1, le=10)
    Product_MVP_Innovation_rationale: str = Field(min_length=1)
    Product_MVP_Innovation: int = Field(ge=1, le=10)
    Team_Strength_rationale: str = Field(min_length=1)
    Team_Strength: int = Field(ge=1, le=10)
    Business_Model_rationale: str = Field(min_length=1)
    Business_Model: int = Field(ge=1, le=10)
    Presentation_Clarity_rationale: str = Field(min_length=1)
    Presentation_Clarity: int = Field(ge=1, le=10)
    final_score: float = Field(ge=1, le=10)
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


# Program -> output schema, for agent.py's builders and workflow.py's
# head-output flattening. Replaces the per-program if/elif chains, which
# carried unreachable generic fallbacks (ProgramName is a 3-value Literal
# and get_program_config raises on anything else).
ANALYST_SCHEMA_BY_PROGRAM: dict[str, type[BaseModel]] = {
    "fellowship_v2": FellowshipV2AnalystReport,
    "alchemist": AlchemistAnalystReport,
    "r2b": R2BAnalystReport,
}
HEAD_SCHEMA_BY_PROGRAM: dict[str, type[BaseModel]] = {
    "fellowship_v2": FellowshipV2HeadScore,
    "alchemist": AlchemistHeadScore,
    "r2b": R2BHeadScoreNamed,
}
