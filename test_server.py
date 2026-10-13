import http.server
import json
import socket
import sqlite3
import sys
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill  # noqa: E402
import db  # noqa: E402
import server  # noqa: E402


def make_call(**overrides):
    call = dict(
        request_id="req-1",
        session_id="sess-1",
        agent="main",
        model="claude-sonnet-5",
        timestamp="2026-08-28T12:00:00Z",
        input_tokens=100,
        output_tokens=200,
        cache_read_tokens=0,
        cache_write_5m_tokens=0,
        cache_write_1h_tokens=0,
    )
    call.update(overrides)
    return call


def make_tool_use(**overrides):
    tool_use = dict(
        tool_use_id="toolu-1",
        request_id="req-1",
        session_id="sess-1",
        agent="main",
        tool_name="Bash",
        timestamp="2026-08-28T12:00:00Z",
        detail=None,
    )
    tool_use.update(overrides)
    return tool_use


def make_store(tmp_path, calls=(), tool_uses=(), events=(), labels=None, projects=None, transcripts=None):
    """A meter.db under tmp_path/store. `projects` maps session_id -> project
    dir; a session in `calls` that is not named there has no recorded project."""
    store = tmp_path / "store"
    conn = db.connect(store)
    for c in calls:
        db.insert_call(conn, c)
    for t in tool_uses:
        db.insert_tool_use(conn, **t)
    for e in events:
        db.insert_usage_limit_event(conn, **e)
    for session_id, label in (labels or {}).items():
        db.save_session_label(conn, session_id=session_id, label=label)
    for session_id, project in (projects or {}).items():
        db.save_session(
            conn, session_id=session_id, project=str(project),
            transcript_path=str((transcripts or {}).get(session_id) or ""),
        )
    conn.commit()
    conn.close()
    return store


def _api_data(app, path, query=None):
    status, body = app.handle_api(path, query or {})
    assert status == 200
    return body["data"]


def test_resolve_range_day_counts():
    now = datetime(2026, 8, 28, 15, 30, tzinfo=timezone.utc)

    since, until, bucket = server.resolve_range("today", now=now)
    assert (since, until, bucket) == ("2026-08-28T00:00:00Z", "2026-08-28T15:30:00Z", "hour")

    since, until, bucket = server.resolve_range("7d", now=now)
    assert since == "2026-08-22T00:00:00Z"
    assert bucket == "day"

    since, _, _ = server.resolve_range("30d", now=now)
    assert since == "2026-07-30T00:00:00Z"

    since, _, _ = server.resolve_range("month", now=now)
    assert since == "2026-08-01T00:00:00Z"

    since, _, _ = server.resolve_range("6m", now=now)
    assert since == "2026-02-28T00:00:00Z"  # 182 days inclusive of today

    since, _, _ = server.resolve_range("13w", now=now)
    assert since == "2026-05-30T00:00:00Z"  # 91 days inclusive of today


def test_range_bounds_life_has_no_lower_bound():
    since, until = server.range_bounds("life", now=datetime(2026, 8, 28, tzinfo=timezone.utc))
    assert since is None
    assert until == "2026-08-28T00:00:00Z"


def test_rollup_group_by_agent_sums_tokens_and_cost():
    rows = [
        make_call(request_id="r1", agent="main", input_tokens=1_000_000, output_tokens=0),
        make_call(request_id="r2", agent="builder", input_tokens=500_000, output_tokens=0),
        make_call(request_id="r3", agent="builder", input_tokens=500_000, output_tokens=0),
    ]
    grouped = {g["key"]: g for g in server.rollup_group(rows, key_fn=lambda r: r["agent"])}

    assert grouped["main"]["calls"] == 1
    assert grouped["main"]["tokens"] == 1_000_000
    assert grouped["main"]["cost"] == pytest.approx(2.00)
    assert grouped["builder"]["calls"] == 2
    assert grouped["builder"]["tokens"] == 1_000_000


def test_rollup_timeseries_zero_fills_and_buckets_by_day():
    rows = [
        make_call(request_id="r1", timestamp="2026-08-26T10:00:00Z", input_tokens=10, output_tokens=0),
        make_call(request_id="r2", timestamp="2026-08-26T18:00:00Z", input_tokens=20, output_tokens=0),
        make_call(request_id="r3", timestamp="2026-08-28T09:00:00Z", input_tokens=30, output_tokens=0),
    ]
    points = server.rollup_timeseries(rows, "2026-08-26T00:00:00Z", "2026-08-28T00:00:00Z", "day")
    by_bucket = {p["bucket"]: p for p in points}

    assert list(by_bucket) == ["2026-08-26", "2026-08-27", "2026-08-28"]
    assert by_bucket["2026-08-26"]["tokens"] == 30
    assert by_bucket["2026-08-26"]["calls"] == 2
    assert by_bucket["2026-08-27"]["tokens"] == 0
    assert by_bucket["2026-08-27"]["calls"] == 0
    assert by_bucket["2026-08-28"]["tokens"] == 30


def test_rollup_timeseries_by_model_groups_and_sorts_per_bucket():
    rows = [
        make_call(request_id="r1", timestamp="2026-08-26T10:00:00Z", model="claude-sonnet-5", input_tokens=10, output_tokens=0),
        make_call(request_id="r2", timestamp="2026-08-26T11:00:00Z", model="claude-haiku-4.5", input_tokens=100, output_tokens=0),
        make_call(request_id="r3", timestamp="2026-08-26T12:00:00Z", model="claude-haiku-4.5", input_tokens=50, output_tokens=0),
    ]
    points = server.rollup_timeseries(rows, "2026-08-26T00:00:00Z", "2026-08-27T00:00:00Z", "day")
    by_bucket = {p["bucket"]: p for p in points}

    # Descending by tokens, same sort rollup_group always applies - haiku's
    # combined 150 outranks sonnet's 10 despite sonnet appearing first.
    assert [g["key"] for g in by_bucket["2026-08-26"]["by_model"]] == ["claude-haiku-4.5", "claude-sonnet-5"]
    assert by_bucket["2026-08-26"]["by_model"][0]["tokens"] == 150
    # A zero-call bucket's by_model is an empty list, not a missing key.
    assert by_bucket["2026-08-27"]["by_model"] == []


def test_rollup_tool_group_separates_tool_skill_and_mcp_families():
    rows = [
        make_tool_use(tool_use_id="t1", tool_name="Bash"),
        make_tool_use(tool_use_id="t2", tool_name="Bash"),
        make_tool_use(tool_use_id="t3", tool_name="Skill", detail="review-pr"),
        make_tool_use(tool_use_id="t4", tool_name="Skill", detail="review-pr"),
        make_tool_use(tool_use_id="t5", tool_name="mcp__claude-in-chrome__navigate"),
    ]

    tools = {g["key"]: g["count"] for g in server.rollup_tool_group(rows, key_fn=server._tool_key)}
    skills = {g["key"]: g["count"] for g in server.rollup_tool_group(rows, key_fn=server._skill_key)}
    mcp = {g["key"]: g["count"] for g in server.rollup_tool_group(rows, key_fn=server._mcp_key)}

    assert tools == {"Bash": 2}
    assert skills == {"review-pr": 2}
    assert mcp == {"claude-in-chrome": 1}


def test_skill_key_buckets_unresolved_detail_instead_of_dropping_the_row():
    rows = [
        make_tool_use(tool_use_id="t1", tool_name="Skill", detail=None),
        make_tool_use(tool_use_id="t2", tool_name="Skill", detail="review-pr"),
    ]
    skills = {g["key"]: g["count"] for g in server.rollup_tool_group(rows, key_fn=server._skill_key)}
    assert skills == {"unknown": 1, "review-pr": 1}


def test_session_trace_returns_none_for_unknown_session():
    assert server.build_session_trace("sess-missing", []) is None


def test_session_trace_includes_saved_label_when_present():
    calls = [make_call(request_id="r1", session_id="sess-1")]
    trace = server.build_session_trace("sess-1", calls, "Add a login page to the app")
    assert trace["label"] == "Add a login page to the app"


def test_session_trace_label_is_empty_when_no_saved_label():
    calls = [make_call(request_id="r1", session_id="sess-1")]
    trace = server.build_session_trace("sess-1", calls)
    assert trace["label"] == ""


def test_unpriced_model_group_reports_null_pure_group_reports_number_individual_reports_unknown():
    priced = make_call(request_id="r1", model="claude-sonnet-5", input_tokens=1_000_000, output_tokens=0)
    unpriced = make_call(request_id="r2", model="claude-nonexistent-9000", agent="main")

    mixed_group = server.rollup_group([priced, unpriced], key_fn=lambda r: r["agent"])
    assert mixed_group[0]["cost"] is None

    pure_priced_group = server.rollup_group([priced], key_fn=lambda r: r["agent"])
    assert pure_priced_group[0]["cost"] == pytest.approx(2.00)

    import pricing

    assert pricing.call_cost(unpriced) == "unknown"


def test_rollup_sessions_normalizes_a_null_agent_instead_of_crashing():
    calls = [
        make_call(request_id="r1", session_id="sess-1", agent=None),
        make_call(request_id="r2", session_id="sess-1", agent="builder"),
    ]
    for row in calls:
        row["project"] = "proj"

    sessions = server.rollup_sessions(calls, [])

    assert sessions[0]["agents"] == ["builder", "unknown"]


def test_session_without_usage_limit_event_reports_false():
    events = []
    calls = [make_call(request_id="r1", session_id="sess-1")]
    for row in calls:
        row["project"] = "proj"
    sessions = server.rollup_sessions(calls, events)
    assert sessions[0]["usage_limit_hit"] is False


def test_rollup_sessions_includes_saved_label_when_present():
    calls = [make_call(request_id="r1", session_id="sess-1")]
    for row in calls:
        row["project"] = "proj"

    sessions = server.rollup_sessions(calls, [], {"sess-1": "Add a login page to the app"})

    assert sessions[0]["label"] == "Add a login page to the app"


def test_rollup_sessions_label_is_empty_when_no_saved_label():
    calls = [make_call(request_id="r1", session_id="sess-1")]
    for row in calls:
        row["project"] = "proj"

    sessions_no_labels_arg = server.rollup_sessions(calls, [])
    assert sessions_no_labels_arg[0]["label"] == ""

    sessions_unmatched_labels = server.rollup_sessions(calls, [], {"sess-other": "Some other session"})
    assert sessions_unmatched_labels[0]["label"] == ""


def test_encode_project_path_swaps_dots_as_well_as_slashes(tmp_path):
    # Claude Code's own encoding swaps "." for "-" too, not just "/" - a
    # project folder segment with a dot in it (e.g. "cairn-2.0") otherwise
    # encodes to the wrong folder name and its transcripts are never found.
    root = tmp_path / "cairn-2.0" / "token-metering"
    root.mkdir(parents=True)

    encoded = server.encode_project_path(root)

    assert "." not in encoded
    assert encoded == str(root.resolve()).replace("/", "-").replace(".", "-")


def test_extract_call_content_skips_tool_result_echo_to_find_the_real_prompt():
    entries = [
        {"type": "user", "message": {"role": "user", "content": "real question"}},
        {
            "type": "assistant",
            "requestId": "r1",
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]},
        },
        {
            "type": "user",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "output"}]},
        },
        {
            "type": "assistant",
            "requestId": "r2",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "the answer"}]},
        },
    ]

    assert server._extract_call_content(entries, "r2") == ("real question", "the answer", [])


def test_extract_call_content_collects_tool_calls_with_per_tool_summary():
    entries = [
        {"type": "user", "message": {"role": "user", "content": "fix the bug"}},
        {
            "type": "assistant",
            "requestId": "r1",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/a/b.py"}},
                    {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "pytest -q"}},
                    {"type": "tool_use", "id": "t3", "name": "Grep", "input": {"pattern": "TODO"}},
                    {"type": "tool_use", "id": "t4", "name": "WebFetch", "input": {"url": "https://example.com"}},
                    {"type": "tool_use", "id": "t5", "name": "Task", "input": {"description": "investigate"}},
                    {"type": "tool_use", "id": "t6", "name": "Skill", "input": {"skill": "cairn:shared"}},
                    {"type": "tool_use", "id": "t7", "name": "SomeUnknownTool", "input": {"x": "y"}},
                    {"type": "tool_use", "id": "t8", "name": "Write", "input": {}},
                    {"type": "text", "text": "done"},
                ],
            },
        },
    ]

    prompt, response, tool_calls = server._extract_call_content(entries, "r1")

    assert prompt == "fix the bug"
    assert response == "done"
    assert tool_calls == [
        {"name": "Read", "summary": "/a/b.py"},
        {"name": "Bash", "summary": "pytest -q"},
        {"name": "Grep", "summary": "TODO"},
        {"name": "WebFetch", "summary": "https://example.com"},
        {"name": "Task", "summary": "investigate"},
        {"name": "Skill", "summary": "cairn:shared"},
        {"name": "SomeUnknownTool", "summary": ""},
        {"name": "Write", "summary": ""},
    ]


# --------------------------------------------------------------------------
# Static serving
# --------------------------------------------------------------------------


@pytest.fixture
def static_site(tmp_path):
    """A built-looking static dir: index.html plus assets/app.js."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><p>spa</p>\n")
    (static / "assets" / "app.js").write_text("console.log(1)\n")
    return static


@pytest.mark.parametrize("rel", ["/\x00", "/assets/\x00x.js"])
def test_safe_static_path_refuses_a_nul_byte(static_site, rel):
    assert server._safe_static_path(static_site, rel) is None


def _raw_get(port: int, raw_path: bytes, host: bytes | None = b"127.0.0.1") -> bytes:
    host_line = b"Host: " + host + b"\r\n" if host is not None else b""
    request = b"GET " + raw_path + b" HTTP/1.1\r\n" + host_line + b"Connection: close\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


@pytest.mark.parametrize("raw_path", [b"/\x00", b"/assets/\x00x.js"])
def test_http_static_route_answers_a_raw_nul_byte_with_the_spa_fallback(tmp_path, static_site, raw_path):
    app = server.MeterApp(make_store(tmp_path), static_dir=static_site)
    handler = server.make_handler(app)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.allowed_hosts = server.allowed_hosts(httpd)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        response = _raw_get(port, raw_path, host=f"127.0.0.1:{port}".encode())
        assert response.startswith(b"HTTP/1.")
        head, _, body = response.partition(b"\r\n\r\n")
        assert b" 200 " in head.split(b"\r\n")[0]
        assert body == b"<!doctype html><p>spa</p>\n"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/assets/app.js") as resp:
            assert resp.status == 200
            assert resp.read() == b"console.log(1)\n"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(scope="module")
def live_port(tmp_path_factory):
    """One server for the Host-header tests (httpd.shutdown() costs ~0.5s)."""
    tmp_path = tmp_path_factory.mktemp("host")
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><p>spa</p>\n")
    (static / "assets" / "app.js").write_text("console.log(1)\n")
    app = server.MeterApp(make_store(tmp_path, calls=[make_call()]), static_dir=static)
    handler = server.make_handler(app)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    handler.allowed_hosts = server.allowed_hosts(httpd)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("path", [b"/api/rollup/timeseries?range=life", b"/api/call/sess-1/1", b"/", b"/assets/app.js"])
@pytest.mark.parametrize("host", [b"evil.example", b"evil.example:%d", b"127.0.0.1", b"localhost:1"])
def test_forged_host_gets_403_json_on_api_and_static_routes(live_port, path, host):
    if b"%d" in host:
        host = host % live_port
    response = _raw_get(live_port, path, host=host)
    head, _, body = response.partition(b"\r\n\r\n")
    assert b" 403 " in head.split(b"\r\n")[0]
    assert json.loads(body) == {"error": "forbidden host"}


def test_missing_host_header_is_refused(live_port):
    response = _raw_get(live_port, b"/api/rollup/timeseries?range=life", host=None)
    assert b" 403 " in response.split(b"\r\n")[0]


@pytest.mark.parametrize("name", ["127.0.0.1", "localhost"])
@pytest.mark.parametrize("path", [b"/api/rollup/timeseries?range=life", b"/api/call/sess-1/1", b"/assets/app.js"])
def test_real_host_gets_200(live_port, name, path):
    response = _raw_get(live_port, path, host=f"{name}:{live_port}".encode())
    assert b" 200 " in response.split(b"\r\n")[0]


def test_allowed_hosts_adds_ipv6_loopback_only_for_an_ipv6_socket():
    class Fake:
        server_address = ("::1", 4321)
        address_family = socket.AF_INET6

    assert "[::1]:4321" in server.allowed_hosts(Fake())
    Fake.address_family = socket.AF_INET
    assert server.allowed_hosts(Fake()) == {"127.0.0.1:4321", "localhost:4321"}


# --------------------------------------------------------------------------
# Store reads: ranges, day detail, session trace
# --------------------------------------------------------------------------


def test_fetch_includes_subsecond_timestamps_at_the_lower_boundary(tmp_path):
    store = make_store(tmp_path, calls=[
        make_call(request_id="r1", timestamp="2026-08-28T00:00:00.500Z", input_tokens=1, output_tokens=0),
        make_call(request_id="r2", timestamp="2026-08-27T23:59:59.900Z", input_tokens=2, output_tokens=0),
    ])
    app = server.MeterApp(store)

    rows = app._fetch_table("calls", None, since="2026-08-28T00:00:00Z", until="2026-08-29T00:00:00Z")

    # r1 is just after the lower bound - included; r2 just before - excluded.
    assert {r["request_id"] for r in rows} == {"r1"}


def test_day_detail_accepts_an_unpadded_date_and_still_matches_zero_padded_rows(tmp_path):
    store = make_store(tmp_path, calls=[
        make_call(request_id="r1", timestamp="2026-08-05T10:00:00Z", input_tokens=100, output_tokens=0),
    ])
    detail = server.MeterApp(store).day_detail("2026-08-5")

    assert detail["total_tokens"] == 100
    assert detail["by_model"][0]["calls"] == 1


def test_day_detail_adds_by_tool_and_by_agent_for_the_same_window(tmp_path):
    store = make_store(
        tmp_path,
        calls=[
            make_call(request_id="r1", agent="builder", timestamp="2026-08-05T10:00:00Z", input_tokens=100, output_tokens=0),
            make_call(request_id="r2", agent="reviewer", timestamp="2026-08-05T11:00:00Z", input_tokens=50, output_tokens=0),
            # The next day: excluded from every group.
            make_call(request_id="r3", agent="builder", timestamp="2026-08-06T10:00:00Z", input_tokens=999, output_tokens=0),
        ],
        tool_uses=[
            make_tool_use(tool_use_id="t1", request_id="r1", agent="builder", tool_name="Bash", timestamp="2026-08-05T10:00:00Z"),
            make_tool_use(tool_use_id="t2", request_id="r1", agent="builder", tool_name="Bash", timestamp="2026-08-05T10:05:00Z"),
            make_tool_use(tool_use_id="t3", request_id="r2", agent="reviewer", tool_name="Read", timestamp="2026-08-05T11:00:00Z"),
            make_tool_use(tool_use_id="t4", request_id="r3", agent="builder", tool_name="Bash", timestamp="2026-08-06T10:00:00Z"),
        ],
    )

    detail = server.MeterApp(store).day_detail("2026-08-05")

    assert {g["key"]: g["count"] for g in detail["by_tool"]} == {"Bash": 2, "Read": 1}
    by_agent = {g["key"]: g for g in detail["by_agent"]}
    assert by_agent["builder"]["tokens"] == 100
    assert by_agent["reviewer"]["tokens"] == 50


def test_session_trace_orders_calls_and_global_position_agrees_with_call_detail(tmp_path):
    calls = [
        make_call(request_id="r-main-1", agent="main", timestamp="2026-08-27T14:00:00Z"),
        make_call(request_id="r-builder-1", agent="builder", timestamp="2026-08-27T14:05:00Z"),
        make_call(request_id="r-builder-2", agent="builder", timestamp="2026-08-27T14:15:00Z"),
        make_call(request_id="r-main-2", agent="main", timestamp="2026-08-27T14:20:00Z"),
    ]
    trace = server.build_session_trace("sess-1", calls)

    assert [a["agent"] for a in trace["agents"]] == ["main", "builder"]
    main_trace = trace["agents"][0]["trace"]
    builder_trace = trace["agents"][1]["trace"]
    assert [c["request_id"] for c in main_trace] == ["r-main-1", "r-main-2"]
    assert main_trace[0]["duration_seconds"] == pytest.approx(1200.0)
    assert main_trace[1]["duration_seconds"] is None
    assert [c["global_position"] for c in main_trace] == [1, 4]
    assert [c["global_position"] for c in builder_trace] == [2, 3]

    app = server.MeterApp(make_store(tmp_path, calls=calls))
    for call in main_trace + builder_trace:
        detail = app.call_detail("sess-1", call["global_position"])
        assert detail["request_id"] == call["request_id"]


def test_session_trace_and_call_detail_only_see_their_own_session(tmp_path):
    store = make_store(tmp_path, calls=[
        make_call(request_id="r1", session_id="sess-1", timestamp="2026-08-27T14:00:00Z"),
        make_call(request_id="r2", session_id="sess-1", timestamp="2026-08-27T14:05:00Z"),
        make_call(request_id="r3", session_id="sess-2", timestamp="2026-08-27T14:10:00Z"),
    ])
    app = server.MeterApp(store)

    trace = app.session_trace("sess-1")
    assert {c["request_id"] for a in trace["agents"] for c in a["trace"]} == {"r1", "r2"}
    assert app.call_detail("sess-1", 2)["request_id"] == "r2"
    assert app.call_detail("sess-1", 3) is None


def test_calls_session_id_query_uses_the_index(tmp_path):
    store = make_store(tmp_path, calls=[make_call(request_id="r1", session_id="sess-1")])
    conn = server._open_readonly(db.db_path(store), "calls")
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN SELECT * FROM calls WHERE session_id = ?", ("sess-1",)).fetchall()
    finally:
        conn.close()

    assert "idx_calls_session_id" in " ".join(str(cell) for row in plan for cell in row)


def test_usage_limit_events_surfaced_separately_and_not_counted_as_calls(tmp_path):
    store = make_store(
        tmp_path,
        calls=[make_call(request_id="r1", session_id="sess-1")],
        events=[dict(session_id="sess-1", timestamp="2026-08-28T12:30:00Z", raw_entry='{"isApiErrorMessage": true}')],
    )
    app = server.MeterApp(store)

    events = app.usage_limit_events("life")
    assert [e["session_id"] for e in events] == ["sess-1"]
    sessions = app.sessions("life")
    assert sessions[0]["calls"] == 1
    assert sessions[0]["usage_limit_hit"] is True


def test_sessions_and_trace_surface_saved_label_or_empty(tmp_path):
    store = make_store(
        tmp_path,
        calls=[make_call(request_id="r1", session_id="sess-labeled"), make_call(request_id="r2", session_id="sess-unlabeled")],
        labels={"sess-labeled": "Add a login page to the app"},
    )
    app = server.MeterApp(store)

    sessions = {s["session_id"]: s for s in app.sessions("life")}
    assert sessions["sess-labeled"]["label"] == "Add a login page to the app"
    assert sessions["sess-unlabeled"]["label"] == ""
    assert app.session_trace("sess-labeled")["label"] == "Add a login page to the app"
    assert app.session_trace("sess-unlabeled")["label"] == ""


def test_session_payloads_carry_no_cairn_version(tmp_path):
    app = server.MeterApp(make_store(tmp_path, calls=[make_call()]))

    assert "cairn_version" not in _api_data(app, "/api/rollup/session", {"range": ["life"]})[0]
    assert "cairn_version" not in _api_data(app, "/api/session/sess-1/trace")


# --------------------------------------------------------------------------
# Projects: derived from where each session ran
# --------------------------------------------------------------------------


def test_projects_come_from_the_sessions_recorded_directories_and_rows_carry_the_label(tmp_path):
    store = make_store(
        tmp_path,
        calls=[
            make_call(request_id="a1", session_id="sess-a", input_tokens=1_000_000, output_tokens=0),
            make_call(request_id="b1", session_id="sess-b", input_tokens=2_000_000, output_tokens=0),
        ],
        projects={"sess-a": "/work/project-a", "sess-b": "/work/project-b"},
    )
    app = server.MeterApp(store)

    assert {p.label for p in app.projects()} == {"project-a", "project-b"}
    rows = app._ranged_calls("life", None)
    assert {r["session_id"]: r["project"] for r in rows} == {"sess-a": "project-a", "sess-b": "project-b"}
    assert {r["project"] for r in app._ranged_calls("life", "project-b")} == {"project-b"}


def test_a_session_without_a_recorded_project_is_grouped_under_unknown(tmp_path):
    app = server.MeterApp(make_store(tmp_path, calls=[make_call()]))

    assert [p.label for p in app.projects()] == ["(unknown)"]
    assert app.sessions("life")[0]["project"] == "(unknown)"


def test_colliding_directory_names_are_lengthened_and_call_detail_resolves_each(tmp_path):
    store = make_store(
        tmp_path,
        calls=[make_call(request_id="a1", session_id="sess-a"), make_call(request_id="b1", session_id="sess-b")],
        projects={"sess-a": "/org1/backend", "sess-b": "/org2/backend"},
    )
    app = server.MeterApp(store)

    assert {p.label for p in app.projects()} == {"org1/backend", "org2/backend"}
    assert app.call_detail("sess-a", 1)["project"] == "org1/backend"
    assert app.call_detail("sess-b", 1)["project"] == "org2/backend"


def test_a_root_inside_another_root_reports_it_as_parent_and_the_filter_cascades(tmp_path):
    store = make_store(
        tmp_path,
        calls=[
            make_call(request_id="e1", session_id="sess-engine", input_tokens=1_000_000, output_tokens=0),
            make_call(request_id="s1", session_id="sess-site", input_tokens=2_000_000, output_tokens=0),
            make_call(request_id="p1", session_id="sess-parent", input_tokens=5, output_tokens=0),
            make_call(request_id="o1", session_id="sess-other", input_tokens=7, output_tokens=0),
        ],
        projects={
            "sess-engine": "/w/ai-worth-carrying/engine",
            "sess-site": "/w/ai-worth-carrying/site",
            "sess-parent": "/w/ai-worth-carrying",
            "sess-other": "/w/unrelated",
        },
    )
    app = server.MeterApp(store)
    by_label = {p.label: p for p in app.projects()}

    assert by_label["engine"].parent == "ai-worth-carrying"
    assert by_label["site"].parent == "ai-worth-carrying"
    assert by_label["ai-worth-carrying"].parent is None
    assert by_label["unrelated"].parent is None
    rows = app._ranged_calls("life", "ai-worth-carrying")
    assert {r["project"] for r in rows} == {"engine", "site", "ai-worth-carrying"}


def test_handle_api_projects_reports_the_system_hostname_and_each_projects_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(server.socket, "gethostname", lambda: "my-laptop")
    app = server.MeterApp(make_store(tmp_path, calls=[make_call()], projects={"sess-1": "/w/solo-project"}))

    status, body = app.handle_api("/api/projects", {})

    assert status == 200
    assert body["data"]["hostname"] == "my-laptop"
    assert body["data"]["projects"] == [{"label": "solo-project", "parent": None}]


def test_projects_caches_within_ttl_but_sees_a_new_project_when_ttl_is_zero(tmp_path):
    store = make_store(tmp_path, calls=[make_call()], projects={"sess-1": "/w/one"})
    cached = server.MeterApp(store, discovery_cache_ttl=60)
    uncached = server.MeterApp(store, discovery_cache_ttl=0)
    assert [p.label for p in cached.projects()] == ["one"]
    assert [p.label for p in uncached.projects()] == ["one"]

    conn = db.connect(store)
    db.insert_call(conn, make_call(request_id="r2", session_id="sess-2"))
    db.save_session(conn, session_id="sess-2", project="/w/two")
    conn.commit()
    conn.close()

    assert [p.label for p in cached.projects()] == ["one"]
    assert {p.label for p in uncached.projects()} == {"one", "two"}


# --------------------------------------------------------------------------
# Cold start and bad input
# --------------------------------------------------------------------------


def test_cold_start_missing_store_returns_empty_results_and_creates_nothing(tmp_path):
    store = tmp_path / "fresh-store"
    app = server.MeterApp(store)

    assert app.agent_rollup("7d") == []
    assert app.sessions("7d") == []
    assert app.tool_rollup("7d") == []
    assert app.projects() == []
    timeseries = app.timeseries("life")
    assert timeseries["points"] == []
    assert timeseries["total_cost"] == 0.0
    assert not store.exists()


def test_cold_start_empty_db_returns_empty_results(tmp_path):
    app = server.MeterApp(make_store(tmp_path))

    assert app.agent_rollup("30d") == []
    assert app.sessions("30d") == []
    assert app.usage_limit_events("30d") == []


def test_handle_api_404s_for_unknown_session_and_call(tmp_path):
    app = server.MeterApp(make_store(tmp_path))

    assert app.handle_api("/api/session/no-such-session/trace", {})[0] == 404
    assert app.handle_api("/api/call/no-such-session/1", {})[0] == 404


def test_handle_api_rejects_unknown_range(tmp_path):
    app = server.MeterApp(tmp_path / "none")
    status, body = app.handle_api("/api/rollup/agent", {"range": ["bogus"]})
    assert status == 400
    assert "unknown range" in body["error"]


def test_handle_api_has_no_task_routes(tmp_path):
    app = server.MeterApp(make_store(tmp_path))

    for path in ("/api/tasks", "/api/tasks/detail", "/api/tasks/doc"):
        assert app.handle_api(path, {})[0] == 404


# --------------------------------------------------------------------------
# Transcript lookup
# --------------------------------------------------------------------------


def _write_transcript(path: Path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


_TRANSCRIPT = [
    {"type": "user", "message": {"role": "user", "content": "What's the token total?"}},
    {
        "type": "assistant",
        "requestId": "r1",
        "timestamp": "2026-08-28T12:00:00Z",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "It's 300 tokens."}]},
    },
]


def test_call_detail_transcript_unavailable_still_reports_tokens_and_cost(tmp_path):
    store = make_store(
        tmp_path, calls=[make_call(request_id="r1", input_tokens=1_000_000, output_tokens=0)],
        projects={"sess-1": "/w/proj"},
    )
    app = server.MeterApp(store, claude_projects_dir=tmp_path / "never-populated")

    detail = app.call_detail("sess-1", 1)

    assert detail["available"] is False
    assert detail["prompt"] is None and detail["response"] is None
    assert detail["tool_calls"] == []
    assert detail["input_tokens"] == 1_000_000
    assert detail["cost"] == pytest.approx(2.00)


def test_call_detail_reads_the_transcript_path_recorded_at_capture(tmp_path):
    transcript = tmp_path / "anywhere" / "sess-1.jsonl"
    _write_transcript(transcript, _TRANSCRIPT)
    store = make_store(
        tmp_path, calls=[make_call(request_id="r1")],
        projects={"sess-1": "/w/proj"}, transcripts={"sess-1": transcript},
    )

    detail = server.MeterApp(store, claude_projects_dir=tmp_path / "unused").call_detail("sess-1", 1)

    assert detail["available"] is True
    assert detail["prompt"] == "What's the token total?"
    assert detail["response"] == "It's 300 tokens."


def test_call_detail_falls_back_to_the_projects_dir_layout_when_the_recorded_path_is_gone(tmp_path):
    claude_projects = tmp_path / "claude-home" / "projects"
    root = tmp_path / "work" / "proj"
    transcript = server.transcript_path_for(claude_projects, root, "sess-1")
    _write_transcript(transcript, _TRANSCRIPT)
    store = make_store(
        tmp_path, calls=[make_call(request_id="r1")],
        projects={"sess-1": root}, transcripts={"sess-1": tmp_path / "moved-away.jsonl"},
    )

    detail = server.MeterApp(store, claude_projects_dir=claude_projects).call_detail("sess-1", 1)

    assert detail["available"] is True
    assert detail["response"] == "It's 300 tokens."


def test_call_detail_falls_back_to_a_subagent_transcript(tmp_path):
    transcript = tmp_path / "t" / "sess-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("")  # main transcript, no matching request
    _write_transcript(transcript.parent / "sess-1" / "subagents" / "agent-abc123.jsonl", [
        {"type": "user", "message": {"role": "user", "content": "Do the thing."}},
        {"type": "assistant", "requestId": "r-sub",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]}},
    ])
    store = make_store(
        tmp_path, calls=[make_call(request_id="r-sub", agent="builder")],
        projects={"sess-1": "/w/proj"}, transcripts={"sess-1": transcript},
    )

    detail = server.MeterApp(store).call_detail("sess-1", 1)

    assert detail["available"] is True
    assert detail["response"] == "Done."


# --------------------------------------------------------------------------
# HTTP smoke tests (thin dispatch layer only)
# --------------------------------------------------------------------------


def test_default_port_is_4318():
    assert server.DEFAULT_PORT == 4318


def test_http_smoke_rollup_timeseries_endpoint(tmp_path):
    port = server.start(make_store(tmp_path, calls=[make_call()]), backfill_enabled=False)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rollup/timeseries?range=life") as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
            assert body["data"]["total_tokens"] == 300
    finally:
        server.stop()


def test_http_smoke_catch_all_serves_html_for_a_client_route(tmp_path):
    port = server.start(make_store(tmp_path), backfill_enabled=False)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/sessions/sess-1") as resp:
            assert resp.status == 200
            assert "text/html" in resp.headers.get("Content-Type", "")
    finally:
        server.stop()


def test_start_returns_without_blocking_on_backfill(tmp_path, monkeypatch):
    release = threading.Event()
    finished = threading.Event()

    def blocking_run(store_dir=None, claude_projects_dir=None):
        release.wait(timeout=5)
        finished.set()

    monkeypatch.setattr(backfill, "run", blocking_run)

    port = server.start(make_store(tmp_path))
    try:
        assert port > 0
        assert not finished.is_set()
    finally:
        release.set()
        finished.wait(timeout=2)
        server.stop()


def test_start_backfill_enabled_false_skips_backfill(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(backfill, "run", lambda **kwargs: calls.append(kwargs))

    port = server.start(make_store(tmp_path), backfill_enabled=False)
    try:
        assert port > 0
    finally:
        server.stop()

    assert calls == []


def test_start_runs_backfill_into_the_same_store_it_serves(tmp_path):
    claude_projects = tmp_path / "claude-projects"
    entry = {"requestId": "req-bf", "cwd": "/w/proj", "timestamp": "2026-08-28T12:00:00Z",
             "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 5, "output_tokens": 1}}}
    _write_transcript(claude_projects / "-w-proj" / "sess-bf.jsonl", [entry])
    store = tmp_path / "store"
    port = server.start(store, claude_projects_dir=claude_projects)
    try:
        deadline = datetime.now().timestamp() + 5
        rows = []
        while datetime.now().timestamp() < deadline and not rows:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/rollup/session?range=life") as resp:
                rows = json.loads(resp.read())["data"]
        assert [r["session_id"] for r in rows] == ["sess-bf"]
    finally:
        server.stop()
