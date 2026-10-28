#!/usr/bin/env bash
# Register the Dreamer Sync native-messaging host with Chrome. Idempotent;
# run it once, then follow the printed steps. It writes ONE file (the host
# manifest) and nothing else: it does not touch Chrome's profile, the
# extension, or cron.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The extension connects to the host name compiled into background.js, so that
# constant is the only name a manifest can usefully carry.
EXT_HOST="$(sed -n 's/^const HOST = "\([^"]*\)";.*/\1/p' "$ROOT/extension/dreamer-sync/background.js")"
[[ -n "$EXT_HOST" ]] || { echo "cannot read the host name from $ROOT/extension/dreamer-sync/background.js" >&2; exit 1; }
read -r HOST_NAME EXT_ID < <(python3 - "$ROOT/config.yaml" "$EXT_HOST" <<'PY'
import sys, yaml
c = yaml.safe_load(open(sys.argv[1])).get("claude_ai_sync") or {}
print(c.get("host_name") or sys.argv[2], c.get("extension_id") or "")
PY
)
if [[ "$HOST_NAME" != "$EXT_HOST" ]]; then
  cat >&2 <<MSG
config.yaml claude_ai_sync.host_name is "$HOST_NAME", but the extension connects to
"$EXT_HOST" (extension/dreamer-sync/background.js). A manifest under any other
name is never reached. Nothing was written. Set claude_ai_sync.host_name to
$EXT_HOST, or remove the key.
MSG
  exit 1
fi
if ! [[ "$EXT_ID" =~ ^[a-p]{32}$ ]]; then
  cat >&2 <<MSG
config.yaml claude_ai_sync.extension_id is empty or not a valid extension id (32 letters a-p).
Nothing was written. To set it up:
  1. Open chrome://extensions, turn on "Developer mode", click "Load unpacked"
     and choose: $ROOT/extension/dreamer-sync
  2. Copy the ID Chrome shows on the extension's card into config.yaml as
     claude_ai_sync.extension_id
  3. Re-run this script.
MSG
  exit 1
fi

NMH_DIR="${CHROME_NMH_DIR:-$HOME/.config/google-chrome/NativeMessagingHosts}"
MANIFEST="$NMH_DIR/$HOST_NAME.json"
HOST_BIN="$ROOT/bin/claude-ai-host"
[[ -x "$HOST_BIN" ]] || { echo "missing or not executable: $HOST_BIN" >&2; exit 1; }

mkdir -p "$NMH_DIR"
TMP="$(mktemp "$NMH_DIR/.tmp-XXXXXX")"
python3 - "$HOST_NAME" "$HOST_BIN" "$EXT_ID" > "$TMP" <<'PY'
import json, sys
name, path, ext = sys.argv[1:4]
print(json.dumps({
    "name": name,
    "description": "Dreamer Sync: writes claude.ai chats into Dreamer's to_ingest/",
    "path": path,
    "type": "stdio",
    "allowed_origins": [f"chrome-extension://{ext}/"],
}, indent=2))
PY
chmod 644 "$TMP"
mv "$TMP" "$MANIFEST"
echo "host manifest: $MANIFEST"
echo "  host:         $HOST_BIN"
echo "  extension id: $EXT_ID"
cat <<STEPS

One-time steps, in the Chrome you use every day:
  1. Open chrome://extensions
  2. Turn on "Developer mode" (top right)
  3. Click "Load unpacked" and choose: $ROOT/extension/dreamer-sync
  4. Check the card shows ID $EXT_ID. The ID is derived from the folder path
     Chrome loaded, so if it differs, copy the ID shown into config.yaml
     (claude_ai_sync.extension_id) and re-run this script.
  5. Make sure you are logged in to https://claude.ai in that Chrome
  6. Click the Dreamer Sync toolbar icon once (puzzle-piece menu, pin it).
     That forces the first run; a red "!" badge means it needs attention.
  7. Check:  cat $ROOT/vault/.vault-meta/claude-ai-sync.json
             tail $ROOT/logs/claude-ai-host.log
STEPS
