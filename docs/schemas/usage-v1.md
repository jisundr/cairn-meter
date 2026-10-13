# usage.json, schema 1

The contract for the file cairn-meter exports: token counts and cost per session for one project. Other tools read it to show cost next to their own data; cairn-meter is the only writer. The executable check is `export_usage.validate`, pinned to this document by `test_export_usage.py`.

## Scope

- Covers `<project>/.cairn/export/usage.json`.
- Describes the file only. cairn-meter's own store (`~/.claude/cairn-meter/meter.db`) is private and not part of this contract.
- Reads nothing from other tools. A reader needs this document and nothing else from cairn-meter.

## Location and writes

- Path: `<project>/.cairn/export/usage.json`, where `<project>` is the directory a session ran in.
- Written atomically: a temp file in the same folder, then a rename. A reader sees the previous complete file or the new complete file, not a partial one.
- Written when a session stops, and only into a project that already has a `.cairn/` directory. An explicit run (`python3 export_usage.py [project_dir]`, or `/cairn-meter export`) also creates the folder.
- Rewritten whole on each write. It reflects the store at that moment, so a session that is still running shows the totals up to its last stop.

## Document

A single JSON object.

| Key | Type | Meaning |
|---|---|---|
| `schema` | int | Schema version. 1 here. |
| `generated` | string | When this file was written: UTC ISO 8601, `YYYY-MM-DDTHH:MM:SSZ`. |
| `project` | string | Absolute path of the project directory the sessions ran in. |
| `currency` | string | `"USD"`. |
| `prices_as_of` | string or null | Date (`YYYY-MM-DD`) the rate table was last checked. Null when the table carries no date. |
| `sessions` | list | One entry per session recorded for this project, ordered by `started` then `session_id`. Empty when there are none. |

### Session entry

| Key | Type | Meaning |
|---|---|---|
| `session_id` | string | Claude Code's session id, the same id core's `tasks.json` lists under `sessions[].session_id`. |
| `started` | string | Timestamp of the session's first recorded call, UTC ISO 8601 ending `Z`. |
| `ended` | string | Timestamp of its last recorded call, same form. |
| `models` | list of strings | Distinct models the session called, sorted. Includes unpriced ones. |
| `input_tokens` | int | Sum of uncached input tokens. |
| `output_tokens` | int | Sum of output tokens. |
| `cache_read_tokens` | int | Sum of tokens read from the prompt cache. |
| `cache_write_tokens` | int | Sum of tokens written to the prompt cache, 5 minute and 1 hour entries together. |
| `cost_usd` | number or null | Price of the session at the table's rates, rounded to 6 decimals. Null when any model used has no complete rate. |
| `unpriced_models` | list of strings | The models that made `cost_usd` null, sorted. Empty exactly when `cost_usd` is a number. |

Token counts cover the whole session: the main conversation and its subagents.

## Cost rules

- A session's cost is the sum over its calls of tokens times the model's rate, for input, output, cache read, cache write 5 minute and cache write 1 hour.
- A call on a model with no rate, or with an incomplete rate set, is unpriced. One unpriced call makes the whole session's `cost_usd` null and puts the model in `unpriced_models`. The file carries no partial sum.
- A reader adding costs across sessions treats any null as unknown, and does not skip it.
- Rates live in cairn-meter's `prices.json` and are applied when the file is written, so a later price change shows on the next write.

## Example

```json
{
  "schema": 1,
  "generated": "2026-10-13T09:30:00Z",
  "project": "/Users/me/work/app",
  "currency": "USD",
  "prices_as_of": "2026-10-11",
  "sessions": [
    {
      "session_id": "0b6e5f1a-2c3d-4e5f-8a9b-1c2d3e4f5a6b",
      "started": "2026-10-12T10:00:00Z",
      "ended": "2026-10-12T11:30:00Z",
      "models": ["claude-sonnet-5"],
      "input_tokens": 1000000,
      "output_tokens": 100000,
      "cache_read_tokens": 500000,
      "cache_write_tokens": 300000,
      "cost_usd": 4.0,
      "unpriced_models": []
    }
  ]
}
```

## Change policy

- Adding an optional key keeps `schema` at 1. Readers ignore keys they do not know.
- Removing a key, changing a type or changing what a value means raises `schema`. cairn-meter writes the new number only after this document describes it.
- A reader that sees a `schema` it does not know treats the file as absent.
