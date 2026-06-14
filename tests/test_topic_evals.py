from __future__ import annotations

import io
from unittest.mock import patch

from spotter.models import ClassificationResult, Match
from spotter.topic_evals import build_topic_eval_examples, evaluate_topic_examples, run_topic_evals
from spotter.usage import UsageAccumulator
from tests.support import TestCase, config_dict, make_config


class TopicEvalTests(TestCase):
    def test_topic_examples_only_assert_their_associated_topic(self):
        raw = config_dict(self.temp_dir)
        raw["topics"] = [
            {
                "id": "primary",
                "name": "Primary",
                "description": "Primary topic.",
                "positive_examples": ["Overlapping positive."],
                "negative_examples": ["Different topic only."],
            },
            {
                "id": "secondary",
                "name": "Secondary",
                "description": "Secondary topic.",
            },
        ]
        config = make_config(self.temp_dir, raw)
        examples = build_topic_eval_examples(config.topics)
        matches = (
            Match(
                message_pk=examples[0].message.message_pk,
                topic_id="primary",
                reason="Matches primary.",
                notification="Primary match.",
            ),
            Match(
                message_pk=examples[0].message.message_pk,
                topic_id="secondary",
                reason="Also matches secondary.",
                notification="Secondary match.",
            ),
            Match(
                message_pk=examples[1].message.message_pk,
                topic_id="secondary",
                reason="Matches a different topic.",
                notification="Secondary match.",
            ),
        )

        self.assertEqual([True, True], evaluate_topic_examples(examples, matches))

    def test_run_topic_evals_requires_every_fixed_run_to_pass(self):
        raw = config_dict(self.temp_dir)
        raw["topics"][0]["positive_examples"] = ["Expected positive."]
        config = make_config(self.temp_dir, raw)
        passing_match = Match(
            message_pk=1,
            topic_id="example_topic",
            reason="Matches.",
            notification="Match.",
        )

        with (
            patch(
                "spotter.topic_evals.classify_messages",
                side_effect=[
                    ClassificationResult(matches=(passing_match,), usage=UsageAccumulator()),
                    ClassificationResult(matches=(), usage=UsageAccumulator()),
                ],
            ) as classify,
            patch("sys.stdout", new_callable=io.StringIO) as stdout,
        ):
            result = run_topic_evals(config, runs=2)

        self.assertEqual(1, result)
        self.assertEqual(2, classify.call_count)
        self.assertIn("[FAIL 1/2] example_topic positive", stdout.getvalue())
