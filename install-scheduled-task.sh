#!/usr/bin/env bash
# Install or manage the macOS launchd agent for local usage collection.
#
# Usage:
#   ./install-scheduled-task.sh
#   ./install-scheduled-task.sh --run-now
#   ./install-scheduled-task.sh --uninstall
set -euo pipefail

LABEL="com.llm-usage.update"
PLIST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
LOG="${HOME}/Library/Logs/llm-usage-update.log"
UID_NUM="$(id -u)"
DOMAIN="gui/${UID_NUM}"
TARGET="${DOMAIN}/${LABEL}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
UPDATE_SCRIPT="${ROOT}/update-local.sh"

RUN_NOW=0
UNINSTALL=0
for arg in "$@"; do
  case "$arg" in
    --run-now) RUN_NOW=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help)
      sed -n '2,8p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--run-now] [--uninstall]" >&2
      exit 2
      ;;
  esac
done

bootout_if_loaded() {
  if launchctl print "$TARGET" >/dev/null 2>&1; then
    launchctl bootout "$TARGET"
  fi
}

if [ "$UNINSTALL" -eq 1 ]; then
  bootout_if_loaded
  if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed launch agent '$LABEL'."
  else
    echo "Launch agent '$LABEL' is not installed."
  fi
  exit 0
fi

if [ ! -x "$UPDATE_SCRIPT" ]; then
  echo "Update script was not found or is not executable: $UPDATE_SCRIPT" >&2
  exit 1
fi

xml_escape() {
  printf '%s' "$1" | sed -e 's/\&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e "s/'/\&apos;/g" -e 's/"/\&quot;/g'
}

ESC_SCRIPT="$(xml_escape "$UPDATE_SCRIPT")"
ESC_ROOT="$(xml_escape "$ROOT")"
ESC_LOG="$(xml_escape "$LOG")"
ESC_HOME="$(xml_escape "$HOME")"

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"

# Hourly at minute 0. A missed hour (sleep / lid closed) runs on wake.
# Aqua session lets git reuse osxkeychain, same idea as Windows InteractiveToken.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>${ESC_SCRIPT}</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${ESC_ROOT}</string>
  <key>StartCalendarInterval</key>
  <dict>
    <key>Minute</key>
    <integer>0</integer>
  </dict>
  <key>StandardOutPath</key>
  <string>${ESC_LOG}</string>
  <key>StandardErrorPath</key>
  <string>${ESC_LOG}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key>
    <string>${ESC_HOME}</string>
    <key>GIT_TERMINAL_PROMPT</key>
    <string>0</string>
  </dict>
  <key>ProcessType</key>
  <string>Background</string>
  <key>LimitLoadToSessionType</key>
  <string>Aqua</string>
</dict>
</plist>
EOF

bootout_if_loaded
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl enable "$TARGET"

echo "Installed launch agent '$LABEL' (hourly at minute 0)."
echo "Logs: $LOG"
echo "Inspect: launchctl print $TARGET"
echo "Run now: launchctl kickstart -k $TARGET"
echo "Secrets (DEEPSEEK_PLATFORM_TOKEN, …) belong in $ROOT/.env, not this plist."

if [ "$RUN_NOW" -eq 1 ]; then
  launchctl kickstart -k "$TARGET"
  echo "Started '$LABEL'."
fi
