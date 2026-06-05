#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CASES_PATH = Path(__file__).with_name("cases.jsonl")
sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class ExpectedMatch:
    message_pk: int
    topic_id: str


@dataclass(frozen=True)
class EvalMessage:
    message_pk: int
    group_name: str
    group_jid: str
    sender_name: str
    sender_jid: str | None
    local_time: str
    text: str


@dataclass(frozen=True)
class EvalCase:
    id: str
    name: str
    messages: list[EvalMessage]
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
    raw_matches: list[dict[str, Any]]
    alerts: list[dict[str, Any]]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run manual classifier evals against the live configured Anthropic model."
    )
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH, help="Path to JSONL eval cases.")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config.json", help="Path to spotter config JSON.")
    parser.add_argument("--env", type=Path, default=REPO_ROOT / ".env", help="Path to the .env file with secrets.")
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

    spotter_cli = load_spotter_cli()
    config_path = spotter_cli.resolve_config_path(args.config)
    spotter_cli.load_env_file(args.env.expanduser().resolve())
    config = spotter_cli.load_config(config_path)
    model, temperature = llm_settings(spotter_cli, config)
    if temperature not in (0, None):
        print(
            "Refusing to run classifier evals with non-zero llm.temperature. "
            f"Set llm.temperature to 0 or null in {config_path} for reproducible manual evals.",
            file=sys.stderr,
        )
        return 2
    validate_case_topics(cases, {topic.id for topic in spotter_cli.get_topics(config)})

    accumulator = spotter_cli.UsageAccumulator()
    results = [run_case(spotter_cli, config, accumulator, case) for case in cases]
    print_results(results, accumulator, model=model, temperature=temperature, verbose=args.verbose)
    return 0 if all(result.passed for result in results) else 1


def load_spotter_cli() -> Any:
    module_path = REPO_ROOT / "spotter.py"
    spec = importlib.util.spec_from_file_location("spotter_cli_for_evals", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def parse_case(data: dict[str, Any]) -> EvalCase:
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


def parse_message(data: dict[str, Any]) -> EvalMessage:
    return EvalMessage(
        message_pk=int(data["message_pk"]),
        group_name=required_str(data, "group_name"),
        group_jid=required_str(data, "group_jid"),
        sender_name=required_str(data, "sender_name"),
        sender_jid=optional_str(data.get("sender_jid")),
        local_time=required_str(data, "local_time"),
        text=required_str(data, "text"),
    )


def parse_expected_match(data: dict[str, Any]) -> ExpectedMatch:
    return ExpectedMatch(message_pk=int(data["message_pk"]), topic_id=required_str(data, "topic_id"))


def required_str(data: dict[str, Any], key: str) -> str:
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def optional_str(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("optional string fields must be non-empty when present")
    return value.strip()


def require_list(data: dict[str, Any], key: str) -> list[Any]:
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


def llm_settings(spotter_cli: Any, config: dict[str, Any]) -> tuple[str, float | None]:
    llm_config = config.get("llm", {})
    default_model = getattr(spotter_cli, "DEFAULT_MODEL", "claude-sonnet-4-6")
    temperature = llm_config.get("temperature", 0)
    return str(llm_config.get("model", default_model)), None if temperature is None else float(temperature)


def run_case(spotter_cli: Any, config: dict[str, Any], accumulator: Any, case: EvalCase) -> CaseResult:
    messages = [spotter_cli.Message(**message.__dict__) for message in case.messages]
    raw_matches = spotter_cli.classify_messages(config, messages, accumulator)
    alerts = spotter_cli.build_alerts(config, messages, raw_matches, existing_alert_keys=set())
    failures = evaluate_case(case, alerts)
    return CaseResult(case=case, passed=not failures, failures=failures, raw_matches=raw_matches, alerts=alerts)


def evaluate_case(case: EvalCase, alerts: list[dict[str, Any]]) -> list[str]:
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


def match_key(match: dict[str, Any]) -> tuple[int, str]:
    return int(match["message_pk"]), str(match["topic_id"])


def expected_key(expected: ExpectedMatch) -> tuple[int, str]:
    return expected.message_pk, expected.topic_id


def format_keys(keys: set[tuple[int, str]]) -> str:
    return ", ".join(f"{message_pk}/{topic_id}" for message_pk, topic_id in sorted(keys))


def print_results(
    results: list[CaseResult], accumulator: Any, *, model: str, temperature: float | None, verbose: bool
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


def compact_match(match: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_pk": match.get("message_pk"),
        "topic_id": match.get("topic_id"),
        "confidence": match.get("confidence"),
        "reason": match.get("reason"),
        "notification": match.get("notification"),
    }


def list_cases(cases: list[EvalCase]) -> None:
    for case in cases:
        suffix = f" ({case.failure_type})" if case.failure_type else ""
        print(f"{case.id}: {case.name}{suffix}")


if __name__ == "__main__":
    raise SystemExit(main())
