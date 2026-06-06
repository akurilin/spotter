from __future__ import annotations

from types import SimpleNamespace

from spotter.classifier import classify_batch, validate_matches
from spotter.config import LlmConfig, Topic
from spotter.models import Match, Message
from spotter.usage import UsageAccumulator
from tests.support import TestCase


class ClassifierTests(TestCase):
    def test_classify_batch_omits_null_temperature(self):
        messages_api = SimpleNamespace()
        messages_api.create = lambda **kwargs: (
            setattr(messages_api, "kwargs", kwargs)
            or SimpleNamespace(parsed_output={"matches": []}, stop_reason="end_turn", usage=SimpleNamespace())
        )
        client = SimpleNamespace(messages=messages_api)

        classify_batch(
            client,
            LlmConfig(temperature=None),
            (Topic(id="example_topic", name="Example topic", description="Synthetic topic."),),
            [
                Message(
                    message_pk=42,
                    group_name="Example group",
                    group_jid="example-group@g.us",
                    sender_name="Example sender",
                    sender_jid="example-sender@s.whatsapp.net",
                    local_time="2026-01-01 12:00:00",
                    text="Synthetic test message.",
                )
            ],
            UsageAccumulator(),
        )

        self.assertNotIn("temperature", messages_api.kwargs)

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
