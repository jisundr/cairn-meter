#!/usr/bin/env bash
# Stop hook: records the session's tokens in ~/.claude/cairn-meter/meter.db and,
# when the project has a .cairn/ folder, refreshes .cairn/export/usage.json.
set -uo pipefail
here=${0%/*}
if [ "${1:-}" = --selftest ]; then
p=0;f=0;t(){ "$@"&&p=$((p+1))||f=$((f+1)); }
t bash -n "$0"
h=$(mktemp -d);w=$(mktemp -d);tp=$h/t.jsonl
echo '{"requestId":"r1","cwd":"'"$w"'","message":{"model":"claude-sonnet-5","usage":{"input_tokens":3,"output_tokens":2}}}' >"$tp"
in='{"session_id":"s1","transcript_path":"'"$tp"'","cwd":"'"$w"'"}'
HOME=$h CLAUDE_PLUGIN_ROOT="$here/.." "$0" <<<"$in" &>/dev/null
t [ -f "$h/.claude/cairn-meter/meter.db" ]
t [ ! -e "$w/.cairn" ]
mkdir "$w/.cairn"
HOME=$h CLAUDE_PLUGIN_ROOT="$here/.." "$0" <<<"$in" &>/dev/null
t [ -f "$w/.cairn/export/usage.json" ]
t [ "$(ls "$h/.claude")" = cairn-meter ]
rm -rf "$h" "$w"
echo "selftest: $p passed, $f failed"
exit $((f>0))
fi
pr=${CLAUDE_PLUGIN_ROOT:-$here/..}
python3 -c '
import json,sys
from pathlib import Path
pr=sys.argv[1]
try:d=json.load(sys.stdin);si,tp,cw=d["session_id"],d["transcript_path"],d["cwd"]
except Exception:sys.exit(0)
sys.path.insert(0,pr)
import db
s=db.DEFAULT_STORE_DIR
try:
 import parser;parser.parse_session(s,Path(tp),si,cw)
except Exception:pass
try:
 import export_usage;export_usage.write_if_cairn(s,Path(cw))
except Exception:pass
try:
 import pricing_check;pricing_check.maybe_run(s)
except Exception:pass
' "$pr" >/dev/null
exit 0
