from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from typing import Any

from spotter.config import LlmConfig, Topic
from spotter.errors import ClassificationError
from spotter.models import ClassificationResult, Match, Message
from spotter.usage import UsageAccumulator

try:
    from anthropic import Anthropic, APIConnectionError, APIStatusError, APITimeoutError
except ImportError:  # pragma: no cover - handled at runtime for clear setup errors.
    Anthropic = None  # type: ignore[assignment]
    APIConnectionError = APIStatusError = APITimeoutError = Exception  # type: ignore[misc,assignment]


LOGGER = logging.getLogger("spotter")


def classify_messages(
    config: LlmConfig,
    batch_size: int,
    topics: tuple[Topic, ...],
    messages: list[Message],
) -> ClassificationResult:
    """Classify messages with Claude in configured batches and return matches with usage."""
    if Anthropic is None:
        raise ClassificationError("Missing dependency: install requirements in .venv first.")

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ClassificationError("ANTHROPIC_API_KEY is not set. Add it to .env.")

    client = Anthropic(
        api_key=api_key,
        timeout=config.timeout_seconds,
        max_retries=config.max_retries,
    )

    accumulator = UsageAccumulator()
    all_matches: list[Match] = []
    batches = chunks(messages, batch_size)
    LOGGER.info(
        "Claude classification starting: model=%s batches=%s messages=%s", config.model, len(batches), len(messages)
    )
    for batch_index, batch in enumerate(batches, start=1):
        LOGGER.info(
            "Classifying batch %s/%s: messages=%s pk_range=%s-%s time_range=%s to %s",
            batch_index,
            len(batches),
            len(batch),
            batch[0].message_pk,
            batch[-1].message_pk,
            batch[0].local_time,
            batch[-1].local_time,
        )
        try:
            batch_matches = classify_batch(client, config, topics, batch, accumulator)
        except ClassificationError as exc:
            raise ClassificationError(
                f"Batch {batch_index}/{len(batches)} failed "
                f"(messages={len(batch)} pk_range={batch[0].message_pk}-{batch[-1].message_pk}): {exc}"
            ) from exc
        LOGGER.info("Batch %s/%s complete: matches=%s", batch_index, len(batches), len(batch_matches))
        all_matches.extend(batch_matches)
    return ClassificationResult(matches=tuple(all_matches), usage=accumulator)


def classify_batch(
    client: Any,
    config: LlmConfig,
    topics: tuple[Topic, ...],
    batch: list[Message],
    accumulator: UsageAccumulator,
) -> list[Match]:
    """Send one message batch to Claude and validate the sparse match response."""
    max_tokens = config.max_tokens
    retry_max_tokens = config.retry_max_tokens

    payload = {
        "topics": [asdict(topic) for topic in topics],
        "valid_message_pks": [message.message_pk for message in batch],
        "messages": [asdict(message) for message in batch],
    }

    kwargs = {
        "model": config.model,
        "max_tokens": max_tokens,
        "temperature": config.temperature,
        "system": system_prompt(),
        "messages": [
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            }
        ],
    }

    if config.use_output_config:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": matches_schema()}}

    response = None
    try:
        for attempt_index, attempt_max_tokens in enumerate([max_tokens, retry_max_tokens], start=1):
            kwargs["max_tokens"] = attempt_max_tokens
            response = create_message_with_optional_output_config(client, kwargs)
            if response_stop_reason(response) != "max_tokens":
                break
            if attempt_max_tokens >= retry_max_tokens:
                raise ClassificationError(
                    "Anthropic response hit max_tokens before producing complete JSON "
                    f"(max_tokens={attempt_max_tokens}). Increase llm.retry_max_tokens or lower whatsapp.batch_size."
                )
            LOGGER.warning(
                "Anthropic response hit max_tokens on attempt %s; retrying with max_tokens=%s.",
                attempt_index,
                retry_max_tokens,
            )
    except (APIConnectionError, APITimeoutError) as exc:
        raise ClassificationError(f"Anthropic connection failure after retries: {exc}") from exc
    except APIStatusError as exc:
        status_code = getattr(exc, "status_code", "unknown")
        request_id = getattr(exc, "request_id", None)
        suffix = f" request_id={request_id}" if request_id else ""
        raise ClassificationError(f"Anthropic API status error {status_code}.{suffix} {exc}") from exc

    if response is None:
        raise ClassificationError("Anthropic returned no response.")

    accumulator.add(getattr(response, "usage", None))

    parsed = getattr(response, "parsed_output", None)
    if parsed is None:
        parsed = parse_json_response(response)

    return validate_matches(parsed, {message.message_pk for message in batch}, {topic.id for topic in topics})


def create_message_with_optional_output_config(client: Any, kwargs: dict[str, Any]) -> Any:
    """Create an Anthropic message, falling back if the SDK lacks output_config support."""
    try:
        return client.messages.create(**kwargs)
    except TypeError as exc:
        if "output_config" not in str(exc):
            raise
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("output_config", None)
        return client.messages.create(**fallback_kwargs)


def response_stop_reason(response: Any) -> str | None:
    """Return Claude's stop reason when present."""
    value = getattr(response, "stop_reason", None)
    return str(value) if value is not None else None


def system_prompt() -> str:
    """Build the system instruction used for sparse topic classification."""
    return (
        "You classify WhatsApp group messages for topic-based user alerts. "
        "Return only messages that clearly match at least one configured topic. "
        "Do not score or return non-matching messages. "
        "If a message matches multiple topics, return only the first matching topic in the configured topic list. "
        "Use the exact message_pk and topic id values from the input. "
        "Never invent, approximate, or alter message_pk values; omit a match if the exact id is not present. "
        "Confidence must be a number from 0 to 1. "
        "Keep reason under 120 characters and notification under 100 characters. "
        "Return JSON with one top-level key named matches."
    )


def matches_schema() -> dict[str, Any]:
    """Return the JSON schema requested from Claude for structured match output."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "matches": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "message_pk": {"type": "integer"},
                        "topic_id": {"type": "string"},
                        "confidence": {"type": "number"},
                        "reason": {"type": "string"},
                        "notification": {"type": "string"},
                    },
                    "required": ["message_pk", "topic_id", "confidence", "reason", "notification"],
                },
            }
        },
        "required": ["matches"],
    }


def parse_json_response(response: Any) -> dict[str, Any]:
    """Parse a non-structured Claude response body as JSON."""
    text_parts = []
    for block in getattr(response, "content", []):
        text = getattr(block, "text", None)
        if text:
            text_parts.append(text)

    raw_text = "".join(text_parts).strip()
    if not raw_text:
        raise ClassificationError("Anthropic returned no text content.")
    if response_stop_reason(response) == "max_tokens":
        raise ClassificationError(
            "Anthropic response hit max_tokens before valid JSON could be parsed. "
            "Increase llm.max_tokens/llm.retry_max_tokens or lower whatsapp.batch_size."
        )

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        preview = raw_text[-500:] if len(raw_text) > 500 else raw_text
        raise ClassificationError(
            f"Could not parse Anthropic JSON response: {exc}. stop_reason={response_stop_reason(response)} "
            f"response_chars={len(raw_text)} tail={preview!r}"
        ) from exc


def validate_matches(parsed: Any, message_pks: set[int], topic_ids: set[str]) -> list[Match]:
    """Validate Claude matches and drop malformed or hallucinated individual matches."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get("matches"), list):
        raise ClassificationError("Anthropic response must be an object with a matches array.")

    valid_matches = []
    for item in parsed["matches"]:
        if not isinstance(item, dict):
            raise ClassificationError("Each match must be an object.")

        try:
            message_pk = int(item.get("message_pk"))
            confidence = float(item.get("confidence"))
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring malformed Anthropic match: %s", item)
            continue

        topic_id = str(item.get("topic_id", ""))
        reason = str(item.get("reason", "")).strip()
        notification = str(item.get("notification", "")).strip()

        if message_pk not in message_pks:
            LOGGER.warning("Ignoring Anthropic match with unknown message_pk: %s", message_pk)
            continue
        if topic_id not in topic_ids:
            LOGGER.warning("Ignoring Anthropic match with unknown topic_id: %s", topic_id)
            continue
        if not 0 <= confidence <= 1:
            LOGGER.warning("Ignoring Anthropic match with invalid confidence: %s", confidence)
            continue

        valid_matches.append(
            Match(
                message_pk=message_pk,
                topic_id=topic_id,
                confidence=confidence,
                reason=reason,
                notification=notification,
            )
        )
    return valid_matches


def chunks(items: list[Message], size: int) -> list[list[Message]]:
    """Split a list of messages into fixed-size batches."""
    return [items[index : index + size] for index in range(0, len(items), size)]
