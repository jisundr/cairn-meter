import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import export_usage  # noqa: E402

NOW = datetime(2026, 10, 13, 9, 30, 0, tzinfo=timezone.utc)


def _call(request_id, session_id, **kw):
    row = dict(
        request_id=request_id, session_id=session_id, agent="main", model="claude-sonnet-5",
        timestamp="2026-10-12T10:00:00Z", input_tokens=0, output_tokens=0,
        cache_read_tokens=0, cache_write_5m_tokens=0, cache_write_1h_tokens=0,
    )
    row.update(kw)
    return row


def _store(tmp_path, sessions):
    """sessions: {session_id: (project, [call kwargs...])}"""
    store = tmp_path / "store"
    conn = db.connect(store)
    n = 0
    for session_id, (project, calls) in sessions.items():
        db.save_session(conn, session_id=session_id, project=str(project))
        for kw in calls:
            n += 1
            db.insert_call(conn, _call(f"r{n}", session_id, **kw))
    conn.commit()
    conn.close()
    return store


def test_build_sums_tokens_and_prices_a_session(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path, {"s1": (project, [
        dict(timestamp="2026-10-12T10:00:00Z", input_tokens=1_000_000, output_tokens=100_000),
        dict(timestamp="2026-10-12T11:30:00Z", cache_read_tokens=500_000,
             cache_write_5m_tokens=200_000, cache_write_1h_tokens=100_000),
    ])})

    doc = export_usage.build(store, project, now=NOW)

    assert doc["schema"] == 1
    assert doc["currency"] == "USD"
    assert doc["generated"] == "2026-10-13T09:30:00Z"
    assert doc["project"] == os.path.abspath(project)
    assert doc["prices_as_of"]
    (s,) = doc["sessions"]
    assert s["session_id"] == "s1"
    assert (s["started"], s["ended"]) == ("2026-10-12T10:00:00Z", "2026-10-12T11:30:00Z")
    assert s["models"] == ["claude-sonnet-5"]
    assert s["input_tokens"] == 1_000_000
    assert s["output_tokens"] == 100_000
    assert s["cache_read_tokens"] == 500_000
    assert s["cache_write_tokens"] == 300_000
    # sonnet-5 per MTok: input 2.00, output 10.00, cache read 0.20, 5m 2.50, 1h 4.00
    assert s["cost_usd"] == pytest.approx(2.00 + 1.00 + 0.10 + 0.50 + 0.40)
    assert s["unpriced_models"] == []
    assert export_usage.validate(doc) == []


def test_unpriced_model_makes_cost_null_and_is_named(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path, {"s1": (project, [
        dict(input_tokens=1_000_000),
        dict(model="claude-made-up-9", input_tokens=10),
    ])})

    (s,) = export_usage.build(store, project, now=NOW)["sessions"]

    assert s["cost_usd"] is None
    assert s["unpriced_models"] == ["claude-made-up-9"]
    assert s["models"] == ["claude-made-up-9", "claude-sonnet-5"]
    assert s["input_tokens"] == 1_000_010


def test_only_the_projects_own_sessions_are_exported(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    store = _store(tmp_path, {
        "in-a": (a, [dict(input_tokens=1)]),
        "in-b": (b, [dict(input_tokens=2)]),
    })

    assert [s["session_id"] for s in export_usage.build(store, a)["sessions"]] == ["in-a"]
    assert [s["session_id"] for s in export_usage.build(store, b)["sessions"]] == ["in-b"]


def test_a_symlinked_project_path_still_matches(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    store = _store(tmp_path, {"s1": (real, [dict(input_tokens=1)])})

    assert [s["session_id"] for s in export_usage.build(store, link)["sessions"]] == ["s1"]


def test_missing_store_builds_an_empty_valid_document_without_creating_it(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    store = tmp_path / "no-store"

    doc = export_usage.build(store, project, now=NOW)

    assert doc["sessions"] == []
    assert export_usage.validate(doc) == []
    assert not store.exists()


def test_write_if_cairn_skips_a_project_without_cairn_dir(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path, {"s1": (project, [dict(input_tokens=1)])})

    assert export_usage.write_if_cairn(store, project, now=NOW) is None
    assert not (project / ".cairn").exists()


def test_write_if_cairn_writes_into_an_existing_cairn_dir_atomically(tmp_path):
    project = tmp_path / "proj"
    (project / ".cairn").mkdir(parents=True)
    store = _store(tmp_path, {"s1": (project, [dict(input_tokens=7)])})

    path = export_usage.write_if_cairn(store, project, now=NOW)

    assert path == project / ".cairn" / "export" / "usage.json"
    doc = json.loads(path.read_text())
    assert export_usage.validate(doc) == []
    assert doc["sessions"][0]["input_tokens"] == 7
    assert [p.name for p in path.parent.iterdir()] == ["usage.json"]  # no temp file left


def test_explicit_write_creates_cairn_with_a_gitignore_only_when_it_made_the_folder(tmp_path):
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    existing = tmp_path / "existing"
    (existing / ".cairn").mkdir(parents=True)
    store = _store(tmp_path, {})

    export_usage.write(store, fresh, now=NOW)
    export_usage.write(store, existing, now=NOW)

    assert (fresh / ".cairn" / ".gitignore").read_text() == "*\n"
    assert not (existing / ".cairn" / ".gitignore").exists()
    assert (fresh / ".cairn" / "export" / "usage.json").is_file()


@pytest.mark.parametrize("mutate, expected", [
    (lambda d: d.update(schema=2), "schema must be 1"),
    (lambda d: d.update(project="relative/path"), "project must be an absolute path"),
    (lambda d: d.update(currency="EUR"), 'currency must be "USD"'),
    (lambda d: d["sessions"][0].update(input_tokens=-1), "input_tokens must be a non-negative integer"),
    (lambda d: d["sessions"][0].update(started="yesterday"), "started must be a UTC ISO timestamp"),
    (lambda d: d["sessions"][0].update(cost_usd=None), "cost_usd is null exactly when"),
    (lambda d: d["sessions"][0].update(unpriced_models=["x"]), "cost_usd is null exactly when"),
])
def test_validate_rejects_a_document_that_breaks_the_contract(tmp_path, mutate, expected):
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path, {"s1": (project, [dict(input_tokens=1)])})
    doc = export_usage.build(store, project, now=NOW)
    mutate(doc)

    assert any(expected in p for p in export_usage.validate(doc))


def test_main_report_prints_sessions_and_writes_nothing(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path, {"abcdef123456": (project, [dict(input_tokens=1_000_000)])})

    assert export_usage.main(["--store", str(store), "--report", str(project)]) == 0

    out = capsys.readouterr().out
    assert "abcdef12" in out
    assert "$2.00" in out
    assert not (project / ".cairn").exists()


def test_main_export_writes_the_file(tmp_path, capsys):
    project = tmp_path / "proj"
    project.mkdir()
    store = _store(tmp_path, {"s1": (project, [dict(input_tokens=1)])})

    assert export_usage.main(["--store", str(store), str(project)]) == 0

    assert (project / ".cairn" / "export" / "usage.json").is_file()
