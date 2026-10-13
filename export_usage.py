#!/usr/bin/env python3
"""Writes cairn-meter's `usage.json` export. stdlib only.

One file per project: `<project>/.cairn/export/usage.json`, the contract in
`docs/schemas/usage-v1.md`. The Stop hook calls `write_if_cairn`, which
writes only into a project that already has a `.cairn/` directory. An
explicit run (`python3 export_usage.py [project_dir]`) creates the folder.

Usage:
    python3 export_usage.py [--store DIR] [--report] [project_dir]
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import pricing  # noqa: E402

SCHEMA = 1
CURRENCY = "USD"
EXPORT_RELPATH = Path(".cairn") / "export" / "usage.json"

_SESSION_INT_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _project_keys(project: Path) -> list[str]:
    """The strings a session's recorded project may equal: the path as given
    (made absolute) and its symlink-resolved form."""
    absolute = os.path.abspath(project)
    return sorted({absolute, os.path.realpath(project)})


def build(store_dir: Path, project: Path, now: datetime | None = None) -> dict:
    """The usage document for `project`, from the store. A store with no
    database yet yields an empty session list."""
    project = Path(project)
    keys = _project_keys(project)
    sessions: list[dict] = []
    prices = pricing.current_prices()

    if db.db_path(store_dir).exists():
        conn = db.connect(store_dir)
        try:
            marks = ",".join("?" for _ in keys)
            rows = conn.execute(
                "SELECT c.* FROM calls c JOIN sessions s ON s.session_id = c.session_id "
                f"WHERE s.project IN ({marks}) ORDER BY c.session_id, c.timestamp, c.request_id",
                keys,
            ).fetchall()
            columns = [d[0] for d in conn.execute("SELECT * FROM calls LIMIT 0").description]
        finally:
            conn.close()
        by_session: dict[str, list[dict]] = {}
        for row in rows:
            record = dict(zip(columns, row))
            by_session.setdefault(record["session_id"], []).append(record)
        for session_id, calls in by_session.items():
            sessions.append(_session_entry(session_id, calls, prices))
        sessions.sort(key=lambda s: (s["started"], s["session_id"]))

    return {
        "schema": SCHEMA,
        "generated": _now_iso(now),
        "project": os.path.abspath(project),
        "currency": CURRENCY,
        "prices_as_of": pricing.prices_as_of(),
        "sessions": sessions,
    }


def _session_entry(session_id: str, calls: list[dict], prices: dict) -> dict:
    timestamps = [c["timestamp"] for c in calls]
    unpriced = sorted({c["model"] for c in calls if pricing.call_cost(c, prices=prices) == pricing.UNKNOWN})
    cost = pricing.group_cost(calls, prices=prices)
    return {
        "session_id": session_id,
        "started": min(timestamps),
        "ended": max(timestamps),
        "models": sorted({c["model"] for c in calls}),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "cache_read_tokens": sum(c["cache_read_tokens"] for c in calls),
        "cache_write_tokens": sum(c["cache_write_5m_tokens"] + c["cache_write_1h_tokens"] for c in calls),
        "cost_usd": None if cost is None else round(cost, 6),
        "unpriced_models": unpriced,
    }


def validate(doc) -> list[str]:
    """Problems found in a usage document, empty when it satisfies schema 1.
    The checks mirror `docs/schemas/usage-v1.md`."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["document is not an object"]
    if doc.get("schema") != SCHEMA or isinstance(doc.get("schema"), bool):
        problems.append("schema must be 1")
    if not _is_utc_iso(doc.get("generated")):
        problems.append("generated must be a UTC ISO timestamp")
    project = doc.get("project")
    if not isinstance(project, str) or not os.path.isabs(project):
        problems.append("project must be an absolute path")
    if doc.get("currency") != CURRENCY:
        problems.append('currency must be "USD"')
    as_of = doc.get("prices_as_of")
    if as_of is not None and not isinstance(as_of, str):
        problems.append("prices_as_of must be a string or null")
    sessions = doc.get("sessions")
    if not isinstance(sessions, list):
        return problems + ["sessions must be a list"]
    for i, s in enumerate(sessions):
        where = f"sessions[{i}]"
        if not isinstance(s, dict):
            problems.append(f"{where} is not an object")
            continue
        if not isinstance(s.get("session_id"), str) or not s.get("session_id"):
            problems.append(f"{where}.session_id must be a non-empty string")
        for key in ("started", "ended"):
            if not _is_utc_iso(s.get(key)):
                problems.append(f"{where}.{key} must be a UTC ISO timestamp")
        for key in ("models", "unpriced_models"):
            value = s.get(key)
            if not isinstance(value, list) or not all(isinstance(m, str) for m in value):
                problems.append(f"{where}.{key} must be a list of strings")
        for key in _SESSION_INT_FIELDS:
            value = s.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                problems.append(f"{where}.{key} must be a non-negative integer")
        cost = s.get("cost_usd")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0):
            problems.append(f"{where}.cost_usd must be a non-negative number or null")
        unpriced = s.get("unpriced_models")
        if isinstance(unpriced, list) and (cost is None) != bool(unpriced):
            problems.append(f"{where}.cost_usd is null exactly when unpriced_models is not empty")
    return problems


def _is_utc_iso(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return value.endswith("Z") and parsed.tzinfo is not None


def _write_atomic(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".usage.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write(store_dir: Path, project: Path, now: datetime | None = None) -> Path:
    """Write `<project>/.cairn/export/usage.json`, creating `.cairn/` (with a
    `.gitignore` that ignores it) when it is not there yet."""
    project = Path(project)
    cairn = project / ".cairn"
    fresh = not cairn.exists()
    path = project / EXPORT_RELPATH
    _write_atomic(path, json.dumps(build(store_dir, project, now), indent=2) + "\n")
    if fresh and not (cairn / ".gitignore").exists():
        (cairn / ".gitignore").write_text("*\n")
    return path


def write_if_cairn(store_dir: Path, project: Path, now: datetime | None = None) -> Path | None:
    """The Stop hook's entry: writes only when `<project>/.cairn/` already
    exists, so a project that never adopted cairn gets no new directory."""
    if not (Path(project) / ".cairn").is_dir():
        return None
    return write(store_dir, project, now)


def report(doc: dict) -> str:
    """A printable per-session table of a usage document."""
    lines = [f"cairn-meter usage for {doc['project']} (prices as of {doc['prices_as_of'] or 'unknown'})"]
    if not doc["sessions"]:
        lines.append("no sessions recorded for this project yet")
        return "\n".join(lines)
    lines.append(f"{'session':<10} {'started':<20} {'input':>10} {'output':>10} {'cache r':>11} {'cache w':>11} {'cost':>10}")
    total = 0.0
    unknown = False
    for s in doc["sessions"]:
        if s["cost_usd"] is None:
            cost = "unknown"
            unknown = True
        else:
            cost = f"${s['cost_usd']:.2f}"
            total += s["cost_usd"]
        lines.append(
            f"{s['session_id'][:8]:<10} {s['started']:<20} {s['input_tokens']:>10} {s['output_tokens']:>10} "
            f"{s['cache_read_tokens']:>11} {s['cache_write_tokens']:>11} {cost:>10}"
        )
    suffix = " (excludes sessions with unpriced models)" if unknown else ""
    lines.append(f"total priced cost: ${total:.2f}{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    store = db.DEFAULT_STORE_DIR
    only_report = False
    positional: list[str] = []
    while argv:
        arg = argv.pop(0)
        if arg == "--store" and argv:
            store = Path(argv.pop(0))
        elif arg == "--report":
            only_report = True
        else:
            positional.append(arg)
    project = Path(positional[0]) if positional else Path.cwd()
    if only_report:
        print(report(build(store, project)))
        return 0
    path = write(store, project)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
