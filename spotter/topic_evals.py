"""Run user-authored topic examples through the production classifier."""

from __future__ import annotations

from dataclasses import dataclass

from spotter.classifier import classify_messages
from spotter.config import AppConfig, Topic
from spotter.models import Match, Message
from spotter.usage import UsageAccumulator


@dataclass(frozen=True)
class TopicEvalExample:
    """One topic-specific binary expectation represented as a synthetic message."""

    message: Message
    topic_id: str
    expected_match: bool


def run_topic_evals(config: AppConfig, runs: int) -> int:
    """Run configured topic examples a fixed number of times and print stability results."""
    if runs <= 0:
        raise ValueError("runs must be a positive integer")

    examples = build_topic_eval_examples(config.topics)
    if not examples:
        print("No topic eval examples configured. Add positive_examples or negative_examples to a topic.")
        return 2

    pass_counts = [0] * len(examples)
    accumulator = UsageAccumulator()
    messages = [example.message for example in examples]

    for _run_index in range(runs):
        classification = classify_messages(config.llm, config.whatsapp.batch_size, config.topics, messages)
        accumulator.merge(classification.usage)
        for index, passed in enumerate(evaluate_topic_examples(examples, classification.matches)):
            pass_counts[index] += int(passed)

    print_topic_eval_results(config, examples, pass_counts, runs, accumulator)
    return 0 if all(pass_count == runs for pass_count in pass_counts) else 1


def build_topic_eval_examples(topics: tuple[Topic, ...]) -> list[TopicEvalExample]:
    """Convert configured text examples into classifier messages and expectations."""
    examples = []
    message_pk = 1
    for topic in topics:
        for expected_match, texts in (
            (True, topic.positive_examples),
            (False, topic.negative_examples),
        ):
            for text in texts:
                examples.append(
                    TopicEvalExample(
                        message=Message(
                            message_pk=message_pk,
                            group_name="Topic eval",
                            group_jid="topic-eval@g.us",
                            sender_name="Example sender",
                            sender_jid="example-sender@lid",
                            local_time="2026-01-01 00:00:00",
                            text=text,
                        ),
                        topic_id=topic.id,
                        expected_match=expected_match,
                    )
                )
                message_pk += 1
    return examples


def evaluate_topic_examples(examples: list[TopicEvalExample], matches: tuple[Match, ...]) -> list[bool]:
    """Return whether each example's associated topic expectation was met."""
    actual_keys = {(match.message_pk, match.topic_id) for match in matches}
    return [
        ((example.message.message_pk, example.topic_id) in actual_keys) == example.expected_match
        for example in examples
    ]


def print_topic_eval_results(
    config: AppConfig,
    examples: list[TopicEvalExample],
    pass_counts: list[int],
    runs: int,
    accumulator: UsageAccumulator,
) -> None:
    """Print per-example stability plus positive and negative summaries."""
    stable_count = sum(pass_count == runs for pass_count in pass_counts)
    positive_indexes = [index for index, example in enumerate(examples) if example.expected_match]
    negative_indexes = [index for index, example in enumerate(examples) if not example.expected_match]
    print(f"Topic evals: {stable_count}/{len(examples)} examples passed all {runs} run(s)")
    print(f"Positive examples: {stable_total(pass_counts, positive_indexes, runs)}/{len(positive_indexes)} stable")
    print(f"Negative examples: {stable_total(pass_counts, negative_indexes, runs)}/{len(negative_indexes)} stable")
    print(
        f"Model: {config.llm.model}\n"
        "Usage: "
        f"batches={accumulator.batches} input_tokens={accumulator.input_tokens} "
        f"output_tokens={accumulator.output_tokens}"
    )

    for example, pass_count in zip(examples, pass_counts, strict=True):
        status = "PASS" if pass_count == runs else "FAIL"
        polarity = "positive" if example.expected_match else "negative"
        print(f"\n[{status} {pass_count}/{runs}] {example.topic_id} {polarity}\n  {example.message.text}")


def stable_total(pass_counts: list[int], indexes: list[int], runs: int) -> int:
    """Count examples in one polarity that passed every requested run."""
    return sum(pass_counts[index] == runs for index in indexes)
