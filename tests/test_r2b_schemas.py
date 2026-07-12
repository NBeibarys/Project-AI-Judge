import unittest

from src.adk_agents.schemas import (
    CriterionEvidence,
    RUBRIC_CRITERIA_R2B,
    R2BAnalystReport,
    R2BHeadScoreNamed,
    set_active_criteria,
)


class R2BSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        set_active_criteria(RUBRIC_CRITERIA_R2B)

    def test_r2b_analyst_report_converts_to_generic_report(self) -> None:
        report = R2BAnalystReport(
            video_notes="Chronological walkthrough of the video.",
            Problem_Solution=CriterionEvidence(evidence="demo", notes="demo"),
            Market_Potential=CriterionEvidence(evidence="demo", notes="demo"),
            Product_MVP_Innovation=CriterionEvidence(evidence="demo", notes="demo"),
            Team_Strength=CriterionEvidence(evidence="demo", notes="demo"),
            Business_Model=CriterionEvidence(evidence="demo", notes="demo"),
            Presentation_Clarity=CriterionEvidence(evidence="demo", notes="demo"),
        )

        converted = report.to_analyst_report()

        self.assertEqual(set(converted.criteria), set(RUBRIC_CRITERIA_R2B))

    def test_r2b_head_score_named_schema_converts_to_generic_payload(self) -> None:
        score = R2BHeadScoreNamed(
            Problem_Solution=7,
            Market_Potential=8,
            Product_MVP_Innovation=6,
            Team_Strength=7,
            Business_Model=8,
            Presentation_Clarity=9,
            Problem_Solution_rationale="Good fit",
            Market_Potential_rationale="Strong demand",
            Product_MVP_Innovation_rationale="Clear MVP",
            Team_Strength_rationale="Strong team",
            Business_Model_rationale="Solid monetization",
            Presentation_Clarity_rationale="Clear pitch",
            final_score=7.5,
            confidence="high",
        )

        converted = score.to_head_score()

        self.assertEqual(set(converted.criterion_scores), set(RUBRIC_CRITERIA_R2B))
        self.assertEqual(converted.final_score, 7.5)


if __name__ == "__main__":
    unittest.main()
