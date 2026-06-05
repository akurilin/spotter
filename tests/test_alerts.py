from __future__ import annotations

from unittest.mock import patch

from spotter.notifications import notify_alerts
from spotter.whatsapp_db import Message
from tests.support import TestCase, load_spotter_cli

spotter_cli = load_spotter_cli()


class AlertTests(TestCase):
    def test_message_matching_multiple_topics_notifies_for_first_configured_topic_only(self):
        config = {
            "notifications": {"macos": True, "pushover": False},
            "topics": [
                {
                    "id": "engineering_hiring",
                    "name": "Engineering hiring",
                    "description": "Engineering hiring advice",
                    "threshold": 0.75,
                },
                {
                    "id": "cto_coaching",
                    "name": "CTO coaching",
                    "description": "CTO coaching opportunities",
                    "threshold": 0.75,
                },
            ],
        }
        message = Message(
            message_pk=42,
            group_name="Founders Community",
            group_jid="12345-67890@g.us",
            sender_name="Founder",
            sender_jid="123456789@lid",
            local_time="2026-01-02 03:04:05",
            text="Anyone know the best way to start a CTO hunt?",
        )
        matches = [
            {
                "message_pk": message.message_pk,
                "topic_id": "cto_coaching",
                "confidence": 0.78,
                "reason": "Founder needs CTO coaching.",
                "notification": "Founder looking for CTO coaching.",
            },
            {
                "message_pk": message.message_pk,
                "topic_id": "engineering_hiring",
                "confidence": 0.82,
                "reason": "Founder asks about CTO hiring.",
                "notification": "Founder seeking CTO hiring advice.",
            },
        ]

        alerts = spotter_cli.build_alerts(config, [message], matches, existing_alert_keys=set())

        with patch("spotter.notifications.send_macos_notification") as send_notification:
            failures = notify_alerts(config, alerts)

        self.assertEqual([], failures)
        self.assertEqual(1, len(alerts))
        self.assertEqual("engineering_hiring", alerts[0]["topic_id"])
        send_notification.assert_called_once()


if __name__ == "__main__":
    import unittest

    unittest.main()
