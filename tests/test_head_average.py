import dataclasses
import unittest

from src.adk_agents.workflow import AdkReviewWorkflow
from src.programs import get_program_config


class AverageHeadSamplesTests(unittest.TestCase):
    def test_no_matching_criteria_escalates_instead_of_dividing_by_zero(
        self,
    ) -> None:
        # Criterion-name drift between the schema and the program config
        # leaves every averaged criterion empty; that must escalate the row,
        # not raise ZeroDivisionError inside a worker thread.
        program_config = dataclasses.replace(
            get_program_config("r2b"),
            rubric_criteria=("A",),
        )
        workflow = AdkReviewWorkflow(
            analyzer_model="gemini-dummy-analyzer",
            grader_model="gemini-dummy-grader",
            program_config=program_config,
        )

        result = workflow._average_head_samples(
            [{"criterion_scores": {"B": 7}, "criterion_rationale": {"B": "r"}}]
        )

        self.assertIsNone(result["score"])
        self.assertTrue(result["human_review_flag"])
        self.assertEqual(result["confidence"], "n/a")


if __name__ == "__main__":
    unittest.main()
