#!/usr/bin/env node

/**
 * Autonomous Coding Bot - NPM CLI Launcher
 * https://github.com/Vinay-711/autonomous-coding-bot
 */

const { spawn, execSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const http = require('http');

const ROOT_DIR = path.resolve(__dirname, '..');
const IS_WINDOWS = process.platform === 'win32';
const VENV_DIR = path.join(ROOT_DIR, '.venv');
const VENV_PYTHON = IS_WINDOWS
  ? path.join(VENV_DIR, 'Scripts', 'python.exe')
  : path.join(VENV_DIR, 'bin', 'python');
const VENV_PIP = IS_WINDOWS
  ? path.join(VENV_DIR, 'Scripts', 'pip.exe')
  : path.join(VENV_DIR, 'bin', 'pip');

// ANSI Color Helpers
const colors = {
  reset: '\x1b[0m',
  bold: '\x1b[1m',
  green: '\x1b[32m',
  yellow: '\x1b[33m',
  blue: '\x1b[34m',
  cyan: '\x1b[36m',
  red: '\x1b[31m',
  gray: '\x1b[90m',
};

function log(msg) {
  console.log(`${colors.cyan}[autonomous-coding-bot]${colors.reset} ${msg}`);
}

function logError(msg) {
  console.error(`${colors.red}[autonomous-coding-bot:error]${colors.reset} ${msg}`);
}

// 1. Check if command exists
function commandExists(cmd) {
  try {
    execSync(IS_WINDOWS ? `where ${cmd}` : `which ${cmd}`, { stdio: 'ignore' });
    return true;
  } catch {
    return false;
  }
}

// 2. Check system prerequisites
function checkPrerequisites() {
  if (!commandExists('python3') && !commandExists('python')) {
    logError('Python 3 is required but was not found on your PATH.');
    console.log('👉 Please install Python 3.10+ from https://www.python.org/downloads/');
    process.exit(1);
  }

  if (!commandExists('javac')) {
    console.log(`${colors.yellow}⚠️  Notice: 'javac' (Java Compiler) was not found on your PATH.${colors.reset}`);
    console.log('   The bot compiles solutions locally before submitting.');
    console.log('   Please install Java JDK 17 or 21 (e.g. brew install openjdk@21) if you want local compilation.\n');
  }
}

// 3. Ensure .env exists
function ensureEnvFile() {
  const envPath = path.join(ROOT_DIR, '.env');
  const envExamplePath = path.join(ROOT_DIR, '.env.example');

  if (!fs.existsSync(envPath)) {
    if (fs.existsSync(envExamplePath)) {
      fs.copyFileSync(envExamplePath, envPath);
      log(`Created initial .env from .env.example`);
    } else {
      fs.writeFileSync(
        envPath,
        `GEMINI_API_KEY=""\nGITHUB_USERNAME="Vinay-711"\nCODEFORCES_HANDLE=""\n`,
        'utf8'
      );
      log(`Created initial .env file`);
    }

    console.log(`\n${colors.yellow}⚠️  Please open ${envPath} and add your GEMINI_API_KEY!${colors.reset}\n`);
  }
}

// 4. Ensure virtual environment & dependencies are installed
function ensureVenv() {
  if (!fs.existsSync(VENV_PYTHON)) {
    log('Setting up isolated Python virtual environment (.venv)...');
    const pyCmd = commandExists('python3') ? 'python3' : 'python';
    try {
      execSync(`${pyCmd} -m venv "${VENV_DIR}"`, { cwd: ROOT_DIR, stdio: 'inherit' });
      log('Installing required dependencies from requirements.txt...');
      execSync(`"${VENV_PIP}" install -r requirements.txt`, { cwd: ROOT_DIR, stdio: 'inherit' });
      log('Installing Playwright Chromium browser binary...');
      execSync(`"${VENV_PYTHON}" -m playwright install chromium`, { cwd: ROOT_DIR, stdio: 'inherit' });
      log(`${colors.green}Environment setup complete!${colors.reset}`);
    } catch (err) {
      logError(`Failed to setup Python virtual environment: ${err.message}`);
      process.exit(1);
    }
  }
}

// 5. Check if Chrome CDP is alive on port 9222
function isChromeActive() {
  return new Promise((resolve) => {
    const req = http.get('http://127.0.0.1:9222/json/version', { timeout: 1500 }, (res) => {
      resolve(res.statusCode === 200);
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => {
      req.destroy();
      resolve(false);
    });
  });
}

// 6. Launch Chrome with remote debugging if not active
async function ensureChrome() {
  const active = await isChromeActive();
  if (active) {
    return;
  }

  log('Chrome is not currently running on port 9222. Launching Chrome with remote debugging...');

  let chromePath = '';
  if (process.platform === 'darwin') {
    chromePath = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
  } else if (process.platform === 'linux') {
    const candidates = ['google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser'];
    for (const c of candidates) {
      if (commandExists(c)) {
        chromePath = c;
        break;
      }
    }
  } else if (IS_WINDOWS) {
    chromePath = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
  }

  const profileDir = path.join(ROOT_DIR, 'saved_chrome_login');
  if (!fs.existsSync(profileDir)) {
    fs.mkdirSync(profileDir, { recursive: true });
  }

  if (chromePath && (fs.existsSync(chromePath) || commandExists(chromePath))) {
    try {
      const chromeSubprocess = spawn(
        chromePath,
        [
          '--remote-debugging-port=9222',
          `--user-data-dir=${profileDir}`,
          '--no-first-run',
          '--no-default-browser-check',
        ],
        {
          detached: true,
          stdio: 'ignore',
        }
      );
      chromeSubprocess.unref();

      // Wait up to 6 seconds for Chrome port to become responsive
      for (let i = 0; i < 12; i++) {
        await new Promise((r) => setTimeout(r, 500));
        if (await isChromeActive()) {
          log(`${colors.green}Connected to Chrome browser session on port 9222!${colors.reset}`);
          return;
        }
      }
    } catch (e) {
      logError(`Could not auto-start Chrome: ${e.message}`);
    }
  }

  console.log(`${colors.yellow}⚠️  Could not auto-launch Chrome on port 9222.${colors.reset}`);
  console.log(`👉 Please run ${colors.bold}npm run chrome${colors.reset} or start Chrome with:`);
  console.log(`   google-chrome --remote-debugging-port=9222 --user-data-dir="./saved_chrome_login"\n`);
}

// Main execution routine
async function main() {
  const args = process.argv.slice(2);

  // If user passed --setup flag, only run prerequisite & venv setup
  if (args.includes('--setup')) {
    checkPrerequisites();
    ensureEnvFile();
    ensureVenv();
    log(`${colors.green}Setup completed successfully!${colors.reset}`);
    return;
  }

  // If user passed --chrome flag, only launch Chrome
  if (args.includes('--chrome')) {
    await ensureChrome();
    return;
  }

  checkPrerequisites();
  ensureEnvFile();
  ensureVenv();
  await ensureChrome();

  const botScript = path.join(ROOT_DIR, 'bot.py');
  const pythonExec = fs.existsSync(VENV_PYTHON) ? VENV_PYTHON : 'python3';

  // Spawn bot.py passing all CLI arguments
  const child = spawn(pythonExec, [botScript, ...args], {
    cwd: ROOT_DIR,
    stdio: 'inherit',
    env: process.env,
  });

  child.on('error', (err) => {
    logError(`Failed to run bot: ${err.message}`);
    process.exit(1);
  });

  child.on('close', (code) => {
    process.exit(code || 0);
  });
}

main().catch((err) => {
  logError(err.message);
  process.exit(1);
});
