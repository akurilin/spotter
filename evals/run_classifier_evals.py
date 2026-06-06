#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = Path(__file__).with_name("cases.jsonl")

# This file is run directly, so the repository root is not otherwise importable.
sys.path.insert(0, str(REPO_ROOT))

from spotter.alerts import build_alerts  # noqa: E402
from spotter.classifier import classify_messages  # noqa: E402
from spotter.config import AppConfig, load_config, load_env_file  # noqa: E402
from spotter.models import Alert, Match, Message  # noqa: E402
from spotter.usage import UsageAccumulator  # noqa: E402


@dataclass(frozen=True)
class ExpectedMatch:
    message_pk: int
    topic_id: str


@dataclass(frozen=True)
class EvalCase:
    id: str
    name: str
    messages: list[Message]
    expected_matches: list[ExpectedMatch]
    expected_non_matches: list[ExpectedMatch]
    allow_extra_matches: bool
    failure_type: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class CaseResult:
    case: EvalCase
    passed: bool
    failures: list[str]
    raw_matches: tuple[Match, ...]
    alerts: list[Alert]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run manual classifier evals against the live configured OpenRouter model."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="Path to JSONL eval cases.")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json", help="Path to spotter config JSON.")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env", help="Path to the .env file with secrets.")
    parser.add_argument("--model", help="Override llm.model for this eval run.")
    parser.add_argument(
        "--omit-temperature",
        action="store_true",
        help="Omit the temperature parameter for models that reject it.",
    )
    parser.add_argument("--case", action="append", dest="case_ids", help="Run only this case id. Can be repeated.")
    parser.add_argument("--list", action="store_true", help="List cases without calling the model.")
    parser.add_argument("--live", action="store_true", help="Actually call the configured model.")
    parser.add_argument("--allow-ci", action="store_true", help="Permit live evals when CI is set.")
    parser.add_argument("--verbose", action="store_true", help="Print raw model matches and thresholded alerts.")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.case_ids:
        requested_ids = set(args.case_ids)
        cases = [case for case in cases if case.id in requested_ids]
        missing_ids = requested_ids - {case.id for case in cases}
        if missing_ids:
            print(f"Unknown case id(s): {', '.join(sorted(missing_ids))}", file=sys.stderr)
            return 2

    if args.list:
        list_cases(cases)
        return 0

    if not args.live:
        print("Refusing to call the live model without --live.")
        print(f"Cases loaded: {len(cases)}")
        print("Use --list to inspect cases, or run with --live for a paid model-backed eval.")
        return 2

    if os.environ.get("CI") and not args.allow_ci:
        print("Refusing to run live classifier evals in CI without --allow-ci.", file=sys.stderr)
        return 2

    config_path = args.config.expanduser().resolve()
    load_env_file(args.env.expanduser().resolve())
    config = load_config(config_path)
    llm_updates = {}
    if args.model:
        llm_updates["model"] = args.model
    if args.omit_temperature:
        llm_updates["temperature"] = None
    if llm_updates:
        config = config.model_copy(update={"llm": config.llm.model_copy(update=llm_updates)})
    model = config.llm.model
    temperature = config.llm.temperature
    if temperature not in (0, None):
        print(
            "Refusing to run classifier evals with non-zero llm.temperature. "
            f"Set llm.temperature to 0 or null in {config_path} for reproducible manual evals.",
            file=sys.stderr,
        )
        return 2
    validate_case_topics(cases, {topic.id for topic in config.topics})

    accumulator = UsageAccumulator()
    results = [run_case(config, accumulator, case) for case in cases]
    print_results(results, accumulator, model=model, temperature=temperature, verbose=args.verbose)
    if accumulator.input_tokens <= 0 or accumulator.output_tokens <= 0:
        print(
            "Live eval failed: OpenRouter response usage did not include positive input/output token counts.",
            file=sys.stderr,
        )
        return 1
    return 0 if all(result.passed for result in results) else 1


def load_cases(path: Path) -> list[EvalCase]:
    cases = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                cases.append(parse_case(json.loads(line)))
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Invalid eval case at {path}:{line_number}: {exc}") from exc
    return cases


def parse_case(data: dict) -> EvalCase:
    expected_matches = [parse_expected_match(item) for item in data.get("expected_matches", [])]
    expected_non_matches = [parse_expected_match(item) for item in data.get("expected_non_matches", [])]
    if not expected_matches and not expected_non_matches and not bool(data.get("allow_extra_matches", False)):
        raise ValueError("case must define expected_matches or expected_non_matches")

    return EvalCase(
        id=required_str(data, "id"),
        name=required_str(data, "name"),
        failure_type=optional_str(data.get("failure_type")),
        notes=optional_str(data.get("notes")),
        messages=[parse_message(item) for item in require_list(data, "messages")],
        expected_matches=expected_matches,
        expected_non_matches=expected_non_matches,
        allow_extra_matches=bool(data.get("allow_extra_matches", False)),
    )


def parse_message(data: dict) -> Message:
    return Message(
        message_pk=int(data["message_pk"]),
        group_name=required_str(data, "group_name"),
        group_jid=required_str(data, "group_jid"),
        sender_name=required_str(data, "sender_name"),
        sender_jid=optional_str(data.get("sender_jid")),
        local_time=required_str(data, "local_time"),
        text=required_str(data, "text"),
    )


def parse_expected_match(data: dict) -> ExpectedMatch:
    return ExpectedMatch(message_pk=int(data["message_pk"]), topic_id=required_str(data, "topic_id"))


def required_str(data: dict, key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional string fields must be non-empty when present")
    return value.strip()


def require_list(data: dict, key: str) -> list:
    value = data[key]
    if not isinstance(value, list) or not value:
        raise ValueError(f"{key} must be a non-empty list")
    return value


def validate_case_topics(cases: list[EvalCase], topic_ids: set[str]) -> None:
    referenced_topic_ids = {
        expected.topic_id for case in cases for expected in [*case.expected_matches, *case.expected_non_matches]
    }
    unknown_topic_ids = referenced_topic_ids - topic_ids
    if unknown_topic_ids:
        raise ValueError(
            "Eval cases reference topic ids that are not in the selected config: "
            + ", ".join(sorted(unknown_topic_ids))
        )


def run_case(config: AppConfig, accumulator: UsageAccumulator, case: EvalCase) -> CaseResult:
    classification = classify_messages(config.llm, config.whatsapp.batch_size, config.topics, case.messages)
    accumulator.merge(classification.usage)
    raw_matches = classification.matches
    alerts = build_alerts(
        config.topics,
        case.messages,
        classification.matches,
        existing_alert_keys=set(),
        created_at="eval",
    )
    failures = evaluate_case(case, alerts)
    return CaseResult(case=case, passed=not failures, failures=failures, raw_matches=raw_matches, alerts=alerts)


def evaluate_case(case: EvalCase, alerts: list[Alert]) -> list[str]:
    actual_keys = {match_key(alert) for alert in alerts}
    expected_keys = {expected_key(expected) for expected in case.expected_matches}
    expected_non_keys = {expected_key(expected) for expected in case.expected_non_matches}
    failures = []

    missing_keys = expected_keys - actual_keys
    if missing_keys:
        failures.append("missing expected match(es): " + format_keys(missing_keys))

    unexpected_keys = expected_non_keys & actual_keys
    if unexpected_keys:
        failures.append("returned expected non-match(es): " + format_keys(unexpected_keys))

    if not case.allow_extra_matches:
        extra_keys = actual_keys - expected_keys - expected_non_keys
        if extra_keys:
            failures.append("returned extra match(es): " + format_keys(extra_keys))

    return failures


def match_key(match: Alert) -> tuple[int, str]:
    return match.message_pk, match.topic_id


def expected_key(expected: ExpectedMatch) -> tuple[int, str]:
    return expected.message_pk, expected.topic_id


def format_keys(keys: set[tuple[int, str]]) -> str:
    return ", ".join(f"{message_pk}/{topic_id}" for message_pk, topic_id in sorted(keys))


def print_results(
    results: list[CaseResult], accumulator: UsageAccumulator, *, model: str, temperature: float | None, verbose: bool
) -> None:
    passed_count = sum(result.passed for result in results)
    failed_count = len(results) - passed_count
    temperature_text = "omitted" if temperature is None else f"{temperature:g}"
    print(f"Classifier evals: {passed_count} passed, {failed_count} failed")
    print(
        f"Model: {model} temperature={temperature_text}\n"
        "Usage: "
        f"batches={accumulator.batches} input_tokens={accumulator.input_tokens} "
        f"output_tokens={accumulator.output_tokens}"
    )

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"\n[{status}] {result.case.id}: {result.case.name}")
        for failure in result.failures:
            print(f"  - {failure}")
        if verbose:
            print("  raw_matches:")
            for match in result.raw_matches:
                print("    " + json.dumps(compact_match(match), ensure_ascii=False, sort_keys=True))
            print("  thresholded_alerts:")
            for alert in result.alerts:
                print("    " + json.dumps(compact_match(alert), ensure_ascii=False, sort_keys=True))


def compact_match(match: Match | Alert) -> dict:
    return {
        "message_pk": match.message_pk,
        "topic_id": match.topic_id,
        "confidence": match.confidence,
        "reason": match.reason,
        "notification": match.notification,
    }


def list_cases(cases: list[EvalCase]) -> None:
    for case in cases:
        suffix = f" ({case.failure_type})" if case.failure_type else ""
        print(f"{case.id}: {case.name}{suffix}")


if __name__ == "__main__":
    raise SystemExit(main())
