#!/usr/bin/env python3
"""Once-a-day pricing-drift check for cairn-meter. stdlib only.

Finds every model recorded in `meter.db` that has no entry in
`prices.json`, and names them on stderr. Detection only: it never edits
`prices.json` and never derives a rate.

Throttled to one scan per UTC calendar day by a small state file next to
the store (`{"last_checked": "YYYY-MM-DD"}`).

Usage:
    from pricing_check import maybe_run
    maybe_run(store_dir, state_path)
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import pricing  # noqa: E402

log = logging.getLogger(__name__)

STATE_FILENAME = "pricing-check-state.json"


def today_utc() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def unpriced_models(store_dir: Path, prices: dict | None = None) -> set[str]:
    """Models in the store that are not keys of `prices`. A store with no
    database yet reports none; a database error is logged, not raised.
    """
    prices = pricing.current_prices() if prices is None else prices
    if not db.db_path(store_dir).exists():
        return set()
    try:
        conn = db.connect(store_dir)
        try:
            rows = conn.execute("SELECT DISTINCT model FROM calls").fetchall()
        finally:
            conn.close()
    except Exception:
        log.warning("pricing_check: could not read %s", store_dir, exc_info=True)
        return set()
    return {model for (model,) in rows if model not in prices}


def _last_checked(state_path: Path) -> str | None:
    try:
        return json.loads(state_path.read_text()).get("last_checked")
    except (OSError, ValueError, AttributeError):
        return None


def _write_state(state_path: Path, today: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"last_checked": today}))
    os.replace(tmp, state_path)


def maybe_run(store_dir: Path, state_path: Path | None = None, today: str | None = None) -> None:
    """Scan at most once per UTC day; warn on stderr naming any unpriced model."""
    store_dir = Path(store_dir)
    state_path = Path(state_path) if state_path else store_dir / STATE_FILENAME
    today = today or today_utc()
    if _last_checked(state_path) == today:
        return
    missing = unpriced_models(store_dir)
    _write_state(state_path, today)
    if missing:
        print(
            "cairn-meter: models missing from prices.json (cost shows unknown): "
            + ", ".join(sorted(missing))
            + " - add their rates to prices.json",
            file=sys.stderr,
        )
