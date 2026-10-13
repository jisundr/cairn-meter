# cairn-meter

Token and cost metering for Claude Code sessions, with a local dashboard. It records each session's tokens when the session stops, prices them from a rate table, and serves Overview, Sessions and per-session drilldown pages on `127.0.0.1:4318`.

cairn-meter works on its own. It reads Claude Code's own transcripts and needs no other plugin. When a project has a `.cairn/` folder it also writes `.cairn/export/usage.json`, which other tools can read to show cost next to their own data.

## Install

The plugin is listed in the cairn marketplace:

```
/plugin marketplace add jisundr/cairn-3.0-core
/plugin install cairn-meter@cairn-marketplace
```

To try a local checkout, run `claude --plugin-dir <path-to-this-repo>`.

Requires Python 3.11 or later on `PATH` as `python3`. There are no Python packages to install.

## Use

| Command | What it does |
|---|---|
| `/cairn-meter` | Starts the dashboard in the background and opens it. Default port 4318. |
| `/cairn-meter report` | Prints one row per session for the current project. Writes nothing. |
| `/cairn-meter export` | Writes `<project>/.cairn/export/usage.json`, creating `.cairn/` if needed. |

The Stop hook (`hooks/stop-meter.sh`) does the recording; there is nothing to run by hand.

The Stop hook exports `usage.json` only when the session's cwd is the project root that holds `.cairn/`; a session started in a subdirectory gets none (a v1 limitation). A session's project is pinned to the first cwd seen.

## What it writes

| Where | What | When |
|---|---|---|
| `~/.claude/cairn-meter/meter.db` | The store: calls, tool uses, session labels, and the project each session ran in. | Each Stop. |
| `~/.claude/cairn-meter/pricing-check-state.json` | Date of the last unpriced-model scan. | At most once a day. |
| `<project>/.cairn/export/usage.json` | Per-session tokens and cost for that project. Contract: [docs/schemas/usage-v1.md](docs/schemas/usage-v1.md). | Each Stop, only in a project that already has `.cairn/`; or on `/cairn-meter export`. |

Nothing is written inside a project that has no `.cairn/` folder, other than by `/cairn-meter export`. Nothing is read from or written to `~/.claude/cairn/`.

## History

`meter.db` starts empty. `backfill.py` rebuilds history from Claude Code's transcripts under `~/.claude/projects/`; the dashboard runs it in the background when it starts, and it can be run directly:

```
python3 backfill.py [projects_dir]
```

Each session's project comes from the working directory its transcript records. cairn-meter does not import a `tokens.db` from another tool.

## Prices

Rates live in `prices.json` (dollars per million tokens, with an `as_of` date in its `_meta` entry) and are applied when data is read or exported, so an edit takes effect without migrating anything. A session that used a model with no complete rate has no cost: the dashboard shows it as unknown and `usage.json` carries `cost_usd: null` with the model named in `unpriced_models`. Once a day the Stop hook names any such model on stderr.

## Layout

| Path | Role |
|---|---|
| `.claude-plugin/plugin.json` | Plugin manifest. |
| `hooks/` | `hooks.json` and `stop-meter.sh`. |
| `commands/cairn-meter.md` | The slash command. |
| `db.py`, `parser.py` | Store schema and the transcript walker. |
| `pricing.py`, `prices.json`, `pricing_check.py` | Cost lookup and the daily drift check. |
| `backfill.py` | Rebuilds history from transcripts. |
| `export_usage.py` | Writes and validates `usage.json`. |
| `server.py`, `static/` | Dashboard API and the built frontend. |
| `frontend/` | Frontend source (Vite, React). |
| `docs/schemas/usage-v1.md` | The `usage.json` contract. |

## Develop

```
python -m pytest                       # backend
cd frontend
npm ci
npm run build                          # writes ../static
npm test
npm run e2e                            # builds, then runs Playwright
```

`hooks/stop-meter.sh --selftest` runs the hook against a throwaway HOME and project.

## Origin

The metering code was split out of core's `mission-control/` directory at core commit `0d4e7a3` (cairn core 3.2.0 plus the budget-warnings trim). Kanban, task drawer and attention signaling stayed behind; the per-project `tokens.db` became one user-level `meter.db`.

## License

MIT. See [LICENSE](LICENSE).
