#!/usr/bin/env python3
"""Rebuilds cairn-meter's history from Claude Code's own transcripts. stdlib only.

Walks `~/.claude/projects/*/*.jsonl` and parses every session not yet
recorded in `meter.db`. A session's project comes from the `cwd` its
transcript records, so no per-project folder name has to be decoded.
Idempotent; one unreadable file does not stop the rest. Reads transcripts
only: it writes nothing outside the store.

Usage:
    python3 backfill.py [projects_dir]
    from backfill import run; run(store_dir, claude_projects_dir)
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import parser  # noqa: E402

log = logging.getLogger(__name__)


def run(store_dir: Path | None = None, claude_projects_dir: Path | None = None) -> int:
    """Parse every unrecorded session transcript into the store; returns how
    many sessions were added."""
    store_dir = Path(store_dir) if store_dir else db.DEFAULT_STORE_DIR
    projects_dir = Path(claude_projects_dir) if claude_projects_dir else db.DEFAULT_CLAUDE_PROJECTS_DIR
    if not projects_dir.is_dir():
        return 0

    added = 0
    conn = db.connect(store_dir)
    try:
        for transcript_path in sorted(projects_dir.glob("*/*.jsonl")):
            session_id = transcript_path.stem
            try:
                if db.has_session(conn, session_id):
                    continue
                parser.parse_session(store_dir, transcript_path, session_id)
                added += 1
            except Exception:
                log.warning("backfill: skipping session %s", session_id, exc_info=True)
    finally:
        conn.close()
    return added


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    projects_dir = Path(argv[0]) if argv else None
    added = run(claude_projects_dir=projects_dir)
    print(f"cairn-meter backfill: {added} session(s) added")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
