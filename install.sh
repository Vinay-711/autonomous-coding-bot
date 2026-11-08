#!/usr/bin/env bash
# ==============================================================================
# 🚀 AutoCoder AI: Global Terminal Command Installer
# ==============================================================================
# Installs 'autocoder' as a global terminal command on macOS and Linux.
# Usage:
#   Local:   ./install.sh
#   One-line: curl -fsSL https://raw.githubusercontent.com/Vinay-711/autonomous-coding-bot/main/install.sh | bash
# ==============================================================================

set -e

BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
NC="\033[0m"

echo -e "${CYAN}${BOLD}"
echo "╔═══════════════════════════════════════════════════════════════════════╗"
echo "║          🚀 AutoCoder AI: Global Terminal Command Installer          ║"
echo "╚═══════════════════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ Python 3 is required but not installed.${NC}"
    echo "Please install Python 3.10+ first (e.g. 'brew install python3' or 'sudo apt install python3')."
    exit 1
fi

# 2. Determine App Directory
REPO_URL="https://github.com/Vinay-711/autonomous-coding-bot.git"
INSTALL_ROOT="$HOME/.autocoder"
mkdir -p "$INSTALL_ROOT"

if [ -f "./bot.py" ] && [ -f "./requirements.txt" ]; then
    APP_DIR="$(pwd)"
    echo -e "📂 Installing from local directory: ${GREEN}$APP_DIR${NC}"
else
    APP_DIR="$INSTALL_ROOT/app"
    if [ -d "$APP_DIR/.git" ]; then
        echo -e "🔄 Updating existing installation in: ${GREEN}$APP_DIR${NC}..."
        git -C "$APP_DIR" pull --quiet origin main || true
    else
        echo -e "📦 Cloning repository into: ${GREEN}$APP_DIR${NC}..."
        git clone --quiet "$REPO_URL" "$APP_DIR"
    fi
fi

# 3. Setup Virtual Environment
VENV_DIR="$INSTALL_ROOT/venv"
if [ ! -d "$VENV_DIR" ]; then
    echo -e "📦 Creating dedicated virtual environment in ${GREEN}$VENV_DIR${NC}..."
    python3 -m venv "$VENV_DIR"
fi

echo -e "📥 Installing dependencies and Playwright browser binaries..."
"$VENV_DIR/bin/python3" -m pip install --upgrade pip --quiet
"$VENV_DIR/bin/python3" -m pip install -r "$APP_DIR/requirements.txt" --quiet
"$VENV_DIR/bin/python3" -m playwright install chromium

# 4. Choose Global Binary Location
BIN_DIR=""
if [ -w "/usr/local/bin" ]; then
    BIN_DIR="/usr/local/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
fi

CMD_FILE="$BIN_DIR/autocoder"
echo -e "🔗 Linking executable command to: ${GREEN}$CMD_FILE${NC}..."

cat << EOF > "$CMD_FILE"
#!/usr/bin/env bash
export AUTOCODER_HOME="$INSTALL_ROOT"
exec "$VENV_DIR/bin/python3" "$APP_DIR/bot.py" "\$@"
EOF

chmod +x "$CMD_FILE"

# 5. Check if BIN_DIR is in PATH
PATH_UPDATED=0
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    # Add to shell profile
    SHELL_PROFILE=""
    if [ -n "$ZSH_VERSION" ] || [[ "$SHELL" == *"zsh"* ]]; then
        SHELL_PROFILE="$HOME/.zshrc"
    elif [ -n "$BASH_VERSION" ] || [[ "$SHELL" == *"bash"* ]]; then
        SHELL_PROFILE="$HOME/.bashrc"
    fi

    if [ -n "$SHELL_PROFILE" ] && [ -f "$SHELL_PROFILE" ]; then
        if ! grep -q "$BIN_DIR" "$SHELL_PROFILE"; then
            echo "export PATH=\"$BIN_DIR:\$PATH\"" >> "$SHELL_PROFILE"
            PATH_UPDATED=1
        fi
    fi
fi

# 6. Configuration Check
CONFIG_FILE="$INSTALL_ROOT/.env"
if [ ! -f "$CONFIG_FILE" ] && [ ! -f "$APP_DIR/.env" ]; then
    echo ""
    echo -e "${YELLOW}🔑 Gemini API Key Setup:${NC}"
    echo -e "Get a free key from: ${CYAN}https://aistudio.google.com/${NC}"
    read -r -p "Enter your Google Gemini API Key (or press Enter to skip): " USER_KEY
    if [ -n "$USER_KEY" ]; then
        echo "GEMINI_API_KEY=\"$USER_KEY\"" > "$CONFIG_FILE"
        echo -e "${GREEN}✅ Key saved to $CONFIG_FILE${NC}"
    else
        echo -e "${YELLOW}ℹ️  You can set your API key later by running: autocoder config${NC}"
    fi
fi

echo ""
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════════════════════${NC}"
echo -e "${GREEN}${BOLD}  🎉 AutoCoder AI installed successfully!                             ${NC}"
echo -e "${GREEN}${BOLD}═══════════════════════════════════════════════════════════════════════${NC}"
echo -e "You can now use ${CYAN}${BOLD}autocoder${NC} from ANY terminal window!"
echo ""
echo -e "Commands you can run:"
echo -e "  ${BOLD}autocoder${NC}                    Launch interactive menu / daily quota"
echo -e "  ${BOLD}autocoder daily${NC}              Solve 5 problems on ALL platforms (25 total)"
echo -e "  ${BOLD}autocoder solve leetcode${NC}     Solve problems on LeetCode"
echo -e "  ${BOLD}autocoder solve codeforces${NC}   Solve problems on Codeforces"
echo -e "  ${BOLD}autocoder solve hackerrank${NC}   Solve problems on HackerRank"
echo -e "  ${BOLD}autocoder solve codechef${NC}     Solve problems on CodeChef"
echo -e "  ${BOLD}autocoder solve hackerearth${NC}  Solve problems on HackerEarth"
echo -e "  ${BOLD}autocoder status${NC}             View solved statistics across platforms"
echo -e "  ${BOLD}autocoder chrome${NC}             Launch Chrome with Turnstile bypass"
echo -e "  ${BOLD}autocoder schedule${NC}           Set up daily 6 PM background service"
echo -e "  ${BOLD}autocoder config${NC}             Update API keys and handles"
echo ""
if [ $PATH_UPDATED -eq 1 ]; then
    echo -e "${YELLOW}⚡ Note: Please restart your terminal or run:${NC}"
    echo -e "   source $SHELL_PROFILE"
fi
echo -e "${GREEN}Happy coding! 🚀${NC}"

