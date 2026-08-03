#!/usr/bin/env bash
# First-run setup. Idempotent — safe to re-run; it never overwrites a file
# you have already edited.
#
#   ./bin/setup.sh
#
# Creates config.yaml and the local MCP wiring from the .example templates,
# then tells you what still needs a human decision.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

copied=0
copy_if_absent() {
  local src="$1" dst="$2"
  if [[ -e "$dst" ]]; then
    echo "  = $dst already exists, left alone"
  else
    cp "$src" "$dst"
    echo "  + created $dst"
    copied=1
  fi
}

echo "Setting up Dreamer in $ROOT"
echo
echo "config:"
copy_if_absent config.example.yaml config.yaml
echo
echo "MCP wiring (optional — only needed to query the vault from a chat client):"
copy_if_absent .mcp.json.example .mcp.json
mkdir -p .qmd
copy_if_absent .qmd/index.yml.example .qmd/index.yml

echo
echo "checking dependencies:"
missing=0
check() {
  if command -v "$1" >/dev/null 2>&1; then
    echo "  ok   $1"
  else
    echo "  MISSING  $1 — $2"
    missing=1
  fi
}
check python3 "required"
check git     "required: the vault's history is the audit trail"
check claude  "required for research: https://claude.com/claude-code"
check qmd     "optional: local semantic search over your corpus (https://github.com/pirate/qmd)"
check flock   "recommended: the job lock (util-linux); jobs degrade to unsynchronised without it"

if ! python3 -c "import yaml" 2>/dev/null; then
  echo "  MISSING  python yaml — pip install pyyaml"
  missing=1
else
  echo "  ok   python yaml"
fi

echo
echo "next:"
echo "  1. edit config.yaml — at minimum set corpora.claude_export to your"
echo "     conversation export, or plan to drop transcripts into vault/inbox"
echo "     yourself. Leave decay.go_live_date as null until you have run a"
echo "     backfill and are happy with the results."
echo "  2. ./bin/verify.sh          # confirm the install is sound"
echo "  3. ./bin/ingest.sh          # convert an export into transcripts"
echo "  4. ./bin/nightly-extract.sh # find your first loops"
echo
echo "  Read docs/getting-started.md for the full walkthrough."
[[ $missing -eq 1 ]] && echo && echo "Resolve the MISSING items above first."
exit 0
