#!/usr/bin/env bash
# ==============================================================================
# 🚀 AutoCoder AI - Frictionless One-Click Launcher & Setup Wizard
# ==============================================================================
# This script automatically:
# 1. Verifies system prerequisites (Python 3, Java JDK)
# 2. Creates and manages the Python virtual environment (.venv)
# 3. Installs all required packages and Playwright browser binaries
# 4. Guides first-time setup for free Google Gemini API credentials
# 5. Provides an interactive menu or accepts direct CLI arguments
# ==============================================================================

# ANSI Color Codes
BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
PURPLE="\033[0;35m"
NC="\033[0m" # No Color

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

print_header() {
    clear 2>/dev/null || true
    echo -e "${CYAN}${BOLD}"
    echo "╔═══════════════════════════════════════════════════════════════════════╗"
    echo "║              🤖 AutoCoder AI: Autonomous Coding Agent                 ║"
    echo "║       Multi-Platform Problem Solver • Java Verification • GitHub      ║"
    echo "╚═══════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
}

print_header

# ------------------------------------------------------------------------------
# 1. System Requirements Check
# ------------------------------------------------------------------------------
echo -e "${CYAN}🔍 Checking system prerequisites...${NC}"

# Check for Python 3
if ! command -v python3 &>/dev/null; then
    echo -e "${RED}❌ Error: Python 3 is not installed or not found in PATH.${NC}"
    echo -e "👉 Please install Python 3.10+:"
    echo -e "   • macOS:          brew install python3"
    echo -e "   • Ubuntu/Debian:  sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    echo -e "   • Windows:        Download from https://www.python.org/"
    exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo -e "   ✅ Python: ${GREEN}v$PY_VERSION${NC}"

# Check for Java JDK (javac)
if command -v javac &>/dev/null; then
    JAVAC_VER=$(javac -version 2>&1 | head -n 1)
    echo -e "   ✅ Java Compiler: ${GREEN}$JAVAC_VER${NC}"
else
    echo -e "   ${YELLOW}⚠️  Java Compiler (javac) not found in PATH.${NC}"
    echo -e "   ${YELLOW}   (Local compilation checks will be skipped, but bot can still solve problems)${NC}"
    echo -e "   ${YELLOW}   To install Java 17+: 'brew install openjdk' (macOS) or 'sudo apt install default-jdk' (Linux)${NC}"
fi

# ------------------------------------------------------------------------------
# 2. Virtual Environment Setup
# ------------------------------------------------------------------------------
if [ ! -d ".venv" ]; then
    echo -e "\n${CYAN}📦 Setting up isolated virtual environment (.venv)...${NC}"
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Failed to create virtual environment. Ensure 'python3-venv' is installed.${NC}"
        exit 1
    fi
fi

# Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    PYTHON_EXEC=".venv/bin/python3"
elif [ -f ".venv/Scripts/activate" ]; then
    source .venv/Scripts/activate
    PYTHON_EXEC=".venv/Scripts/python.exe"
else
    PYTHON_EXEC="python3"
fi

# ------------------------------------------------------------------------------
# 3. Dependencies & Browser Binaries Check
# ------------------------------------------------------------------------------
NEED_INSTALL=0
$PYTHON_EXEC -c "import google.genai, playwright, pydantic, pyperclip" 2>/dev/null || NEED_INSTALL=1

if [ $NEED_INSTALL -eq 1 ]; then
    echo -e "\n${CYAN}📥 Installing required dependencies from requirements.txt...${NC}"
    $PYTHON_EXEC -m pip install --upgrade pip --quiet
    $PYTHON_EXEC -m pip install -r requirements.txt
    if [ $? -ne 0 ]; then
        echo -e "${RED}❌ Failed to install Python dependencies.${NC}"
        exit 1
    fi

    echo -e "${CYAN}🌐 Installing Playwright Chromium browser binaries...${NC}"
    $PYTHON_EXEC -m playwright install chromium
    if [ $? -ne 0 ]; then
        echo -e "${YELLOW}⚠️  Playwright browser installation had a warning, continuing...${NC}"
    fi
    echo -e "${GREEN}✅ All dependencies installed successfully!${NC}"
fi

# ------------------------------------------------------------------------------
# 4. API Key & Configuration Wizard
# ------------------------------------------------------------------------------
setup_env_wizard() {
    echo -e "\n${PURPLE}${BOLD}╔═══════════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${PURPLE}${BOLD}║                🔑 Quick Configuration Setup Wizard                    ║${NC}"
    echo -e "${PURPLE}${BOLD}╚═══════════════════════════════════════════════════════════════════════╝${NC}"
    echo -e "AutoCoder AI requires a free Google Gemini API key to solve problems."
    echo -e "👉 Get your key in 10 seconds (free): ${CYAN}${BOLD}https://aistudio.google.com/${NC}\n"

    CURRENT_KEY=""
    CURRENT_GH=""
    CURRENT_CF=""

    if [ -f ".env" ]; then
        CURRENT_KEY=$(grep '^GEMINI_API_KEY=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '"'\'' ')
        CURRENT_GH=$(grep '^GITHUB_USERNAME=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '"'\'' ')
        CURRENT_CF=$(grep '^CODEFORCES_HANDLE=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '"'\'' ')
    fi

    read -r -p "Enter your Google Gemini API Key [${CURRENT_KEY:0:8}...]: " INPUT_KEY
    GEMINI_KEY="${INPUT_KEY:-$CURRENT_KEY}"

    while [ -z "$GEMINI_KEY" ]; do
        echo -e "${RED}Gemini API Key cannot be empty! Please provide a valid key.${NC}"
        read -r -p "Enter your Google Gemini API Key: " GEMINI_KEY
    done

    echo ""
    read -r -p "Enter your GitHub Username (optional, for archiving solutions) [${CURRENT_GH}]: " INPUT_GH
    GH_USER="${INPUT_GH:-$CURRENT_GH}"

    read -r -p "Enter your Codeforces Handle (optional, to avoid duplicate problems) [${CURRENT_CF}]: " INPUT_CF
    CF_HANDLE="${INPUT_CF:-$CURRENT_CF}"

    cat << EOF > .env
# AutoCoder AI Configuration
GEMINI_API_KEY="${GEMINI_KEY}"
GITHUB_USERNAME="${GH_USER}"
CODEFORCES_HANDLE="${CF_HANDLE}"
EOF

    echo -e "\n${GREEN}✅ Configuration saved to .env!${NC}\n"
    sleep 1
}

# Auto-trigger wizard if .env does not exist or GEMINI_API_KEY is empty
if [ ! -f ".env" ]; then
    setup_env_wizard
else
    EXISTING_KEY=$(grep '^GEMINI_API_KEY=' .env 2>/dev/null | cut -d '=' -f2- | tr -d '"'\'' ')
    if [ -z "$EXISTING_KEY" ]; then
        setup_env_wizard
    fi
fi

# ------------------------------------------------------------------------------
# 5. Direct Argument Passthrough
# ------------------------------------------------------------------------------
# If user provided command line arguments (e.g. ./run.sh --platform leetcode --count 3)
if [ $# -gt 0 ]; then
    echo -e "${CYAN}🚀 Executing bot with arguments: $@${NC}\n"
    $PYTHON_EXEC bot.py "$@"
    exit $?
fi

# ------------------------------------------------------------------------------
# 6. Interactive User-Friendly Menu
# ------------------------------------------------------------------------------
while true; do
    print_header
    echo -e "Welcome to ${BOLD}AutoCoder AI${NC}! Choose an action below:"
    echo ""
    echo -e "  ${BOLD}[1]${NC} 🌟 ${GREEN}${BOLD}Run Daily Quota${NC} (Solve 5 problems on ALL 5 platforms - 25 total)"
    echo -e "  ${BOLD}[2]${NC} 🎯 ${CYAN}Solve a Single Platform${NC} (LeetCode, Codeforces, HackerRank, etc.)"
    echo -e "  ${BOLD}[3]${NC} 📄 Solve custom URLs from ${BOLD}problems.txt${NC}"
    echo -e "  ${BOLD}[4]${NC} 🌐 ${YELLOW}Connect Real Chrome${NC} (Bypass Cloudflare Turnstile & anti-bot)"
    echo -e "  ${BOLD}[5]${NC} ⏰ Setup Automated Daily 6:00 PM Schedule (macOS Background Service)"
    echo -e "  ${BOLD}[6]${NC} ⚙️  Reconfigure API Keys & Settings"
    echo -e "  ${BOLD}[0]${NC} 🚪 Exit"
    echo ""
    echo -e "─────────────────────────────────────────────────────────────────────────"
    read -r -p "Select an option [0-6] (Default: 1): " CHOICE
    CHOICE="${CHOICE:-1}"

    case "$CHOICE" in
        1)
            echo -e "\n${GREEN}🚀 Starting autonomous quota run across ALL platforms (5 each)...${NC}"
            $PYTHON_EXEC bot.py --platform all --count 5 --difficulty medium-hard --mode both
            echo ""
            read -r -p "Press Enter to return to main menu..."
            ;;
        2)
            echo ""
            echo -e "Select Platform:"
            echo -e "  1) LeetCode"
            echo -e "  2) Codeforces"
            echo -e "  3) HackerRank"
            echo -e "  4) CodeChef"
            echo -e "  5) HackerEarth"
            read -r -p "Enter platform [1-5]: " P_NUM
            case "$P_NUM" in
                1) PLAT="leetcode" ;;
                2) PLAT="codeforces" ;;
                3) PLAT="hackerrank" ;;
                4) PLAT="codechef" ;;
                5) PLAT="hackerearth" ;;
                *) echo -e "${RED}Invalid selection.${NC}"; sleep 1; continue ;;
            esac

            read -r -p "How many problems to solve? [Default: 5]: " COUNT
            COUNT="${COUNT:-5}"

            read -r -p "Difficulty [easy, medium, hard, medium-hard, any] (Default: medium-hard): " DIFF
            DIFF="${DIFF:-medium-hard}"

            read -r -p "Mode [both, submit, run] (Default: both): " MODE
            MODE="${MODE:-both}"

            echo -e "\n${CYAN}🚀 Launching bot for $PLAT ($COUNT problems, $DIFF difficulty)...${NC}"
            $PYTHON_EXEC bot.py --platform "$PLAT" --count "$COUNT" --difficulty "$DIFF" --mode "$MODE"
            echo ""
            read -r -p "Press Enter to return to main menu..."
            ;;
        3)
            echo -e "\n${CYAN}📄 Solving problem URLs listed in problems.txt...${NC}"
            $PYTHON_EXEC bot.py --platform file --file problems.txt --mode both
            echo ""
            read -r -p "Press Enter to return to main menu..."
            ;;
        4)
            echo -e "\n${YELLOW}🌐 Launching Chrome with Remote Debugging (port 9222)...${NC}"
            ./connect_chrome.sh
            echo ""
            read -r -p "Press Enter to return to main menu..."
            ;;
        5)
            echo -e "\n${PURPLE}⏰ Setting up Daily 6:00 PM automated background schedule...${NC}"
            ./setup_daily_schedule.sh
            echo ""
            read -r -p "Press Enter to return to main menu..."
            ;;
        6)
            setup_env_wizard
            ;;
        0)
            echo -e "\n${GREEN}👋 Happy Coding! See you next time.${NC}\n"
            exit 0
            ;;
        *)
            echo -e "\n${RED}Invalid option '${CHOICE}'. Please choose 0 to 6.${NC}"
            sleep 1
            ;;
    esac
done
