# 📖 AutoCoder AI: Complete Usage & Command Instructions

Welcome to **AutoCoder AI**! This guide contains everything you need to set up, configure, and use the autonomous multi-platform coding bot from your terminal.

> [!IMPORTANT]
> **🔑 Bring Your Own API Key (BYOK)**:
> To run AutoCoder AI, you **must use your own free Google Gemini API key**.
> - Get your free key (takes 10 seconds, no credit card): [Google AI Studio](https://aistudio.google.com/)
> - Your personal API key is stored securely on your own device in `~/.autocoder/.env` and is never shared or tracked in git.
> - Configure or update your key anytime by running: `autocoder config`

---

## ⚡ 1. Fast Global Terminal Installation

Install the `autocoder` command globally so you can use it from **any terminal window**:

### Option 1: One-Line Terminal Installer (Recommended)
```bash
curl -fsSL https://raw.githubusercontent.com/Vinay-711/autonomous-coding-bot/main/install.sh | bash
```

### Option 2: From Cloned Repository
```bash
git clone https://github.com/Vinay-711/autonomous-coding-bot.git
cd autonomous-coding-bot
chmod +x install.sh && ./install.sh
```

### Option 3: Via Python Package (`pipx` or `pip`)
```bash
pipx install git+https://github.com/Vinay-711/autonomous-coding-bot.git
```

---

## 💻 2. Terminal Commands Cheat Sheet

Once installed, simply open any terminal window and use these commands:

| Command | Action |
| :--- | :--- |
| `autocoder` | Launch the interactive menu or start the daily routine |
| `autocoder daily` | Solve today's quota: 5 medium-hard problems on **all 5 platforms** (25 total) |
| `autocoder solve leetcode` | Autonomous solving on **LeetCode** (default: 5 problems) |
| `autocoder solve codeforces` | Autonomous solving on **Codeforces** (default: 5 problems) |
| `autocoder solve hackerrank` | Autonomous solving on **HackerRank** (default: 5 problems) |
| `autocoder solve codechef` | Autonomous solving on **CodeChef** (default: 5 problems) |
| `autocoder solve hackerearth` | Autonomous solving on **HackerEarth** (default: 5 problems) |
| `autocoder status` | View a live summary table of verified solved problems across all platforms |
| `autocoder chrome` | Launch Google Chrome with remote debugging on port `9222` (bypasses Cloudflare Turnstile) |
| `autocoder schedule` | Set up or inspect the automated 6:00 PM background schedule |
| `autocoder config` | Interactive configuration wizard to update Gemini API keys and handles |

---

## 🎯 3. Command Examples

### Solve 3 Medium problems on LeetCode:
```bash
autocoder solve leetcode --count 3 --difficulty medium
```

### Solve 5 Hard problems on Codeforces with live submit:
```bash
autocoder solve codeforces --count 5 --difficulty hard --mode both
```

### Solve custom URLs from a text file:
```bash
autocoder --platform file --file problems.txt --mode both
```

### Run headless in the background:
```bash
autocoder daily --headless
```

### Check your solved problems registry:
```bash
autocoder status
```
Output:
```text
====================================================================
      📊 AutoCoder AI: Solved Problems Status & Registry
====================================================================
  Platform         Status           Verified Count
  ────────────────────────────────────────────────────────────────
  LeetCode         🟢 Active                 65 solved
  Codeforces       🟢 Active                 96 solved
  HackerRank       🟢 Active                 50 solved
  CodeChef         🟢 Active                 44 solved
  HackerEarth      🟢 Active                 60 solved
  ────────────────────────────────────────────────────────────────
  TOTAL            🏆 Accepted              315 problems
====================================================================
```

---

## 🔑 4. API Key & Credentials Setup

AutoCoder AI uses **Google Gemini** (Gemini 2.5 / 3.1 Flash) to read problem descriptions, generate optimal Java algorithms, and self-heal any compilation errors.

1. Get a free API key at [Google AI Studio](https://aistudio.google.com/).
2. Run the interactive config wizard:
   ```bash
   autocoder config
   ```
3. Enter your Gemini API key, your GitHub username (for saving solutions to your GitHub), and your competitive programming handles.

Configuration is saved to `~/.autocoder/.env` (or `.env` in your project folder):
```env
GEMINI_API_KEY="your-gemini-api-key"
GITHUB_USERNAME="your-github-username"
CODEFORCES_HANDLE="your-codeforces-handle"
LEETCODE_USERNAME="your-leetcode-username"
HACKERRANK_USERNAME="your-hackerrank-username"
HACKEREARTH_USERNAME="your-hackerearth-username"
CODECHEF_USERNAME="your-codechef-username"
```

---

## 🌐 5. Bypassing Cloudflare Turnstile & Anti-Bot Checks

Some platforms (especially **Codeforces** and **LeetCode**) use Cloudflare Turnstile, Cloudflare WAF, or canvas bot detection.

To bypass anti-bot challenges seamlessly:
```bash
autocoder chrome
```
This launches Google Chrome with Chrome DevTools Protocol (`CDP`) on port `9222`:
1. Log in to Codeforces or LeetCode once in your real Chrome browser.
2. The bot connects directly to your active browser session, inheriting all cookies and human trust tokens.
3. Submissions pass without triggering anti-bot challenges!

---

## ⏰ 6. Automated Daily 6:00 PM Practice

To have the bot automatically solve 5 verified problems on all platforms every single day:

```bash
autocoder schedule
```
- On **macOS**: Installs a persistent `launchd` service that wakes up daily at 6:00 PM (18:00 Asia/Kolkata), solves 25 problems, commits them to your GitHub, and logs output to `~/.autocoder/logs/daily_run.log`.
- To check service status:
  ```bash
  launchctl list | grep autocoder
  ```
- To uninstall the schedule:
  ```bash
  launchctl unload ~/Library/LaunchAgents/com.autocoder.codingbot.plist
  ```

---

## 🐙 7. GitHub Solution Archival

When `GITHUB_USERNAME` is configured in your `.env`, AutoCoder AI automatically:
1. Clones or updates your dedicated repository for each platform:
   - `https://github.com/<your-username>/leetcode-daily`
   - `https://github.com/<your-username>/codeforces-daily`
   - `https://github.com/<your-username>/hackerrank-daily`
   - `https://github.com/<your-username>/codechef-daily`
   - `https://github.com/<your-username>/hackerearth-daily`
2. Saves the verified Java solution (e.g. `Solution.java`, `Main.java`) and a detailed `README.md` with algorithmic complexity and approach.
3. Commits and pushes changes with a clean commit message.

---

## 🛠️ 8. Troubleshooting

- **`autocoder: command not found`**:
  Make sure `~/.local/bin` is in your PATH. Add this line to your `~/.zshrc` or `~/.bashrc`:
  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  ```
  Then reload your terminal: `source ~/.zshrc`.

- **`Java compiler (javac) not found`**:
  Install OpenJDK 17 or 21:
  - macOS: `brew install openjdk`
  - Ubuntu/Debian: `sudo apt install -y default-jdk`

- **Missing Playwright browsers**:
  Run:
  ```bash
  playwright install chromium
  ```

