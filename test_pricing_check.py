import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import pricing_check  # noqa: E402

from test_pricing import make_call  # noqa: E402


def _seed(store: Path, *models: str) -> Path:
    conn = db.connect(store)
    try:
        for i, model in enumerate(models):
            db.insert_call(conn, make_call(request_id=f"r-{i}", model=model))
        conn.commit()
    finally:
        conn.close()
    return store


def test_unpriced_model_is_detected(tmp_path):
    store = _seed(tmp_path / "s", "known", "mystery")
    assert pricing_check.unpriced_models(store, {"known": {}}) == {"mystery"}


def test_fully_priced_store_is_empty(tmp_path):
    store = _seed(tmp_path / "s", "known")
    assert pricing_check.unpriced_models(store, {"known": {}}) == set()


def test_store_without_a_database_reports_nothing_and_creates_nothing(tmp_path):
    store = tmp_path / "bare"
    assert pricing_check.unpriced_models(store, {}) == set()
    assert not store.exists()


def test_same_day_does_not_rescan(tmp_path, monkeypatch, capsys):
    store = _seed(tmp_path / "s", "mystery")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_checked": "2026-09-29"}))
    calls = []
    monkeypatch.setattr(pricing_check, "unpriced_models", lambda *a, **k: calls.append(a) or set())
    pricing_check.maybe_run(store, state, today="2026-09-29")
    assert calls == []
    assert capsys.readouterr().err == ""


def test_new_day_rescans_and_restamps(tmp_path, monkeypatch):
    store = _seed(tmp_path / "s")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"last_checked": "2026-09-28"}))
    calls = []
    monkeypatch.setattr(pricing_check, "unpriced_models", lambda *a, **k: calls.append(a) or set())
    pricing_check.maybe_run(store, state, today="2026-09-29")
    assert len(calls) == 1
    assert json.loads(state.read_text()) == {"last_checked": "2026-09-29"}


def test_warns_once_naming_model_then_silent_same_day(tmp_path, capsys):
    store = _seed(tmp_path / "s", "claude-unpriced-9")
    pricing_check.maybe_run(store, today="2026-09-29")
    assert "claude-unpriced-9" in capsys.readouterr().err
    pricing_check.maybe_run(store, today="2026-09-29")
    assert capsys.readouterr().err == ""


def test_state_file_defaults_to_the_store_directory(tmp_path):
    store = _seed(tmp_path / "s", "claude-sonnet-5")
    pricing_check.maybe_run(store, today="2026-09-29")
    assert json.loads((store / pricing_check.STATE_FILENAME).read_text()) == {"last_checked": "2026-09-29"}
