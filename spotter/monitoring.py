"""Report successful scanner runs to external uptime monitoring."""

from __future__ import annotations

import urllib.error
import urllib.request

from spotter.errors import MonitoringError

DEAD_MANS_SNITCH_TIMEOUT_SECONDS = 15


def ping_dead_mans_snitch(url: str) -> None:
    """Send a successful-run GET request to a Dead Man's Snitch check-in URL."""
    if not url.startswith("https://"):
        raise MonitoringError("Dead Man's Snitch URL must use HTTPS.")

    try:
        request = urllib.request.Request(url, headers={"User-Agent": "spotter"}, method="GET")
        with urllib.request.urlopen(request, timeout=DEAD_MANS_SNITCH_TIMEOUT_SECONDS) as response:
            status_code = response.getcode()
    except urllib.error.HTTPError as exc:
        raise MonitoringError(f"Dead Man's Snitch returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MonitoringError(f"Dead Man's Snitch request failed: {reason}") from exc

    if status_code is not None and not 200 <= status_code < 300:
        raise MonitoringError(f"Dead Man's Snitch returned HTTP {status_code}.")
