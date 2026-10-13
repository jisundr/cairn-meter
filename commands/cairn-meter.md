---
description: Opens the token and cost dashboard, prints a per-session report, or writes usage.json. Argument report or export.
---

Read the argument: none opens the dashboard, `report` prints the table, `export` writes the file.

**No argument: dashboard**

1. Start `python3 -u ${CLAUDE_PLUGIN_ROOT}/server.py` in the background via `Bash` with `run_in_background`. The `-u` flag keeps the readiness line in the captured output. An empty store needs no handling here: the dashboard renders an empty state until the first session stops.
2. Wait for the readiness line, `cairn-meter dashboard: http://<host>:<port>`, rather than a fixed sleep. The default port is 4318; pass a port as the script's argument to change it.
3. Open that URL in the default browser (`open` on macOS, `xdg-open` on Linux, `start` on Windows).
4. Report the URL and how to stop the server: it runs as a backgrounded task in this session, so stopping it means ending that task (report its task id).

**`report`**

Run `python3 ${CLAUDE_PLUGIN_ROOT}/export_usage.py --report <project-root>` with this session's project root as an absolute path, and show its output. It prints one row per session for that project and writes nothing.

**`export`**

Run `python3 ${CLAUDE_PLUGIN_ROOT}/export_usage.py <project-root>` with this session's project root as an absolute path. It writes `<project-root>/.cairn/export/usage.json` (contract: `docs/schemas/usage-v1.md`), creating `.cairn/` if the project has none. Report the path written.
