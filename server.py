#!/usr/bin/env python3
"""Local dashboard server for cairn-meter. stdlib only.

Binds localhost only, runs in the foreground, stops on Ctrl-C. Serves a
JSON API over `db.py`'s tables (rollups by day/session/agent/model/tool/
skill/MCP-server, per-session call traces, an on-demand prompt/response
lookup) plus the compiled `static/` frontend, with a catch-all to
`index.html` for its client-side `/sessions/<id>` route. Prices are applied
at read time via `pricing.py`; this module never writes to `meter.db`.

Usage:
    python3 server.py [port]
"""
import http.server
import json
import mimetypes
import re
import socket
import sqlite3
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db  # noqa: E402
import pricing  # noqa: E402

STATIC_DIR_NAME = "static"
DEFAULT_PORT = 4318
DEFAULT_HOST = "127.0.0.1"
UNKNOWN_PROJECT_LABEL = "(unknown)"

# Per-day chart range tabs. Rolling ranges are inclusive of today (e.g. "7d"
# spans today and the 6 days before it). "today" buckets by hour instead of
# by day; "month" is the current calendar month to date; "life" has no fixed
# window (it starts at the earliest captured call, or is empty if there are
# none yet). "13w" is Overview's Calendar view's fixed window (91 days = 13
# Monday-aligned weeks, matching its GitHub-style contribution grid) - never
# user-facing in RangeControl.tsx's own 6 options, only ContributionCalendar's
# internal fetch.
_ROLLING_DAY_COUNTS = {"7d": 7, "30d": 30, "6m": 182, "13w": 91}
VALID_RANGES = {"today", "7d", "30d", "month", "6m", "life", "13w"}


# --------------------------------------------------------------------------
# Projects: the distinct directories sessions ran in, read from the store.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    label: str
    root: Path
    parent: str | None = None


def _disambiguate_labels(roots: list[Path]) -> list[str]:
    """`root.name` for each root, except that two or more roots sharing the
    same name (e.g. `/org1/backend` and `/org2/backend`) get enough of
    their parent path prefixed - joined by "/" - to be unique among this
    set, rather than silently colliding on the same plain label. The
    common single-project (or no-collision) case keeps plain `root.name`.
    """
    names = [r.name for r in roots]
    counts: dict[str, int] = defaultdict(int)
    for name in names:
        counts[name] += 1
    if all(counts[name] == 1 for name in names):
        return names

    groups: dict[str, list[int]] = defaultdict(list)
    for i, name in enumerate(names):
        groups[name].append(i)

    labels = list(names)
    for name, indices in groups.items():
        if len(indices) == 1:
            continue
        depth = 2
        while True:
            candidates = ["/".join(roots[i].parts[-depth:]) for i in indices]
            exhausted = all(depth >= len(roots[i].parts) for i in indices)
            if len(set(candidates)) == len(candidates) or exhausted:
                for i, candidate in zip(indices, candidates):
                    labels[i] = candidate
                break
            depth += 1
    return labels


def _compute_parents(roots: list[Path], labels: list[str]) -> list[str | None]:
    """Each root's parent label: root `r`'s parent is the *other*
    root `p` such that `r` is a filesystem subdirectory of `p`,
    picking the longest-path (nearest/most specific) match when more than
    one other root contains `r` - e.g. `engine`/`site` under
    `ai-worth-carrying`. `None` when no other known root contains `r`.
    Compares only the given `roots` - no filesystem access.
    """
    parents: list[str | None] = []
    for i, r in enumerate(roots):
        containing = [j for j, p in enumerate(roots) if j != i and r != p and r.is_relative_to(p)]
        if not containing:
            parents.append(None)
            continue
        nearest = max(containing, key=lambda j: len(roots[j].parts))
        parents.append(labels[nearest])
    return parents


def projects_from_roots(roots: list[str | None]) -> list[Project]:
    """One `Project` per distinct root, in the given order. A session with no
    recorded project is grouped under `(unknown)`. Labels are the directory
    name, lengthened only where two roots would collide; `parent` is the
    nearest other root that contains this one.
    """
    seen: set[str] = set()
    paths: list[Path] = []
    for root in roots:
        key = root or UNKNOWN_PROJECT_LABEL
        if key in seen:
            continue
        seen.add(key)
        paths.append(Path(key))
    labels = _disambiguate_labels(paths)
    parents = _compute_parents(paths, labels)
    return [Project(label=label, root=root, parent=parent) for label, root, parent in zip(labels, paths, parents)]


def _filter_projects(projects: list[Project], project_filter: str | None) -> list[Project]:
    """`project_filter` matches a project whose own label equals it, or one
    of that project's descendants (goal 8's cascade) - walking `.parent`
    labels up from each candidate until it either reaches `project_filter`
    or runs out of known ancestors. A parent-rollup row's click therefore
    filters in its own sessions plus every child's, matching what the
    rolled-up total on that row already shows. `_compute_parents` derives
    `.parent` from filesystem containment, which can't produce a cycle (two
    roots can't each be a subdirectory of the other), so no cycle guard here.
    """
    if not project_filter or project_filter == "all":
        return projects
    by_label = {p.label: p for p in projects}

    def _matches(project: Project) -> bool:
        current: Project | None = project
        while current is not None:
            if current.label == project_filter:
                return True
            current = by_label.get(current.parent) if current.parent else None
        return False

    return [p for p in projects if _matches(p)]


# --------------------------------------------------------------------------
# Small pure helpers
# --------------------------------------------------------------------------


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _total_tokens(row: dict) -> int:
    return (
        row.get("input_tokens", 0)
        + row.get("output_tokens", 0)
        + row.get("cache_read_tokens", 0)
        + row.get("cache_write_5m_tokens", 0)
        + row.get("cache_write_1h_tokens", 0)
    )


def _seconds_between(ts1: str, ts2: str) -> float:
    return (datetime.fromisoformat(ts2) - datetime.fromisoformat(ts1)).total_seconds()


def resolve_range(range_key: str, now: datetime | None = None) -> tuple[str, str, str]:
    """(since, until, bucket) for a fixed-window range. `since`/`until` are
    ISO8601 UTC strings, a half-open [since, until) window. Raises
    ValueError for "life" (no fixed window - see `range_bounds`) or an
    unrecognized range key.
    """
    if range_key not in VALID_RANGES:
        raise ValueError(f"unknown range: {range_key}")
    if range_key == "life":
        raise ValueError('"life" has no fixed window; use range_bounds()')

    now = now or datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    until = _iso(now)

    if range_key == "today":
        return _iso(today_start), until, "hour"
    if range_key == "month":
        return _iso(today_start.replace(day=1)), until, "day"

    days = _ROLLING_DAY_COUNTS[range_key]
    since = _iso(today_start - timedelta(days=days - 1))
    return since, until, "day"


def range_bounds(range_key: str, now: datetime | None = None) -> tuple[str | None, str]:
    """(since, until) for any range, including "life" (since=None -> no
    lower bound; callers query for everything).
    """
    if range_key == "life":
        return None, _iso(now or datetime.now(timezone.utc))
    since, until, _bucket = resolve_range(range_key, now=now)
    return since, until


def _bucket_key(timestamp: str, bucket: str) -> str:
    return timestamp[:13] if bucket == "hour" else timestamp[:10]


def _bucket_range(since_iso: str, until_iso: str, bucket: str) -> list[str]:
    if bucket == "hour":
        start = datetime.strptime(since_iso[:13], "%Y-%m-%dT%H")
        end = datetime.strptime(until_iso[:13], "%Y-%m-%dT%H")
        step = timedelta(hours=1)
        fmt = "%Y-%m-%dT%H"
    else:
        start = datetime.strptime(since_iso[:10], "%Y-%m-%d")
        end = datetime.strptime(until_iso[:10], "%Y-%m-%d")
        step = timedelta(days=1)
        fmt = "%Y-%m-%d"

    keys = []
    cur = start
    while cur <= end:
        keys.append(cur.strftime(fmt))
        cur += step
    return keys


# --------------------------------------------------------------------------
# Rollups over already-fetched rows (pure - no I/O, directly testable)
# --------------------------------------------------------------------------


def rollup_group(rows: list[dict], key_fn) -> list[dict]:
    """Groups `calls`-shaped rows by `key_fn(row)`. Each group reports
    `cost: None` (never a silently partial sum) if any of its rows has an
    unpriced model.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[key_fn(row)].append(row)

    result = [
        {
            "key": key,
            "calls": len(group_rows),
            "tokens": sum(_total_tokens(r) for r in group_rows),
            "cost": pricing.group_cost(group_rows),
        }
        for key, group_rows in groups.items()
    ]
    result.sort(key=lambda g: g["tokens"], reverse=True)
    return result


def rollup_tool_group(rows: list[dict], key_fn) -> list[dict]:
    """Groups `tool_uses`-shaped rows by `key_fn(row)` into counts. Rows for
    which `key_fn` returns None are excluded (e.g. skill/MCP rollups filter
    to their own tool_name family).
    """
    counts = defaultdict(int)
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        counts[key] += 1

    result = [{"key": key, "count": count} for key, count in counts.items()]
    result.sort(key=lambda g: g["count"], reverse=True)
    return result


def _tool_key(row: dict):
    name = row["tool_name"]
    if name == "Skill" or name.startswith("mcp__"):
        return None
    return name


def _skill_key(row: dict):
    if row["tool_name"] != "Skill":
        return None
    # A genuine Skill row can still have detail=None (parser.py writes this
    # when the tool input lacks a "skill" key); bucket it explicitly rather
    # than returning None, which rollup_tool_group treats as "exclude".
    return row["detail"] if row["detail"] is not None else "unknown"


def _mcp_key(row: dict):
    name = row["tool_name"]
    if not name.startswith("mcp__"):
        return None
    parts = name.split("__", 2)
    return parts[1] if len(parts) >= 2 else name


def rollup_timeseries(rows: list[dict], since: str, until: str, bucket: str) -> list[dict]:
    """One point per bucket between `since` (inclusive) and `until`
    (inclusive of its own bucket), zero-filled for buckets with no calls -
    a continuous chart, never gappy. `by_model` (on request, for the Trends
    stacked bar chart) reuses `rollup_group` per bucket - same shape/sort
    (descending tokens) `day_detail()`'s own `by_model` already returns, an
    empty list rather than an error for a zero-call bucket.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[_bucket_key(row["timestamp"], bucket)].append(row)

    points = []
    for key in _bucket_range(since, until, bucket):
        group_rows = grouped.get(key, [])
        points.append(
            {
                "bucket": key,
                "calls": len(group_rows),
                "tokens": sum(_total_tokens(r) for r in group_rows),
                "cost": pricing.group_cost(group_rows),
                "by_model": rollup_group(group_rows, key_fn=lambda r: r["model"]),
            }
        )
    return points


def rollup_sessions(
    calls: list[dict],
    events: list[dict],
    labels: dict[str, str] | None = None,
) -> list[dict]:
    """One row per (project, session_id), most recently started first.
    `usage_limit_hit` cross-references `usage_limit_events` distinctly -
    it is never folded into the token/cost totals here. `labels` (keyed by
    session_id, from `db.py`'s `session_labels` table) is optional and may
    omit a session entirely - such a session's `label` just comes through
    empty rather than erroring.    """
    labels = labels or {}
    limited = {(e["project"], e["session_id"]) for e in events}
    sessions = defaultdict(list)
    for row in calls:
        sessions[(row["project"], row["session_id"])].append(row)

    result = []
    for (project_label, session_id), group_rows in sessions.items():
        timestamps = [r["timestamp"] for r in group_rows]
        result.append(
            {
                "session_id": session_id,
                "project": project_label,
                "started": min(timestamps),
                "ended": max(timestamps),
                "agents": sorted({r["agent"] if r["agent"] is not None else "unknown" for r in group_rows}),
                "calls": len(group_rows),
                "tokens": sum(_total_tokens(r) for r in group_rows),
                "cost": pricing.group_cost(group_rows),
                "usage_limit_hit": (project_label, session_id) in limited,
                "label": labels.get(session_id, ""),
            }
        )
    result.sort(key=lambda s: s["started"], reverse=True)
    return result


def build_session_trace(session_id: str, calls: list[dict], label: str | None = None) -> dict | None:
    """Agent groups (ordered by each agent's first call), each with its
    calls in chronological order. A call's `duration_seconds` is the gap
    to the next call by the *same agent* in this session (there being no
    captured call duration) - the last call in each agent's sequence has
    no next call, so it's None. Returns None if the session has no calls.
    `label` (from `db.py`'s `session_labels` table) is optional and comes
    through empty when the session has no saved label yet.
    """
    if not calls:
        return None

    calls = sorted(calls, key=lambda r: (r["timestamp"], r["request_id"]))
    global_position = {row["request_id"]: i + 1 for i, row in enumerate(calls)}

    by_agent = defaultdict(list)
    for row in calls:
        by_agent[row["agent"]].append(row)

    ordered_agents = sorted(by_agent.items(), key=lambda kv: kv[1][0]["timestamp"])
    agents_out = []
    for agent, agent_calls in ordered_agents:
        trace = []
        for i, row in enumerate(agent_calls):
            next_row = agent_calls[i + 1] if i + 1 < len(agent_calls) else None
            duration = _seconds_between(row["timestamp"], next_row["timestamp"]) if next_row else None
            trace.append(
                {
                    "position": i + 1,
                    "global_position": global_position[row["request_id"]],
                    "request_id": row["request_id"],
                    "timestamp": row["timestamp"],
                    "model": row["model"],
                    "input_tokens": row["input_tokens"],
                    "output_tokens": row["output_tokens"],
                    "cache_read_tokens": row["cache_read_tokens"],
                    "cache_write_5m_tokens": row["cache_write_5m_tokens"],
                    "cache_write_1h_tokens": row["cache_write_1h_tokens"],
                    "cost": pricing.call_cost(row),
                    "duration_seconds": duration,
                }
            )
        agents_out.append(
            {
                "agent": agent,
                "calls": len(agent_calls),
                "tokens": sum(_total_tokens(r) for r in agent_calls),
                "cost": pricing.group_cost(agent_calls),
                "trace": trace,
            }
        )

    return {
        "session_id": session_id,
        "started": calls[0]["timestamp"],
        "ended": calls[-1]["timestamp"],
        "agents": agents_out,
        "label": label or "",
    }


# --------------------------------------------------------------------------
# Transcript lookup: on-demand prompt/response for one call
# --------------------------------------------------------------------------


def encode_project_path(root: Path) -> str:
    # Claude Code swaps both "/" and "." for "-" when deriving a project's
    # folder name under ~/.claude/projects/ - a path segment with a dot in it
    # (e.g. this repo's own "cairn-2.0") otherwise encodes to the wrong
    # folder name, so its transcripts are never found.
    return str(Path(root).resolve()).replace("/", "-").replace(".", "-")


def transcript_path_for(claude_projects_dir: Path, project_root: Path, session_id: str) -> Path:
    """Where a session's main transcript lives, following Claude Code's own
    layout (`~/.claude/projects/<dashed-project-path>/<session_id>.jsonl`) -
    the fallback for a session whose stored `transcript_path` is gone.
    """
    return Path(claude_projects_dir) / encode_project_path(project_root) / f"{session_id}.jsonl"


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts)
    return ""


def _is_tool_result_only(content) -> bool:
    """True for a role="user" message whose content is exclusively
    tool_result blocks (the standard shape tool results are delivered in) -
    i.e. it carries no actual human-authored prompt text.
    """
    if not isinstance(content, list) or not content:
        return False
    return all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)


# Per-tool-name field to pull a short human-readable summary from a
# tool_use block's `input`, mirroring parser.py's `detail = input.get("skill")
# if name == "Skill"` pattern. Falls back to "" for unmapped tools or a
# missing/non-string field.
_TOOL_SUMMARY_FIELD = {
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "NotebookEdit": "file_path",
    "Bash": "command",
    "Grep": "pattern",
    "Glob": "pattern",
    "WebFetch": "url",
    "Task": "description",
    "Skill": "skill",
}


def _tool_use_summary(name, tool_input) -> str:
    field = _TOOL_SUMMARY_FIELD.get(name)
    if field is None or not isinstance(tool_input, dict):
        return ""
    value = tool_input.get(field)
    return value if isinstance(value, str) else ""


def _extract_call_content(entries: list[dict], request_id: str) -> tuple[str, str, list[dict]] | None:
    """(prompt, response, tool_calls) for the entries sharing `request_id`,
    or None if that request_id isn't present. `response` concatenates every
    text block across all entries sharing the id (one call can span several
    entries - see `parser.py`); `prompt` is the nearest preceding user-role
    entry; `tool_calls` is one `{name, summary}` object per `tool_use` block,
    in encounter order.
    """
    response_parts = []
    tool_calls = []
    first_index = None
    for i, entry in enumerate(entries):
        if entry.get("requestId") != request_id:
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        if first_index is None:
            first_index = i
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    response_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    name = block.get("name")
                    tool_calls.append({"name": name, "summary": _tool_use_summary(name, block.get("input"))})

    if first_index is None:
        return None

    prompt_text = ""
    for i in range(first_index - 1, -1, -1):
        message = entries[i].get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if _is_tool_result_only(content):
            # A tool_result echo, not a real prompt - keep walking backward.
            continue
        prompt_text = _tool_result_text(content)
        break

    return prompt_text, "\n".join(response_parts), tool_calls


def lookup_transcript_content(
    transcript_path: Path, request_id: str
) -> tuple[str | None, str | None, list[dict], bool]:
    """(prompt, response, tool_calls, available). available=False
    (prompt/response None, tool_calls []) if the transcript file is missing
    or doesn't contain this request_id - never raises.
    """
    transcript_path = Path(transcript_path)
    found = _extract_call_content(_load_jsonl(transcript_path), request_id)
    if found is not None:
        return found[0], found[1], found[2], True

    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    if subagents_dir.is_dir():
        for subagent_path in sorted(subagents_dir.glob("agent-*.jsonl")):
            found = _extract_call_content(_load_jsonl(subagent_path), request_id)
            if found is not None:
                return found[0], found[1], found[2], True

    return None, None, [], False


# --------------------------------------------------------------------------
# App: wires the pure rollups above to sqlite reads of the one store
# --------------------------------------------------------------------------


def _open_readonly(db_path: Path, table: str) -> sqlite3.Connection | None:
    """A read-only connection to `db_path`, or None if it's missing or
    doesn't yet have `table` (cold start: no `Stop` event has fired yet).
    """
    if not db_path.exists():
        return None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1")
    except sqlite3.OperationalError:
        conn.close()
        return None
    return conn


# Discovery-cache TTL: `projects()` reads the store, so one request cycle
# that calls it more than once (several rollup routes touched by one page
# load) shouldn't repeat the query. 5s keeps a new project from lingering
# unseen, while collapsing same-cycle redundant calls.
_DISCOVERY_CACHE_TTL_SECONDS = 5


class MeterApp:
    """Query layer, independent of HTTP. `handle_api` is the thin dispatch
    layer `Handler` (the socket-facing class below) delegates to.
    """

    def __init__(
        self,
        store_dir: Path | None = None,
        *,
        claude_projects_dir: Path | None = None,
        static_dir: Path | None = None,
        discovery_cache_ttl: float = _DISCOVERY_CACHE_TTL_SECONDS,
    ):
        self.store_dir = Path(store_dir) if store_dir else db.DEFAULT_STORE_DIR
        self.claude_projects_dir = Path(claude_projects_dir) if claude_projects_dir else db.DEFAULT_CLAUDE_PROJECTS_DIR
        self.static_dir = Path(static_dir) if static_dir else Path(__file__).resolve().parent / STATIC_DIR_NAME
        self.discovery_cache_ttl = discovery_cache_ttl
        self._discovery_cache: tuple[float, list[Project]] | None = None

    @property
    def db_file(self) -> Path:
        return db.db_path(self.store_dir)

    def projects(self) -> list[Project]:
        if self._discovery_cache is not None:
            cached_at, cached_projects = self._discovery_cache
            if time.monotonic() - cached_at < self.discovery_cache_ttl:
                return cached_projects
        roots: list[str | None] = []
        conn = _open_readonly(self.db_file, "calls")
        if conn is not None:
            try:
                rows = conn.execute(
                    "SELECT DISTINCT s.project FROM (SELECT DISTINCT session_id FROM calls) c "
                    "LEFT JOIN sessions s ON s.session_id = c.session_id ORDER BY s.project"
                ).fetchall()
                roots = [row[0] for row in rows]
            finally:
                conn.close()
        fresh = projects_from_roots(roots)
        self._discovery_cache = (time.monotonic(), fresh)
        return fresh

    def _label_by_root(self) -> dict[str, str]:
        return {str(p.root): p.label for p in self.projects()}

    def _allowed_labels(self, project_filter: str | None) -> set[str] | None:
        """None when no filter applies (every project); else the matching labels."""
        if not project_filter or project_filter == "all":
            return None
        return {p.label for p in _filter_projects(self.projects(), project_filter)}

    # -- fetch (I/O) --------------------------------------------------

    def _fetch_table(self, table: str, allowed: set[str] | None, since=None, until=None, session_id=None) -> list[dict]:
        conn = _open_readonly(self.db_file, table)
        if conn is None:
            return []
        labels = self._label_by_root()
        rows = []
        try:
            query = (
                f"SELECT t.*, COALESCE(s.project, '') AS _project FROM {table} t "
                "LEFT JOIN sessions s ON s.session_id = t.session_id"
            )
            clauses, params = [], []
            # Compare only whole-second precision on both sides: captured
            # timestamps commonly carry sub-second fractions (e.g.
            # "...:00.500Z"), and lexicographic comparison against a
            # whole-second bound fails at the boundary because "." sorts
            # before "Z"/digits. Truncating both avoids that.
            if since is not None:
                clauses.append("substr(t.timestamp, 1, 19) >= ?")
                params.append(since[:19])
            if until is not None:
                clauses.append("substr(t.timestamp, 1, 19) < ?")
                params.append(until[:19])
            if session_id is not None:
                clauses.append("t.session_id = ?")
                params.append(session_id)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            for row in conn.execute(query, params):
                record = dict(row)
                root = record.pop("_project") or UNKNOWN_PROJECT_LABEL
                record["project"] = labels.get(root, root)
                if allowed is not None and record["project"] not in allowed:
                    continue
                rows.append(record)
        finally:
            conn.close()
        return rows

    def _fetch_session_labels(self) -> dict[str, str]:
        """Every saved label, keyed by session_id. A store with none saved yet
        contributes nothing - never an error."""
        conn = _open_readonly(self.db_file, "session_labels")
        if conn is None:
            return {}
        try:
            return {r["session_id"]: r["label"] for r in conn.execute("SELECT session_id, label FROM session_labels")}
        finally:
            conn.close()

    def _ranged_calls(self, range_key: str, project_filter: str | None) -> list[dict]:
        since, until = range_bounds(range_key)
        return self._fetch_table("calls", self._allowed_labels(project_filter), since=since, until=until)

    def _ranged_tool_uses(self, range_key: str, project_filter: str | None) -> list[dict]:
        since, until = range_bounds(range_key)
        return self._fetch_table("tool_uses", self._allowed_labels(project_filter), since=since, until=until)

    # -- rollup endpoints ----------------------------------------------

    def timeseries(self, range_key: str, project_filter: str | None = None, now: datetime | None = None) -> dict:
        if range_key not in VALID_RANGES:
            raise ValueError(f"unknown range: {range_key}")
        now = now or datetime.now(timezone.utc)
        since, until = range_bounds(range_key, now=now)
        bucket = "hour" if range_key == "today" else "day"

        rows = self._fetch_table("calls", self._allowed_labels(project_filter), since=since, until=until)
        if range_key == "life":
            points = [] if not rows else rollup_timeseries(rows, min(r["timestamp"] for r in rows)[:10] + "T00:00:00Z", until, bucket)
            if points:
                since = min(r["timestamp"] for r in rows)[:10] + "T00:00:00Z"
        else:
            points = rollup_timeseries(rows, since, until, bucket)

        total_tokens = sum(p["tokens"] for p in points)
        if points and any(p["cost"] is None for p in points):
            total_cost = None
        else:
            total_cost = round(sum(p["cost"] for p in points), 6) if points else 0.0

        return {
            "range": range_key,
            "bucket": bucket,
            "since": since,
            "until": until,
            "points": points,
            "total_tokens": total_tokens,
            "total_cost": total_cost,
        }

    def day_detail(self, date_str: str, project_filter: str | None = None) -> dict:
        """`by_tool` is call-count share only (`rollup_tool_group`, same as
        `tool_rollup`) - `tool_uses` rows carry no cost of their own, and
        there's no join back to the `calls` row(s) a tool invocation belongs
        to that would let a call's cost be attributed to its tool(s) (see
        By-tools' Summary note in PLAN.md). `by_agent` reuses the same
        `calls` rows `by_model` already fetched - exact cost/tokens, like
        `agent_rollup`.
        """
        allowed = self._allowed_labels(project_filter)
        day_start = datetime.strptime(date_str, "%Y-%m-%d")
        since = _iso(day_start)
        until = _iso(day_start + timedelta(days=1))
        rows = self._fetch_table("calls", allowed, since=since, until=until)
        by_model = rollup_group(rows, key_fn=lambda r: r["model"])
        by_agent = rollup_group(rows, key_fn=lambda r: r["agent"])
        by_tool = rollup_tool_group(self._fetch_table("tool_uses", allowed, since=since, until=until), key_fn=_tool_key)
        any_unknown = any(g["cost"] is None for g in by_model)
        return {
            "date": date_str,
            "total_tokens": sum(g["tokens"] for g in by_model),
            "total_cost": None if any_unknown else round(sum(g["cost"] for g in by_model), 6),
            "by_model": by_model,
            "by_tool": by_tool,
            "by_agent": by_agent,
        }

    def agent_rollup(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        return rollup_group(self._ranged_calls(range_key, project_filter), key_fn=lambda r: r["agent"])

    def model_rollup(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        return rollup_group(self._ranged_calls(range_key, project_filter), key_fn=lambda r: r["model"])

    def tool_rollup(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        return rollup_tool_group(self._ranged_tool_uses(range_key, project_filter), key_fn=_tool_key)

    def skill_rollup(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        return rollup_tool_group(self._ranged_tool_uses(range_key, project_filter), key_fn=_skill_key)

    def mcp_rollup(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        return rollup_tool_group(self._ranged_tool_uses(range_key, project_filter), key_fn=_mcp_key)

    def usage_limit_events(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        since, until = range_bounds(range_key)
        return self._fetch_table(
            "usage_limit_events", self._allowed_labels(project_filter), since=since, until=until
        )

    def sessions(self, range_key: str, project_filter: str | None = None) -> list[dict]:
        allowed = self._allowed_labels(project_filter)
        since, until = range_bounds(range_key)
        calls = self._fetch_table("calls", allowed, since=since, until=until)
        events = self._fetch_table("usage_limit_events", allowed, since=since, until=until)
        return rollup_sessions(calls, events, self._fetch_session_labels())

    def session_trace(self, session_id: str, project_filter: str | None = None) -> dict | None:
        calls = self._fetch_table("calls", self._allowed_labels(project_filter), session_id=session_id)
        return build_session_trace(session_id, calls, self._fetch_session_labels().get(session_id))

    def _transcript_for(self, session_id: str, project_label: str) -> Path | None:
        """The session's transcript: the path recorded at capture time if it
        still exists, else Claude Code's own layout under the project's root."""
        conn = _open_readonly(self.db_file, "sessions")
        if conn is not None:
            try:
                row = conn.execute(
                    "SELECT transcript_path FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
            finally:
                conn.close()
            if row and row["transcript_path"] and Path(row["transcript_path"]).is_file():
                return Path(row["transcript_path"])
        project = next((p for p in self.projects() if p.label == project_label), None)
        if project is not None and project.label != UNKNOWN_PROJECT_LABEL:
            return transcript_path_for(self.claude_projects_dir, project.root, session_id)
        return None

    def call_detail(self, session_id: str, n: int, project_filter: str | None = None) -> dict | None:
        calls = self._fetch_table("calls", self._allowed_labels(project_filter), session_id=session_id)
        if not calls:
            return None
        calls.sort(key=lambda r: (r["timestamp"], r["request_id"]))
        if n < 1 or n > len(calls):
            return None

        call = calls[n - 1]
        prompt = response = None
        tool_calls: list[dict] = []
        available = False
        transcript = self._transcript_for(session_id, call["project"])
        if transcript is not None:
            prompt, response, tool_calls, available = lookup_transcript_content(transcript, call["request_id"])

        return {
            "position": n,
            "total": len(calls),
            "session_id": session_id,
            "project": call["project"],
            "agent": call["agent"],
            "request_id": call["request_id"],
            "timestamp": call["timestamp"],
            "model": call["model"],
            "input_tokens": call["input_tokens"],
            "output_tokens": call["output_tokens"],
            "cache_read_tokens": call["cache_read_tokens"],
            "cache_write_5m_tokens": call["cache_write_5m_tokens"],
            "cache_write_1h_tokens": call["cache_write_1h_tokens"],
            "cost": pricing.call_cost(call),
            "available": available,
            "prompt": prompt,
            "response": response,
            "tool_calls": tool_calls,
        }

    # -- HTTP dispatch (thin layer over everything above) ----------------

    _SESSION_TRACE_RE = re.compile(r"^/api/session/(?P<session_id>[^/]+)/trace$")
    _CALL_DETAIL_RE = re.compile(r"^/api/call/(?P<session_id>[^/]+)/(?P<n>\d+)$")

    def _envelope(self, data) -> dict:
        return {"data": data, "meta": {"generated_at": _iso(datetime.now(timezone.utc))}}

    def handle_api(self, path: str, query: dict) -> tuple[int, dict]:
        def first(key, default=None):
            values = query.get(key)
            return values[0] if values else default

        if path == "/api/projects":
            return 200, {
                "data": {
                    "hostname": socket.gethostname(),
                    "projects": [{"label": p.label, "parent": p.parent} for p in self.projects()],
                },
                "meta": {"generated_at": _iso(datetime.now(timezone.utc))},
            }

        range_key = first("range", "7d")
        project_filter = first("project")

        if range_key not in VALID_RANGES:
            return 400, {"error": "unknown range", "valid_ranges": sorted(VALID_RANGES)}

        try:
            if path == "/api/rollup/timeseries":
                return 200, self._envelope(self.timeseries(range_key, project_filter=project_filter))
            if path == "/api/rollup/day-detail":
                date_str = first("date")
                if not date_str:
                    return 400, {"error": "date query parameter is required"}
                return 200, self._envelope(self.day_detail(date_str, project_filter=project_filter))
            if path == "/api/rollup/session":
                return 200, self._envelope(self.sessions(range_key, project_filter=project_filter))
            if path == "/api/rollup/agent":
                return 200, self._envelope(self.agent_rollup(range_key, project_filter=project_filter))
            if path == "/api/rollup/model":
                return 200, self._envelope(self.model_rollup(range_key, project_filter=project_filter))
            if path == "/api/rollup/tool":
                return 200, self._envelope(self.tool_rollup(range_key, project_filter=project_filter))
            if path == "/api/rollup/skill":
                return 200, self._envelope(self.skill_rollup(range_key, project_filter=project_filter))
            if path == "/api/rollup/mcp-server":
                return 200, self._envelope(self.mcp_rollup(range_key, project_filter=project_filter))
            if path == "/api/usage-limit-events":
                return 200, self._envelope(self.usage_limit_events(range_key, project_filter=project_filter))
        except ValueError as exc:
            return 400, {"error": str(exc)}

        match = self._SESSION_TRACE_RE.match(path)
        if match:
            data = self.session_trace(match.group("session_id"), project_filter=project_filter)
            if data is None:
                return 404, {"error": "session not found"}
            return 200, self._envelope(data)

        match = self._CALL_DETAIL_RE.match(path)
        if match:
            data = self.call_detail(match.group("session_id"), int(match.group("n")), project_filter=project_filter)
            if data is None:
                return 404, {"error": "call not found"}
            return 200, self._envelope(data)

        return 404, {"error": "unknown route"}


# --------------------------------------------------------------------------
# HTTP: thin stdlib socket layer over MeterApp
# --------------------------------------------------------------------------


def _safe_static_path(static_dir: Path, request_path: str) -> Path | None:
    rel = request_path.lstrip("/")
    if not rel:
        return None
    try:
        candidate = (static_dir / rel).resolve()
    except (OSError, RuntimeError, ValueError):  # a symlink loop or NUL falls back, not crashes
        return None
    try:
        candidate.relative_to(static_dir.resolve())
    except ValueError:
        return None
    return candidate


_PLACEHOLDER_HTML = (
    "<!doctype html><html><body>"
    "<p>cairn-meter dashboard: static frontend not built yet.</p>"
    "<p>Run <code>npm run build</code> in frontend/ to generate static/.</p>"
    "</body></html>"
).encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    app: "MeterApp" = None

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            status, body = self.app.handle_api(parsed.path, parse_qs(parsed.query))
            self._write_json(status, body)
            return
        self._serve_static_or_fallback(parsed.path)

    def _serve_static_or_fallback(self, path: str):
        static_dir = self.app.static_dir
        candidate = _safe_static_path(static_dir, path) if static_dir.is_dir() else None
        if candidate is not None and candidate.is_file():
            self._write_file(candidate)
            return

        index = static_dir / "index.html"
        if index.is_file():
            self._write_file(index)
            return

        self._write_html(200, _PLACEHOLDER_HTML)

    def _write_json(self, status: int, body: dict):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_html(self, status: int, payload: bytes):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _write_file(self, path: Path):
        content = path.read_bytes()
        content_type, _ = mimetypes.guess_type(str(path))
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        pass


def make_handler(app: MeterApp) -> type:
    return type("BoundHandler", (Handler,), {"app": app})


# --------------------------------------------------------------------------
# start()/stop() (free functions); `run`/`main` are thin foreground wrappers.
# --------------------------------------------------------------------------

_server_lock = threading.Lock()
_server_state: dict = {}


def start(
    store_dir: Path | None = None,
    host: str = DEFAULT_HOST,
    port: int = 0,
    *,
    claude_projects_dir: Path | None = None,
    backfill_enabled: bool = True,
) -> int:
    """Start serving `store_dir` in a background daemon thread and return the
    bound port. A server already running under this module is stopped first.

    Once the HTTP server is bound, kicks off `backfill.run()` on its own
    daemon thread - the bound port is returned immediately, never waiting on
    however long backfill takes. `backfill` is imported here, not at module
    top, so importing `server` stays free of it. `backfill_enabled=False`
    lets a caller (tests indifferent to backfill) opt out.
    """
    stop()
    app = MeterApp(store_dir, claude_projects_dir=claude_projects_dir)
    handler_cls = make_handler(app)
    httpd = http.server.ThreadingHTTPServer((host, port), handler_cls)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    with _server_lock:
        _server_state["httpd"] = httpd
        _server_state["thread"] = thread
    if backfill_enabled:
        import backfill  # noqa: E402

        threading.Thread(
            target=backfill.run,
            kwargs={"store_dir": app.store_dir, "claude_projects_dir": app.claude_projects_dir},
            daemon=True,
        ).start()
    return httpd.server_address[1]


def stop() -> None:
    """Stop a server previously started by `start`. A no-op if none is running."""
    with _server_lock:
        httpd = _server_state.pop("httpd", None)
        thread = _server_state.pop("thread", None)
    if httpd is None:
        return
    httpd.shutdown()
    httpd.server_close()
    if thread is not None:
        thread.join(timeout=2)


def run(store_dir: Path | None = None, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT):
    bound_port = start(store_dir, host=host, port=port)
    print(f"cairn-meter dashboard: http://{host}:{bound_port}")
    with _server_lock:
        thread = _server_state.get("thread")
    try:
        if thread is not None:
            thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        stop()


def main(argv: list[str] | None = None):
    argv = sys.argv[1:] if argv is None else argv
    port = int(argv[0]) if argv else DEFAULT_PORT
    run(port=port)


if __name__ == "__main__":
    main()
