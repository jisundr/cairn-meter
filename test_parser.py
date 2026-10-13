import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import parser  # noqa: E402


def write_jsonl(path: Path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for line in lines:
            if isinstance(line, str):
                f.write(line + "\n")
            else:
                f.write(json.dumps(line) + "\n")


def make_usage(**overrides):
    usage = dict(
        input_tokens=10,
        output_tokens=20,
        cache_read_input_tokens=5,
        cache_creation={"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 2},
    )
    usage.update(overrides)
    return usage


def make_call_entry(request_id, tool_use_id=None, tool_name="Read",
                     timestamp="2026-08-28T00:00:00Z", model="claude-sonnet-5",
                     **usage_overrides):
    content = []
    if tool_use_id:
        content.append({"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": {}})
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "model": model,
            "usage": make_usage(**usage_overrides),
            "content": content,
        },
    }


def make_dispatch_entry(tool_use_id, subagent_type, request_id="req-dispatch"):
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": "2026-08-28T00:00:00Z",
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-5",
            "usage": make_usage(),
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_use_id,
                    "name": "Task",
                    "input": {"subagent_type": subagent_type, "description": "do work"},
                }
            ],
        },
    }


def make_dispatch_result_entry(tool_use_id, agent_id):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": f"Dispatched. agentId: {agent_id}"}],
                }
            ],
        },
    }


def test_build_agent_map_maps_subagent_type_to_agent_id():
    main_entries = [
        make_dispatch_entry("toolu_1", "builder"),
        make_dispatch_result_entry("toolu_1", "abc123def"),
    ]
    agent_map = parser.build_agent_map(main_entries)
    assert agent_map == {"abc123def": "builder"}


def test_build_agent_map_skips_unresolved_dispatch():
    main_entries = [make_dispatch_entry("toolu_1", "builder")]
    agent_map = parser.build_agent_map(main_entries)
    assert agent_map == {}


def test_parse_session_attributes_main_and_subagent_entries(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_dispatch_entry("toolu_dispatch", "builder"),
        make_dispatch_result_entry("toolu_dispatch", "abc123"),
        make_call_entry("req-main", tool_use_id="toolu_main", tool_name="Read"),
    ])
    write_jsonl(tmp_path / "session" / "subagents" / "agent-abc123.jsonl", [
        make_call_entry("req-sub", tool_use_id="toolu_sub", tool_name="Write"),
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    calls = {row[0]: row[1] for row in conn.execute("SELECT request_id, agent FROM calls")}
    tool_uses = {row[0]: row[1] for row in conn.execute("SELECT tool_use_id, agent FROM tool_uses")}

    assert calls["req-main"] == "main"
    assert calls["req-sub"] == "builder"
    assert tool_uses["toolu_main"] == "main"
    assert tool_uses["toolu_sub"] == "builder"


def test_parse_session_tags_unmatched_subagent_as_unknown(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-main", tool_use_id="toolu_main"),
    ])
    write_jsonl(tmp_path / "session" / "subagents" / "agent-zzz999.jsonl", [
        make_call_entry("req-sub", tool_use_id="toolu_sub"),
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    agent = conn.execute("SELECT agent FROM calls WHERE request_id = 'req-sub'").fetchone()[0]
    assert agent == "unknown"


def test_parse_session_dedupes_duplicate_request_id(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-dup", tool_use_id="toolu_1"),
        make_call_entry("req-dup", tool_use_id="toolu_2", output_tokens=999),
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    rows = conn.execute("SELECT output_tokens FROM calls WHERE request_id = 'req-dup'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 20


def test_parse_session_dedupes_duplicate_tool_use_id(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-1", tool_use_id="toolu_dup", tool_name="Read"),
        make_call_entry("req-2", tool_use_id="toolu_dup", tool_name="Write"),
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    rows = conn.execute("SELECT tool_name FROM tool_uses WHERE tool_use_id = 'toolu_dup'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "Read"


def test_genuine_usage_limit_string_message_routes_to_usage_limit_events(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        {
            "isApiErrorMessage": True,
            "timestamp": "2026-08-28T00:00:00Z",
            "message": "Claude usage limit reached|1735689600",
        },
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    events = conn.execute("SELECT session_id FROM usage_limit_events").fetchall()
    assert calls == 0
    assert events == [("sess-1",)]


def test_generic_api_error_message_does_not_route_to_usage_limit_events(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        {
            "isApiErrorMessage": True,
            "timestamp": "2026-08-28T00:00:00Z",
            "message": {
                "model": "<synthetic>",
                "role": "assistant",
                "content": [{"type": "text", "text": "error occurred"}],
            },
            "error": "server_error",
        },
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    events = conn.execute("SELECT session_id FROM usage_limit_events").fetchall()
    assert calls == 0
    assert events == []


def test_malformed_json_line_does_not_block_surrounding_entries(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-1", tool_use_id="toolu_1"),
        "{not valid json",
        make_call_entry("req-2", tool_use_id="toolu_2"),
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    request_ids = {row[0] for row in conn.execute("SELECT request_id FROM calls")}
    assert request_ids == {"req-1", "req-2"}


def test_extract_ai_title_keeps_last_non_empty_occurrence():
    entries = [
        {"type": "ai-title", "aiTitle": "First draft title"},
        make_call_entry("req-1", tool_use_id="toolu_1"),
        {"type": "ai-title", "aiTitle": "Revised title"},
    ]
    assert parser._extract_ai_title(entries) == "Revised title"


def test_extract_ai_title_returns_none_when_absent():
    entries = [make_call_entry("req-1", tool_use_id="toolu_1")]
    assert parser._extract_ai_title(entries) is None


def test_extract_ai_title_ignores_a_later_empty_title():
    entries = [
        {"type": "ai-title", "aiTitle": "Keep this one"},
        {"type": "ai-title", "aiTitle": ""},
    ]
    assert parser._extract_ai_title(entries) == "Keep this one"


def test_parse_session_returns_last_ai_title_and_saves_it(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        {"type": "ai-title", "aiTitle": "First draft title"},
        make_call_entry("req-1", tool_use_id="toolu_1"),
        {"type": "ai-title", "aiTitle": "Revised title"},
    ])

    title = parser.parse_session(store_dir, transcript_path, "sess-1")
    assert title == "Revised title"

    conn = db.connect(store_dir)
    row = conn.execute("SELECT label FROM session_labels WHERE session_id = 'sess-1'").fetchone()
    assert row == ("Revised title",)


def test_parse_session_returns_none_and_skips_save_when_no_title_found(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-1", tool_use_id="toolu_1"),
    ])

    title = parser.parse_session(store_dir, transcript_path, "sess-1")
    assert title is None

    conn = db.connect(store_dir)
    row = conn.execute("SELECT label FROM session_labels WHERE session_id = 'sess-1'").fetchone()
    assert row is None


def test_parse_session_rerun_without_a_title_does_not_wipe_a_previously_saved_one(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        {"type": "ai-title", "aiTitle": "Saved on first pass"},
        make_call_entry("req-1", tool_use_id="toolu_1"),
    ])
    parser.parse_session(store_dir, transcript_path, "sess-1")

    # A later pass over a transcript that (for whatever reason) carries no
    # ai-title record this time must not clear the label already saved.
    write_jsonl(transcript_path, [
        make_call_entry("req-1", tool_use_id="toolu_1"),
    ])
    title = parser.parse_session(store_dir, transcript_path, "sess-1")
    assert title is None

    conn = db.connect(store_dir)
    row = conn.execute("SELECT label FROM session_labels WHERE session_id = 'sess-1'").fetchone()
    assert row == ("Saved on first pass",)


def test_parse_session_is_idempotent_on_rerun(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-1", tool_use_id="toolu_1"),
    ])

    parser.parse_session(store_dir, transcript_path, "sess-1")
    conn = db.connect(store_dir)
    first_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    first_tool_uses = conn.execute("SELECT COUNT(*) FROM tool_uses").fetchone()[0]

    parser.parse_session(store_dir, transcript_path, "sess-1")
    second_calls = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    second_tool_uses = conn.execute("SELECT COUNT(*) FROM tool_uses").fetchone()[0]

    assert first_calls == second_calls == 1
    assert first_tool_uses == second_tool_uses == 1


def test_parse_transcript_sentinels_a_missing_model_instead_of_crashing(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"

    write_jsonl(transcript_path, [
        make_call_entry("req-1", tool_use_id="toolu_1", model=None),
    ])

    # calls.model is NOT NULL - this must not raise sqlite3.IntegrityError.
    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    row = conn.execute(
        "SELECT model, input_tokens, output_tokens FROM calls WHERE request_id = 'req-1'"
    ).fetchone()
    assert row == (parser.UNKNOWN_MODEL, 10, 20)


def test_parse_transcript_sentinels_an_absent_model_key_instead_of_crashing(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"
    entry = make_call_entry("req-1", tool_use_id="toolu_1")
    del entry["message"]["model"]
    write_jsonl(transcript_path, [entry])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    model = conn.execute("SELECT model FROM calls WHERE request_id = 'req-1'").fetchone()[0]
    assert model == parser.UNKNOWN_MODEL



def test_parse_session_records_the_given_project_and_transcript(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "t" / "sess-1.jsonl"
    write_jsonl(transcript_path, [make_call_entry("req-1")])

    parser.parse_session(store_dir, transcript_path, "sess-1", project="/work/app")

    conn = db.connect(store_dir)
    row = conn.execute("SELECT project, transcript_path FROM sessions WHERE session_id = 'sess-1'").fetchone()
    assert row == ("/work/app", str(transcript_path))


def test_parse_session_without_a_project_reads_cwd_from_the_transcript(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "t" / "sess-1.jsonl"
    entry = make_call_entry("req-1")
    entry["cwd"] = "/work/from-transcript"
    write_jsonl(transcript_path, [entry])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    assert conn.execute("SELECT project FROM sessions").fetchone() == ("/work/from-transcript",)


def test_parse_session_with_no_project_anywhere_stores_null(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "t" / "sess-1.jsonl"
    write_jsonl(transcript_path, [make_call_entry("req-1")])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    assert conn.execute("SELECT project FROM sessions").fetchone() == (None,)
    assert conn.execute("SELECT COUNT(*) FROM calls").fetchone() == (1,)


def test_parse_session_falls_back_to_flat_cache_creation_total_as_5m_writes(tmp_path):
    import pricing

    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"
    entry = make_call_entry("req-flat")
    entry["message"]["usage"] = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 1_000_000,
    }
    write_jsonl(transcript_path, [entry])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    conn.row_factory = sqlite3.Row
    row = dict(conn.execute("SELECT * FROM calls WHERE request_id = 'req-flat'").fetchone())
    assert row["cache_write_5m_tokens"] == 1_000_000
    assert row["cache_write_1h_tokens"] == 0
    prices = {"m": {"input": 1, "output": 1, "cache_read": 1, "cache_write_5m": 3.75, "cache_write_1h": 6.0}}
    assert pricing.call_cost({**row, "model": "m"}, prices=prices) == 3.75


def test_parse_session_prefers_the_ttl_breakdown_over_the_flat_total(tmp_path):
    store_dir = tmp_path / "store"
    transcript_path = tmp_path / "session.jsonl"
    write_jsonl(transcript_path, [make_call_entry("req-both", cache_creation_input_tokens=99)])

    parser.parse_session(store_dir, transcript_path, "sess-1")

    conn = db.connect(store_dir)
    five, one = conn.execute(
        "SELECT cache_write_5m_tokens, cache_write_1h_tokens FROM calls WHERE request_id = 'req-both'"
    ).fetchone()
    assert (five, one) == (1, 2)
