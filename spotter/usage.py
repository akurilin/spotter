"""Per-run token and message usage records for cost analytics.

Writes one JSON Lines row per scanner run, capturing token counts (split by
type so USD can be derived from a pricing table at query time), message
counts, model, and outcome. Token type names mirror Anthropic's
``response.usage`` fields verbatim.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass
class UsageAccumulator:
    """Sum token usage and batch counts across Anthropic responses in one run."""

    batches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def add(self, usage: Any) -> None:
        """Add tokens from one ``response.usage`` object returned by the Anthropic SDK."""
        self.batches += 1
        self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
        self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        self.cache_creation_input_tokens += int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        self.cache_read_input_tokens += int(getattr(usage, "cache_read_input_tokens", 0) or 0)

    def merge(self, other: UsageAccumulator) -> None:
        """Add another completed accumulator into this one."""
        for field in fields(self):
            setattr(self, field.name, getattr(self, field.name) + getattr(other, field.name))


@dataclass(frozen=True)
class UsageRecord:
    """One scanner run's usage and outcome, ready for JSONL serialization."""

    run_id: str
    started_at: str
    completed_at: str
    model: str
    dry_run: bool
    status: str
    messages: int
    batches: int
    alerts: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int


def new_run_id() -> str:
    """Return a fresh UUID identifying one scanner run."""
    return str(uuid.uuid4())


def write_usage_record(path: Path, record: UsageRecord) -> None:
    """Append a single usage record as a JSON line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(record), ensure_ascii=False, sort_keys=True))
        handle.write("\n")
