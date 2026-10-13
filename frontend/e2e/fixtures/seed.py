#!/usr/bin/env python3
"""Seeds the Playwright "populated" webServer's fixture store: a
`<scratch>/.claude/cairn-meter/meter.db` (via db.py's insert helpers) plus
one transcript `.jsonl` so server.py's on-demand transcript lookup resolves
"available" for one call. Two sessions - one with a saved label and a
usage-limit event (WarningBanner + drilldown coverage), one without a
label (short-id fallback) on an unpriced model (unknown-cost coverage).
Timestamps are relative to the run's current time, not hardcoded, since
server.py's range windows are wall-clock-relative.

Usage: python3 seed.py <scratch_dir>
`<scratch_dir>` is also this webServer's HOME env override, so the store
(`Path.home() / ".claude" / "cairn-meter"`) resolves under it and nothing
reads this machine's real data.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
import db  # noqa: E402

SESSION_MAIN = "e2e-session-main"
SESSION_OTHER = "e2e-session-other"
SESSION_MAIN_LABEL = "Add a login page to the app"
AVAILABLE_REQUEST_ID = "req-available-1"

def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed_db(store_dir: Path, project_root: Path, transcript_path: Path, now: datetime) -> None:
    conn = db.connect(store_dir)

    calls = [
        dict(
            request_id=AVAILABLE_REQUEST_ID,
            session_id=SESSION_MAIN,
            agent="main",
            model="claude-sonnet-5",
            timestamp=iso(now - timedelta(hours=2)),
            input_tokens=1200,
            output_tokens=600,
        ),
        dict(
            request_id="req-2",
            session_id=SESSION_MAIN,
            agent="builder",
            model="claude-sonnet-5",
            timestamp=iso(now - timedelta(hours=1, minutes=50)),
            input_tokens=800,
            output_tokens=1400,
            cache_read_tokens=200,
        ),
        dict(
            request_id="req-3",
            session_id=SESSION_MAIN,
            agent="reviewer",
            model="claude-opus-5",
            timestamp=iso(now - timedelta(hours=1, minutes=20)),
            input_tokens=400,
            output_tokens=300,
        ),
        # e2e-session-other's one call is on "claude-haiku-4.5", which isn't
        # a key in prices.json (only "claude-haiku-4-5-20251001" is priced)
        # - pricing.call_cost returns "unknown" for it, exercising the
        # info-dot affordance (goal 3) without needing a missing-price
        # backend fixture.
        dict(
            request_id="req-4",
            session_id=SESSION_OTHER,
            agent="main",
            model="claude-haiku-4.5",
            timestamp=iso(now - timedelta(days=2, hours=3)),
            input_tokens=300,
            output_tokens=150,
        ),
    ]
    for call in calls:
        db.insert_call(conn, call)

    tool_uses = [
        dict(
            tool_use_id="tu-1",
            request_id="req-2",
            session_id=SESSION_MAIN,
            agent="builder",
            tool_name="Bash",
            timestamp=iso(now - timedelta(hours=1, minutes=50)),
        ),
        dict(
            tool_use_id="tu-2",
            request_id="req-2",
            session_id=SESSION_MAIN,
            agent="builder",
            tool_name="Read",
            timestamp=iso(now - timedelta(hours=1, minutes=49)),
        ),
        dict(
            tool_use_id="tu-3",
            request_id="req-3",
            session_id=SESSION_MAIN,
            agent="reviewer",
            tool_name="Skill",
            detail="commit-msg-lint",
            timestamp=iso(now - timedelta(hours=1, minutes=19)),
        ),
    ]
    for tool_use in tool_uses:
        db.insert_tool_use(conn, **tool_use)

    # The usage-limit event this whole e2e addition exists to guard: the
    # WarningBanner it drives is where AlertTriangleIcon rendered
    # unconstrained (~800px) before the icon-sizing fix landed - no mockup
    # depicts a usage-limit event, so nothing but a real rendered check
    # (jsdom doesn't do real CSS layout) can catch a regression here.
    db.insert_usage_limit_event(
        conn,
        session_id=SESSION_MAIN,
        timestamp=iso(now - timedelta(hours=1, minutes=45)),
        raw_entry=json.dumps({"isApiErrorMessage": True}),
    )
    db.save_session_label(conn, session_id=SESSION_MAIN, label=SESSION_MAIN_LABEL)
    db.save_session(conn, session_id=SESSION_MAIN, project=str(project_root), transcript_path=str(transcript_path))
    db.save_session(conn, session_id=SESSION_OTHER, project=str(project_root))
    conn.commit()
    conn.close()


def seed_transcript(transcript_path: Path) -> None:
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    entries = [
        {"message": {"role": "user", "content": "Add a login page to the app."}},
        {
            "requestId": AVAILABLE_REQUEST_ID,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "tu-available-1", "name": "Read", "input": {"file_path": "src/login.py"}},
                    {"type": "text", "text": "Sure - adding a login page now."},
                ],
            },
        },
    ]
    with transcript_path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


def main() -> None:
    scratch = Path(sys.argv[1]).resolve()
    project_root = scratch / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    store_dir = scratch / ".claude" / "cairn-meter"
    transcript_path = scratch / ".claude" / "projects" / "-e2e-project" / f"{SESSION_MAIN}.jsonl"

    now = datetime.now(timezone.utc)
    seed_transcript(transcript_path)
    seed_db(store_dir, project_root, transcript_path, now)
    print(store_dir)


if __name__ == "__main__":
    main()
