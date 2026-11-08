#!/usr/bin/env bash
# ==============================================================================
# Setup Daily macOS Launchd Service for AI Java Coding Bot
# ==============================================================================

PLIST_NAME="com.autocoder.codingbot.plist"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME"
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="$PROJECT_DIR/.venv/bin/python3"
LOG_FILE="$PROJECT_DIR/logs/daily_run.log"

# Load GEMINI_API_KEY from .env if present
if [ -f "$PROJECT_DIR/.env" ]; then
    export $(grep -v '^#' "$PROJECT_DIR/.env" | xargs)
fi

# Daily Schedule Time: 06:00 PM (18:00 Asia/Kolkata)
SCHEDULE_HOUR=18
SCHEDULE_MINUTE=0

if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN=$(which python3)
fi

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJECT_DIR/logs"

echo "Creating launchd service plist at: $PLIST_PATH"

cat << EOF > "$PLIST_PATH"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.autocoder.codingbot</string>
    <key>WorkingDirectory</key>
    <string>$PROJECT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/opt/openjdk/bin:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>HOME</key>
        <string>$HOME</string>
        <key>GEMINI_API_KEY</key>
        <string>${GEMINI_API_KEY}</string>
    </dict>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_BIN</string>
        <string>$PROJECT_DIR/bot.py</string>
        <string>--platform</string>
        <string>all</string>
        <string>--count</string>
        <string>5</string>
        <string>--difficulty</string>
        <string>medium-hard</string>
        <string>--mode</string>
        <string>both</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>$SCHEDULE_HOUR</integer>
        <key>Minute</key>
        <integer>$SCHEDULE_MINUTE</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

# Unload previous instance if existing
launchctl unload "$PLIST_PATH" 2>/dev/null || true

# Load the new service
launchctl load "$PLIST_PATH"

echo "======================================================================="
echo "✅ Daily macOS Service Configured & Loaded Successfully!"
echo "   Scheduled Time: 06:00 PM (18:00) every day"
echo "   Platforms     : All 5 (LeetCode, Codeforces, HackerRank, CodeChef, HackerEarth)"
echo "   Problems      : 5 Medium-to-Hard questions per platform (25 total / day)"
echo "   Language      : Java (OpenJDK 25 / Java 17+)"
echo "   GitHub Commits: Auto-commit & push to ${GITHUB_USERNAME:-your-username}/*-daily repositories"
echo "   Output Log    : $LOG_FILE"
echo "======================================================================="
echo "To check service status:"
echo "   launchctl list | grep codingbot"
echo "To stop or remove the service:"
echo "   launchctl unload $PLIST_PATH && rm $PLIST_PATH"
echo "======================================================================="
