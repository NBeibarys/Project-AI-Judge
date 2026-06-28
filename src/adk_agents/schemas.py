"""Structured response contracts for the ADK review agents."""
from typing import Optional

from pydantic import BaseModel, Field


class CriterionEvidence(BaseModel):
    evidence: str
    notes: str


class AnalystReport(BaseModel):
    criteria: dict[str, CriterionEvidence]
    missing_sources: list[str] = Field(default_factory=list)


class GraderVerdict(BaseModel):
    approved: bool
    feedback: str = ""
    score: Optional[float] = None
    reasoning: Optional[str] = None
    confidence: Optional[str] = None
