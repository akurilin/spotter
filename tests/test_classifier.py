from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import patch

from spotter.classifier import classify_batch, classify_messages, post_openrouter, strip_code_fence, validate_matches
from spotter.config import LlmConfig, Topic
from spotter.errors import ClassificationError
from spotter.models import Match, Message
from spotter.usage import UsageAccumulator
from tests.support import TestCase


class FakeResponse:
    def __init__(self, payload: dict):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.body


class ClassifierTests(TestCase):
    def test_classify_messages_requires_openrouter_api_key(self):
        with (
            patch.dict("spotter.classifier.os.environ", {}, clear=True),
            self.assertRaisesRegex(ClassificationError, "OPENROUTER_API_KEY is not set"),
        ):
            classify_messages(
                LlmConfig(),
                1,
                (Topic(id="example_topic", name="Example topic", description="Synthetic topic."),),
                [example_message()],
            )

    def test_classify_batch_builds_openrouter_request_and_collects_usage(self):
        response = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"matches": []}'}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 10},
            },
        }
        accumulator = UsageAccumulator()

        with patch("spotter.classifier.post_openrouter", return_value=response) as post:
            matches = classify_batch(
                "test-key",
                LlmConfig(temperature=None),
                (
                    Topic(
                        id="example_topic",
                        name="Example topic",
                        description="Synthetic topic.",
                        positive_examples=("Should match.",),
                        negative_examples=("Should not match.",),
                    ),
                ),
                [example_message()],
                accumulator,
            )

        self.assertEqual([], matches)
        payload = post.call_args.args[0]
        self.assertEqual("anthropic/claude-sonnet-4.6", payload["model"])
        self.assertNotIn("temperature", payload)
        self.assertEqual("system", payload["messages"][0]["role"])
        self.assertIn("every message against every topic independently", payload["messages"][0]["content"])
        self.assertEqual("user", payload["messages"][1]["role"])
        classifier_input = json.loads(payload["messages"][1]["content"])
        self.assertEqual(
            [{"id": "example_topic", "name": "Example topic", "description": "Synthetic topic."}],
            classifier_input["topics"],
        )
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertNotIn(
            "confidence",
            payload["response_format"]["json_schema"]["schema"]["properties"]["matches"]["items"]["properties"],
        )
        self.assertEqual(
            {"require_parameters": True, "data_collection": "deny"},
            payload["provider"],
        )
        self.assertEqual(
            UsageAccumulator(
                batches=1,
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
            ),
            accumulator,
        )

    def test_classify_batch_retries_truncated_response_with_retry_max_tokens(self):
        truncated = {
            "choices": [{"finish_reason": "length", "message": {"content": '{"matches":'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        complete = {
            "choices": [{"finish_reason": "stop", "message": {"content": '{"matches": []}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }

        with patch("spotter.classifier.post_openrouter", side_effect=[truncated, complete]) as post:
            matches = classify_batch(
                "test-key",
                LlmConfig(max_tokens=100, retry_max_tokens=200),
                (Topic(id="example_topic", name="Example topic", description="Synthetic topic."),),
                [example_message()],
                UsageAccumulator(),
            )

        self.assertEqual([], matches)
        self.assertEqual(2, post.call_count)
        self.assertEqual(100, post.call_args_list[0].args[0]["max_tokens"])
        self.assertEqual(200, post.call_args_list[1].args[0]["max_tokens"])

    def test_post_openrouter_retries_transient_http_error_and_honors_retry_after(self):
        error = urllib.error.HTTPError(
            "https://openrouter.ai/api/v1/chat/completions",
            429,
            "Too Many Requests",
            {"Retry-After": "0"},
            io.BytesIO(b'{"error":{"code":429,"message":"rate limited"}}'),
        )
        success = FakeResponse(
            {
                "choices": [{"finish_reason": "stop", "message": {"content": '{"matches": []}'}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4},
            }
        )

        with (
            patch("spotter.classifier.urllib.request.urlopen", side_effect=[error, success]) as urlopen,
            patch("spotter.classifier.time.sleep") as sleep,
        ):
            response = post_openrouter({}, api_key="test-key", timeout_seconds=1, max_retries=1)

        self.assertEqual("stop", response["choices"][0]["finish_reason"])
        self.assertEqual(2, urlopen.call_count)
        sleep.assert_called_once_with(0)
        request = urlopen.call_args.args[0]
        self.assertEqual("Bearer test-key", request.get_header("Authorization"))

    def test_post_openrouter_rejects_embedded_api_error(self):
        response = FakeResponse({"error": {"code": 402, "message": "insufficient credits"}})

        with (
            patch("spotter.classifier.urllib.request.urlopen", return_value=response),
            self.assertRaisesRegex(ClassificationError, "OpenRouter API error 402: insufficient credits"),
        ):
            post_openrouter({}, api_key="test-key", timeout_seconds=1, max_retries=0)

    def test_classify_batch_accepts_markdown_fenced_json(self):
        fenced = {
            "choices": [{"finish_reason": "stop", "message": {"content": '```json\n{"matches": []}\n```'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4},
        }

        with patch("spotter.classifier.post_openrouter", return_value=fenced):
            matches = classify_batch(
                "test-key",
                LlmConfig(),
                (Topic(id="example_topic", name="Example topic", description="Synthetic topic."),),
                [example_message()],
                UsageAccumulator(),
            )

        self.assertEqual([], matches)

    def test_strip_code_fence_handles_common_wrappings(self):
        self.assertEqual('{"matches": []}', strip_code_fence('```json\n{"matches": []}\n```'))
        self.assertEqual('{"matches": []}', strip_code_fence('```\n{"matches": []}\n```'))
        self.assertEqual('{"matches": []}', strip_code_fence('{"matches": []}'))

    def test_validate_matches_keeps_multiple_topics_and_deduplicates_pairs(self):
        matches = validate_matches(
            {
                "matches": [
                    {
                        "message_pk": 42,
                        "topic_id": "example_topic",
                        "reason": "Clear match.",
                        "notification": "Example topic matched.",
                    },
                    {
                        "message_pk": 42,
                        "topic_id": "secondary_topic",
                        "reason": "Also a clear match.",
                        "notification": "Secondary topic matched.",
                    },
                    {
                        "message_pk": 42,
                        "topic_id": "example_topic",
                        "reason": "Duplicate match.",
                        "notification": "Duplicate match.",
                    },
                ]
            },
            message_pks={42},
            topic_ids={"example_topic", "secondary_topic"},
        )

        self.assertEqual(
            [
                Match(
                    message_pk=42,
                    topic_id="example_topic",
                    reason="Clear match.",
                    notification="Example topic matched.",
                ),
                Match(
                    message_pk=42,
                    topic_id="secondary_topic",
                    reason="Also a clear match.",
                    notification="Secondary topic matched.",
                ),
            ],
            matches,
        )


def example_message() -> Message:
    return Message(
        message_pk=42,
        group_name="Example group",
        group_jid="example-group@g.us",
        sender_name="Example sender",
        sender_jid="example-sender@s.whatsapp.net",
        local_time="2026-01-01 12:00:00",
        text="Synthetic test message.",
    )
