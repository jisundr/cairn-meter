import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_usage  # noqa: E402

DOC = Path(__file__).resolve().parent / "docs" / "schemas" / "usage-v1.md"


def _section(text: str, heading: str) -> str:
    return re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^#{{2,3}} |\Z)", text, re.S | re.M).group(1)


def _keys(table_text: str) -> set[str]:
    return set(re.findall(r"^\| `(\w+)` \|", table_text, re.M))


def test_documented_example_satisfies_the_validator():
    example = re.search(r"```json\n(.*?)```", DOC.read_text(), re.S).group(1)

    assert export_usage.validate(json.loads(example)) == []


def test_documented_keys_match_what_the_exporter_writes(tmp_path):
    text = DOC.read_text()
    top_table = re.search(r"^## Document\n(.*?)^### Session entry", text, re.S | re.M).group(1)
    session_table = _section(text, "Session entry")
    project = tmp_path / "proj"
    project.mkdir()
    import db  # noqa: E402

    conn = db.connect(tmp_path / "store")
    db.save_session(conn, session_id="s1", project=str(project))
    db.insert_call(conn, dict(
        request_id="r1", session_id="s1", agent="main", model="claude-sonnet-5",
        timestamp="2026-10-12T10:00:00Z", input_tokens=1, output_tokens=1,
    ))
    conn.commit()
    conn.close()

    doc = export_usage.build(tmp_path / "store", project)

    assert _keys(top_table) == set(doc)
    assert _keys(session_table) == set(doc["sessions"][0])
