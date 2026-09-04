import unittest

from src.adk_agents.schemas import (
    RUBRIC_CRITERIA,
    RUBRIC_CRITERIA_ALCHEMIST,
    RUBRIC_CRITERIA_R2B,
    AlchemistHeadScore,
    FellowshipV2HeadScore,
    R2BHeadScoreNamed,
)
from src.adk_agents.workflow import _convert_named_head

SCHEMAS = [
    (FellowshipV2HeadScore, RUBRIC_CRITERIA),
    (AlchemistHeadScore, RUBRIC_CRITERIA_ALCHEMIST),
    (R2BHeadScoreNamed, RUBRIC_CRITERIA_R2B),
]


class ConvertNamedHeadTests(unittest.TestCase):
    """_convert_named_head flattens each named head schema's per-criterion
    fields into the criterion_scores/criterion_rationale dicts
    _average_head_samples expects."""

    def test_maps_every_criterion_and_rationale(self) -> None:
        for schema, criteria in SCHEMAS:
            with self.subTest(schema=schema.__name__):
                flat = {}
                for i, field in enumerate(schema.CRITERION_FIELDS):
                    flat[f"{field}_rationale"] = f"rationale {i}"
                    flat[field] = i + 4
                flat["final_score"] = 5.5
                flat["confidence"] = "medium"

                result = _convert_named_head(flat, schema.CRITERION_FIELDS)

                self.assertEqual(set(result["criterion_scores"]), set(criteria))
                for field, criterion in schema.CRITERION_FIELDS.items():
                    self.assertEqual(
                        result["criterion_scores"][criterion], flat[field]
                    )
                    self.assertEqual(
                        result["criterion_rationale"][criterion],
                        flat[f"{field}_rationale"],
                    )
                self.assertEqual(result["final_score"], 5.5)
                self.assertEqual(result["confidence"], "medium")

    def test_missing_field_fails_the_sample_instead_of_defaulting(self) -> None:
        for schema, _ in SCHEMAS:
            with self.subTest(schema=schema.__name__):
                flat = {field: 5 for field in schema.CRITERION_FIELDS}
                flat.update(
                    {f"{field}_rationale": "r" for field in schema.CRITERION_FIELDS},
                    final_score=5.0,
                    confidence="low",
                )
                del flat[next(iter(schema.CRITERION_FIELDS))]
                with self.assertRaises(KeyError):
                    _convert_named_head(flat, schema.CRITERION_FIELDS)


if __name__ == "__main__":
    unittest.main()
