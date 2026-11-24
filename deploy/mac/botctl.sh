#!/bin/bash
# Run the Telegram bot as an always-on background service on macOS (launchd).
#
#   deploy/mac/botctl.sh install    # start now + at every login, auto-restart on crash
#   deploy/mac/botctl.sh status     # is it running?
#   deploy/mac/botctl.sh logs       # follow the bot log (Ctrl+C to stop following)
#   deploy/mac/botctl.sh restart    # after changing .env or pulling new code
#   deploy/mac/botctl.sh stop       # stop until next login / start
#   deploy/mac/botctl.sh start
#   deploy/mac/botctl.sh uninstall  # remove the service completely
#
# While running, the Mac is kept from idle-sleeping (caffeinate). The display
# may still turn off. Closing the lid on battery will still sleep the Mac.
set -euo pipefail

LABEL="com.youtube-automation.bot"
PROJECT="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON="$PROJECT/.venv/bin/python"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$PROJECT/logs/bot.out.log"
DOMAIN="gui/$(id -u)"

need_venv() {
  if [ ! -x "$PYTHON" ]; then
    echo "❌ $PYTHON not found. Create it first:"
    echo "   /opt/homebrew/bin/python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
  fi
}

write_plist() {
  mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT/logs"
  cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-is</string>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>app.bot</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key><string>1</string>
    <key>PYTHONWARNINGS</key><string>ignore</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST
}

is_loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

case "${1:-status}" in
  install)
    need_venv
    is_loaded && launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    write_plist
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "✅ Installed. The bot starts now and at every login. Logs: $LOG"
    ;;
  uninstall)
    is_loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    rm -f "$PLIST"
    echo "✅ Removed the background service."
    ;;
  start)
    [ -f "$PLIST" ] || { echo "Not installed — run: $0 install"; exit 1; }
    is_loaded || launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart "$DOMAIN/$LABEL"
    echo "✅ Started."
    ;;
  stop)
    is_loaded && launchctl bootout "$DOMAIN/$LABEL" || true
    echo "⏹  Stopped (starts again at next login, or run: $0 start)."
    ;;
  restart)
    [ -f "$PLIST" ] || { echo "Not installed — run: $0 install"; exit 1; }
    is_loaded || launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo "🔄 Restarted."
    ;;
  status)
    if is_loaded; then
      pid=$(launchctl print "$DOMAIN/$LABEL" | awk '/^\tpid = /{print $3}')
      if [ -n "${pid:-}" ]; then echo "🟢 Running (pid $pid)"; else echo "🟡 Loaded but not running — check: $0 logs"; fi
    else
      echo "🔴 Not running"
    fi
    [ -f "$LOG" ] && { echo "--- last log lines ---"; tail -n 5 "$LOG"; }
    ;;
  logs)
    touch "$LOG"; tail -n 50 -f "$LOG"
    ;;
  *)
    echo "Usage: $0 {install|uninstall|start|stop|restart|status|logs}"; exit 1
    ;;
esac
