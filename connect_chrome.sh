#!/usr/bin/env bash
# ==============================================================================
# Helper to Launch/Relaunch Chrome with Remote Debugging Enabled
# Allows the AI Bot to connect directly to your real Chrome browser!
# ==============================================================================

echo "======================================================================="
echo "🌐 Enabling Automation Access on your Google Chrome..."
echo "======================================================================="

# Gracefully quit current Chrome instance so it unlocks the profile
osascript -e 'tell application "Google Chrome" to quit' 2>/dev/null
sleep 2

# Force kill any stuck helper processes
killall "Google Chrome" 2>/dev/null || true
sleep 1

# Launch Google Chrome with CDP Remote Debugging enabled on port 9222
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222 --restore-last-session >/dev/null 2>&1 &
sleep 2

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Verify port 9222 is active
if curl -s http://localhost:9222/json/version >/dev/null 2>&1; then
    echo "✅ Google Chrome is now running with Remote Debugging on port 9222!"
    echo "   All your open tabs, saved logins, and cookies are ready."
    echo ""
    echo "🚀 Starting the AI Java Coding Bot now..."
    echo "======================================================================="
    python3 "$SCRIPT_DIR/bot.py" "$@"
else
    echo "⚠️  Chrome opened, but port 9222 did not respond in 2s."
    echo "   Running bot with dedicated persistent session fallback..."
    python3 "$SCRIPT_DIR/bot.py" "$@"
fi

