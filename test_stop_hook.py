"""The Stop hook against a throwaway HOME and project, with no core present."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_usage  # noqa: E402

ROOT = Path(__file__).resolve().parent
HOOK = ROOT / "hooks" / "stop-meter.sh"


def _transcript(path: Path, cwd: Path):
    entries = [
        {"type": "ai-title", "aiTitle": "Add login"},
        {
            "requestId": "req-1", "cwd": str(cwd), "timestamp": "2026-10-12T10:00:00Z",
            "message": {"model": "claude-sonnet-5", "role": "assistant", "content": [],
                        "usage": {"input_tokens": 1_000_000, "output_tokens": 0}},
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _stop(home: Path, project: Path, transcript: Path, session_id="sess-1"):
    payload = json.dumps({"session_id": session_id, "transcript_path": str(transcript), "cwd": str(project)})
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CLAUDE_PLUGIN_ROOT": str(ROOT)}
    return subprocess.run([str(HOOK)], input=payload, text=True, env=env, capture_output=True, timeout=30)


def _tree(root: Path) -> set[str]:
    return {str(p.relative_to(root)) for p in root.rglob("*")}


def test_stop_in_a_project_with_cairn_dir_creates_meter_db_and_a_valid_usage_json(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    (project / ".cairn").mkdir(parents=True)
    home.mkdir()
    transcript = tmp_path / "t" / "sess-1.jsonl"
    _transcript(transcript, project)

    result = _stop(home, project, transcript)

    assert result.returncode == 0
    assert (home / ".claude" / "cairn-meter" / "meter.db").is_file()
    usage = project / ".cairn" / "export" / "usage.json"
    doc = json.loads(usage.read_text())
    assert export_usage.validate(doc) == []
    assert doc["project"] == str(project)
    assert [s["session_id"] for s in doc["sessions"]] == ["sess-1"]
    assert doc["sessions"][0]["input_tokens"] == 1_000_000
    assert doc["sessions"][0]["cost_usd"] == 2.0
    # Nothing is written under ~/.claude/cairn/, which belongs to core.
    assert not (home / ".claude" / "cairn").exists()
    assert {p.split("/")[1] for p in _tree(home) if p.startswith(".claude/")} == {"cairn-meter"}


def test_stop_in_a_project_without_cairn_dir_meters_but_writes_no_export(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    home.mkdir()
    transcript = tmp_path / "t" / "sess-1.jsonl"
    _transcript(transcript, project)

    result = _stop(home, project, transcript)

    assert result.returncode == 0
    assert (home / ".claude" / "cairn-meter" / "meter.db").is_file()
    assert _tree(project) == set()
    assert not (home / ".claude" / "cairn").exists()


def test_stop_twice_rewrites_the_export_without_duplicating_the_session(tmp_path):
    home, project = tmp_path / "home", tmp_path / "project"
    (project / ".cairn").mkdir(parents=True)
    home.mkdir()
    transcript = tmp_path / "t" / "sess-1.jsonl"
    _transcript(transcript, project)

    _stop(home, project, transcript)
    _stop(home, project, transcript)

    doc = json.loads((project / ".cairn" / "export" / "usage.json").read_text())
    assert len(doc["sessions"]) == 1
    assert doc["sessions"][0]["input_tokens"] == 1_000_000


def test_stop_with_bad_input_exits_zero_and_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "CLAUDE_PLUGIN_ROOT": str(ROOT)}

    result = subprocess.run([str(HOOK)], input="not json", text=True, env=env, capture_output=True, timeout=30)

    assert result.returncode == 0
    assert _tree(home) == set()


def test_hook_selftest_passes():
    result = subprocess.run([str(HOOK), "--selftest"], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stdout + result.stderr


def test_hooks_json_wires_only_the_stop_meter_script():
    hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]

    assert list(hooks) == ["Stop"]
    commands = [h["command"] for entry in hooks["Stop"] for h in entry["hooks"]]
    assert commands == ['"${CLAUDE_PLUGIN_ROOT}/hooks/stop-meter.sh"']
    assert (ROOT / "hooks" / "stop-meter.sh").exists()


def test_shipped_files_have_no_path_or_import_dependency_on_core():
    shipped = [p for p in ROOT.iterdir() if p.suffix in {".py", ".sh", ".json"} and not p.name.startswith("test_")]
    shipped += list((ROOT / "hooks").iterdir()) + list((ROOT / "commands").glob("*.md"))
    shipped += [ROOT / ".claude-plugin" / "plugin.json"]
    forbidden = [
        (re.compile(r"\.claude/cairn(?!-meter)"), "core's per-user folder"),
        (re.compile(r"known-projects"), "core's known-projects list"),
        (re.compile(r"cairn:start"), "core's marker"),
        (re.compile(r"mission-control"), "core's dashboard"),
        (re.compile(r"tokens\.db"), "core's token store"),
        (re.compile(r"^\s*(import|from)\s+(tasks|interfaces)\b", re.M), "a core module"),
    ]
    offences = []
    for path in shipped:
        text = path.read_text()
        for pattern, why in forbidden:
            if pattern.search(text):
                offences.append(f"{path.name}: {why}")
    assert offences == []
