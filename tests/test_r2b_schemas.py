import unittest

from src.adk_agents.schemas import R2BHeadScoreNamed


class R2BSchemaTests(unittest.TestCase):
    def test_r2b_head_schema_requests_each_rationale_before_its_score(self) -> None:
        scoring_fields = list(R2BHeadScoreNamed.model_fields)[:12]

        self.assertEqual(
            scoring_fields,
            [
                "Problem_Solution_rationale",
                "Problem_Solution",
                "Market_Potential_rationale",
                "Market_Potential",
                "Product_MVP_Innovation_rationale",
                "Product_MVP_Innovation",
                "Team_Strength_rationale",
                "Team_Strength",
                "Business_Model_rationale",
                "Business_Model",
                "Presentation_Clarity_rationale",
                "Presentation_Clarity",
            ],
        )


if __name__ == "__main__":
    unittest.main()
