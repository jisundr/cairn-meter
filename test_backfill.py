import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import backfill  # noqa: E402
import db  # noqa: E402

from test_parser import make_call_entry, write_jsonl  # noqa: E402


def _write_session(projects_dir: Path, folder: str, session_id: str, *, cwd=None, request_id=None):
    entry = make_call_entry(request_id or f"req-{session_id}")
    if cwd is not None:
        entry["cwd"] = cwd
    path = projects_dir / folder / f"{session_id}.jsonl"
    write_jsonl(path, [entry])
    return path


def _sessions(store: Path) -> dict:
    conn = db.connect(store)
    try:
        return dict(conn.execute("SELECT session_id, project FROM sessions").fetchall())
    finally:
        conn.close()


def test_ingests_every_project_folder_and_reads_project_from_cwd(tmp_path):
    projects = tmp_path / "claude-projects"
    store = tmp_path / "store"
    _write_session(projects, "-work-a", "sess-a", cwd="/work/a")
    _write_session(projects, "-work-b", "sess-b", cwd="/work/b")

    added = backfill.run(store, projects)

    assert added == 2
    assert _sessions(store) == {"sess-a": "/work/a", "sess-b": "/work/b"}


def test_second_run_adds_nothing_and_new_transcripts_are_picked_up(tmp_path):
    projects = tmp_path / "claude-projects"
    store = tmp_path / "store"
    _write_session(projects, "-work-a", "sess-a", cwd="/work/a")
    assert backfill.run(store, projects) == 1
    assert backfill.run(store, projects) == 0

    _write_session(projects, "-work-a", "sess-c", cwd="/work/a")

    assert backfill.run(store, projects) == 1


def test_a_missing_projects_dir_adds_nothing_and_creates_no_store(tmp_path):
    store = tmp_path / "store"

    assert backfill.run(store, tmp_path / "absent") == 0
    assert not store.exists()


def test_one_broken_transcript_does_not_stop_the_rest(tmp_path, monkeypatch):
    projects = tmp_path / "claude-projects"
    store = tmp_path / "store"
    _write_session(projects, "-w", "sess-bad", cwd="/w")
    _write_session(projects, "-w", "sess-good", cwd="/w")
    real = backfill.parser.parse_session

    def flaky(store_dir, path, session_id, project=None):
        if session_id == "sess-bad":
            raise RuntimeError("unreadable")
        return real(store_dir, path, session_id, project)

    monkeypatch.setattr(backfill.parser, "parse_session", flaky)

    assert backfill.run(store, projects) == 1
    assert set(_sessions(store)) == {"sess-good"}


def test_backfill_writes_nothing_under_the_projects_dir(tmp_path):
    projects = tmp_path / "claude-projects"
    store = tmp_path / "store"
    path = _write_session(projects, "-w", "sess-a", cwd="/w")
    before = sorted(p.relative_to(projects) for p in projects.rglob("*"))

    backfill.run(store, projects)

    assert sorted(p.relative_to(projects) for p in projects.rglob("*")) == before
    assert path.exists()
