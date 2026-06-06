from __future__ import annotations

from spotter.usage import UsageAccumulator
from tests.support import TestCase


class UsageTests(TestCase):
    def test_add_maps_openrouter_usage_fields(self):
        total = UsageAccumulator()

        total.add(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "prompt_tokens_details": {
                    "cache_write_tokens": 10,
                    "cached_tokens": 5,
                },
            }
        )

        self.assertEqual(
            UsageAccumulator(
                batches=1,
                input_tokens=100,
                output_tokens=20,
                cache_creation_input_tokens=10,
                cache_read_input_tokens=5,
            ),
            total,
        )

    def test_merge_adds_all_accumulated_usage(self):
        total = UsageAccumulator(batches=1, input_tokens=10, output_tokens=2)
        additional = UsageAccumulator(
            batches=2,
            input_tokens=20,
            output_tokens=4,
            cache_creation_input_tokens=3,
            cache_read_input_tokens=5,
        )

        total.merge(additional)

        self.assertEqual(
            UsageAccumulator(
                batches=3,
                input_tokens=30,
                output_tokens=6,
                cache_creation_input_tokens=3,
                cache_read_input_tokens=5,
            ),
            total,
        )
