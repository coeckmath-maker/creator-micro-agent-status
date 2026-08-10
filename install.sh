#!/usr/bin/env bash
# Install the agent-status daemon and its Claude Code hooks.
# Idempotent: safe to re-run. Touches only files under $HOME.
set -euo pipefail

DEST="$HOME/.local/share/wl-agent-status"
UNIT_DIR="$HOME/.config/systemd/user"
SRC="$(cd "$(dirname "$0")" && pwd)/src"

echo "==> installing scripts to $DEST"
mkdir -p "$DEST" "$UNIT_DIR"
install -m 0755 "$SRC"/wl_device.py "$SRC"/agent_status_daemon.py \
                "$SRC"/record_state.py "$SRC"/write_config.py \
                "$SRC"/listen.py "$SRC"/azerty_be.py "$DEST/"

echo "==> checking the device is reachable"
if python3 "$DEST/wl_device.py" off off off off >/dev/null 2>&1; then
    echo "    found, and it answered"
else
    echo "    NOT reachable. Check it's plugged in or paired, and that udev"
    echo "    grants you access to /dev/hidraw* (see README). Installing anyway."
fi

echo "==> installing the systemd --user unit"
sed "s|%h|$HOME|g" "$(dirname "$SRC")/systemd/wl-agent-status.service" \
    > "$UNIT_DIR/wl-agent-status.service"
systemctl --user daemon-reload
systemctl --user enable --now wl-agent-status.service
systemctl --user is-active wl-agent-status.service

cat <<EOF

==> one manual step left

Add this to the "hooks" key of ~/.claude/settings.json (merge it with what is
already there — do not overwrite the file):

{
  "SessionStart":     [{"hooks": [{"type": "command", "command": "python3 $DEST/record_state.py SessionStart",     "timeout": 3}]}],
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 $DEST/record_state.py UserPromptSubmit", "timeout": 3}]}],
  "Stop":             [{"hooks": [{"type": "command", "command": "python3 $DEST/record_state.py Stop",             "timeout": 3}]}],
  "Notification":     [{"matcher": "permission_prompt", "hooks": [{"type": "command", "command": "python3 $DEST/record_state.py Notification", "timeout": 3}]}]
}

Hooks take effect immediately — no need to restart your session.

Then put KV_OAI_AG00..AG03 on four keys of a layer. See the README.

Logs:  journalctl --user -u wl-agent-status -f
EOF
