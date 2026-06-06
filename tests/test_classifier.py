from __future__ import annotations

from spotter.classifier import validate_matches
from spotter.models import Match
from tests.support import TestCase


class ClassifierTests(TestCase):
    def test_validate_matches_returns_typed_matches(self):
        matches = validate_matches(
            {
                "matches": [
                    {
                        "message_pk": 42,
                        "topic_id": "example_topic",
                        "confidence": 0.9,
                        "reason": "Clear match.",
                        "notification": "Example topic matched.",
                    }
                ]
            },
            message_pks={42},
            topic_ids={"example_topic"},
        )

        self.assertEqual(
            [
                Match(
                    message_pk=42,
                    topic_id="example_topic",
                    confidence=0.9,
                    reason="Clear match.",
                    notification="Example topic matched.",
                )
            ],
            matches,
        )
