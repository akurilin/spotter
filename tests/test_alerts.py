from __future__ import annotations

from unittest.mock import patch

from spotter.models import Match, Message
from spotter.notifications import notify_alerts
from tests.support import TestCase, config_dict, load_spotter_cli, make_config

spotter_cli = load_spotter_cli()


class AlertTests(TestCase):
    def test_message_matching_multiple_topics_notifies_for_first_configured_topic_only(self):
        raw_config = config_dict(self.temp_dir)
        raw_config["topics"] = [
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
        ]
        config = make_config(self.temp_dir, raw_config)
        message = Message(
            message_pk=42,
            group_name="Founders Community",
            group_jid="12345-67890@g.us",
            sender_name="Founder",
            sender_jid="123456789@lid",
            local_time="2026-01-02 03:04:05",
            text="Anyone know the best way to start a CTO hunt?",
        )
        matches = (
            Match(
                message_pk=message.message_pk,
                topic_id="cto_coaching",
                confidence=0.78,
                reason="Founder needs CTO coaching.",
                notification="Founder looking for CTO coaching.",
            ),
            Match(
                message_pk=message.message_pk,
                topic_id="engineering_hiring",
                confidence=0.82,
                reason="Founder asks about CTO hiring.",
                notification="Founder seeking CTO hiring advice.",
            ),
        )

        alerts = spotter_cli.build_alerts(config.topics, [message], matches, existing_alert_keys=set())

        with patch("spotter.notifications.send_macos_notification") as send_notification:
            failures = notify_alerts(config.notifications, alerts)

        self.assertEqual([], failures)
        self.assertEqual(1, len(alerts))
        self.assertEqual("engineering_hiring", alerts[0].topic_id)
        send_notification.assert_called_once()


if __name__ == "__main__":
    import unittest

    unittest.main()
