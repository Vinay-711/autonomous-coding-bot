#!/usr/bin/env bash
# ==============================================================================
# 🌐 Launch/Relaunch Chrome with Remote Debugging Enabled (Port 9222)
# Allows the AI Bot to connect directly to your real Chrome browser session,
# automatically bypassing Cloudflare Turnstile, CAPTCHAs, and anti-bot checks.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "======================================================================="
echo "🌐 Launching Google Chrome with Remote Debugging (port 9222)..."
echo "======================================================================="

# Detect OS and locate Google Chrome binary
CHROME_BIN=""
if [[ "$OSTYPE" == "darwin"* ]]; then
    CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null
    sleep 2
    killall "Google Chrome" 2>/dev/null || true
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    CHROME_BIN=$(which google-chrome || which google-chrome-stable || which chromium || which chromium-browser || echo "")
    pkill -f "chrome" 2>/dev/null || true
elif [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" ]]; then
    CHROME_BIN="/c/Program Files/Google/Chrome/Application/chrome.exe"
    taskkill //F //IM chrome.exe 2>/dev/null || true
fi

if [ -n "$CHROME_BIN" ] && [ -x "$CHROME_BIN" ]; then
    echo "Starting: $CHROME_BIN"
    "$CHROME_BIN" --remote-debugging-port=9222 --restore-last-session >/dev/null 2>&1 &
elif [ -n "$CHROME_BIN" ]; then
    "$CHROME_BIN" --remote-debugging-port=9222 --restore-last-session >/dev/null 2>&1 &
else
    echo "⚠️  Could not auto-locate Chrome binary. Trying default 'google-chrome'..."
    google-chrome --remote-debugging-port=9222 --restore-last-session >/dev/null 2>&1 &
fi

sleep 2

# Select Python binary (.venv preferred)
if [ -f "$SCRIPT_DIR/.venv/bin/python3" ]; then
    PYTHON_EXEC="$SCRIPT_DIR/.venv/bin/python3"
elif [ -f "$SCRIPT_DIR/.venv/Scripts/python.exe" ]; then
    PYTHON_EXEC="$SCRIPT_DIR/.venv/Scripts/python.exe"
else
    PYTHON_EXEC="python3"
fi

# Verify port 9222 is active
if curl -s http://localhost:9222/json/version >/dev/null 2>&1; then
    echo "✅ Google Chrome is now active on port 9222!"
    echo "   All your open tabs, saved logins, and cookies are ready."
    echo ""
    echo "🚀 Starting AutoCoder AI connected to your Chrome..."
    echo "======================================================================="
    "$PYTHON_EXEC" "$SCRIPT_DIR/bot.py" "$@"
else
    echo "⚠️  Chrome port 9222 did not respond in 2s."
    echo "   Running bot with dedicated persistent session fallback..."
    "$PYTHON_EXEC" "$SCRIPT_DIR/bot.py" "$@"
fi
