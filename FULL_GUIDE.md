# Setup — Windows / PowerShell

Use this instead of `SETUP.md`. That file is written for bash; several of its
commands fail silently or with confusing errors in PowerShell.

**Key differences from the bash guide:**

| Bash | PowerShell |
|---|---|
| `python3` | `py -3` |
| `source venv/bin/activate` | `venv\Scripts\Activate.ps1` |
| `export VAR=value` | `$env:VAR = "value"` |
| `source .env` | `. .\env.ps1` |
| `touch file` | `New-Item file` |
| `cat > f <<'EOF'` | here-string `@' ... '@` |

Use **PowerShell**, not Command Prompt. Windows Terminal is ideal if you have
it.

---

## Step 1 — Check Python

```powershell
py -3 --version
```

**Expect:** `Python 3.10` or higher.

If you get nothing, or Microsoft Store opens: install Python from python.org
and **tick "Add python.exe to PATH"** during install. Do not install from the
Store — the stub causes odd path problems.

Avoid typing `python3` on Windows; it often triggers the Store stub. Use
`py -3`.

---

## Step 2 — Put the files somewhere sensible

```powershell
mkdir $HOME\projects
cd $HOME\projects
```

Move or unzip the `tokenscore` folder into `$HOME\projects`.

**Check:**

```powershell
ls tokenscore
```

**Expect:** `README.md  SETUP.md  bot  probe.py  scoring  sources  storage`

If you see `bot` and `sources` at the top level with no `tokenscore` folder,
you're one level too deep — `cd ..` and check again.

If unzipping produced `tokenscore\tokenscore\...`, move the inner folder up one
level and delete the outer.

---

## Step 3 — Confirm you're in the right place

```powershell
Test-Path tokenscore\__init__.py
```

**Expect:** `True`

If `False`, create the missing package markers:

```powershell
New-Item -ItemType File -Force tokenscore\__init__.py, tokenscore\bot\__init__.py, tokenscore\sources\__init__.py, tokenscore\scoring\__init__.py, tokenscore\storage\__init__.py
```

**Stay in `$HOME\projects` for everything below. Never `cd tokenscore`.**

---

## Step 4 — Virtual environment

```powershell
py -3 -m venv venv
```

Then activate. If script execution is blocked (common on a fresh Windows
install), allow it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
venv\Scripts\Activate.ps1
```

**Check:** your prompt now starts with `(venv)`.

The `-Scope Process` matters — it applies only to this window and reverts when
you close it. Don't change the machine-wide policy for this.

---

## Step 5 — Install dependencies

```powershell
pip install aiohttp python-telegram-bot
```

**Check:**

```powershell
py -3 -c "import aiohttp, telegram; print('deps ok')"
```

**Expect:** `deps ok`

If `ModuleNotFoundError`: your venv isn't active. Redo Step 4.

---

## Step 6 — Create your Telegram bot

In the Telegram app:

1. Search **@BotFather**, start the chat
2. Send `/newbot`
3. Display name — anything, e.g. `Token Screener`
4. Username — must end in `bot`, e.g. `kenneth_tokenscore_bot`
5. Copy the token it returns: `7891234567:AAHx-abcdef...`

**Check:** a ~46-character string containing a colon.

That token is a password. Anyone who has it controls your bot.

---

## Step 7 — Get your Telegram user ID

Search **@userinfobot** in Telegram, start it. It replies immediately with
your numeric ID, e.g. `812345678`.

**Check:** you have digits, not a username.

---

## Step 8 — Environment variables

PowerShell has no `export` and no `.env` loading. Create a small script
instead:

```powershell
@'
$env:TELEGRAM_BOT_TOKEN = "paste_token_from_step_6"
$env:TELEGRAM_OWNER_IDS = "paste_number_from_step_7"

# Optional — leave empty for now
$env:BIRDEYE_API_KEY  = ""
$env:SOLSCAN_API_KEY  = ""
$env:RUGCHECK_API_KEY = ""
'@ | Set-Content -Encoding UTF8 env.ps1
```

Now open `env.ps1` in Notepad and replace the two placeholder strings with your
real values:

```powershell
notepad env.ps1
```

Save, close, then load it — note the **leading dot and space**, which
dot-sources the script into your current session:

```powershell
. .\env.ps1
```

**Check:**

```powershell
$env:TELEGRAM_OWNER_IDS
```

**Expect:** your number. Blank means you ran `.\env.ps1` instead of `. .\env.ps1`
— without dot-sourcing, the variables vanish when the script ends.

Add `env.ps1` to `.gitignore` before committing anything.

---

## Step 9 — Verify the data sources

**Do not skip this.** The adapters were written against documented API shapes,
not live responses. A wrong JSON key yields a plausible score and no error —
which means you'd trust output that's meaningless.

```powershell
py -3 -m tokenscore.probe --known
```

Probes WSOL and USDC, both of which definitely have liquidity and holders.

| Result | Meaning | Action |
|---|---|---|
| `OK` | Parser found real data | None |
| `EMPTY` | Resolved to nothing | **Fix the adapter** |
| `ERROR: missing API key` | Expected for birdeye/solscan | None |
| `ERROR: HTTP 403/429` | Rate limited | Wait, retry |

**Minimum expectation:** `dexscreener` and `goplus` report `OK`. Neither needs
a key.

If WSOL shows `EMPTY` on liquidity, the adapter is reading a key the API
doesn't return. Dump the real payload:

```powershell
py -3 -m tokenscore.probe So11111111111111111111111111111111111111112 --raw
```

Compare it against the field names in `tokenscore\sources\adapters.py` and fix
them before Step 10.

---

## Step 10 — Start the bot

```powershell
py -3 -m tokenscore.bot.main
```

**Expect:**

```
INFO  tokenscore | authorised users: [812345678]
INFO  tokenscore | registered 15 commands
INFO  tokenscore | polling…
```

| Error | Cause |
|---|---|
| `TELEGRAM_BOT_TOKEN not set` | Step 8 not loaded — run `. .\env.ps1` |
| `TELEGRAM_OWNER_IDS not set` | Same |
| `InvalidToken` | Token mistyped — recheck Step 6 |
| `ModuleNotFoundError: tokenscore` | Wrong folder — see Step 3 |

Leave this window running. Ctrl+C stops the bot.

---

## Step 11 — Test in Telegram, cheapest first

Open a chat with your bot.

1. **`/start`** — command list appears.
   *Nothing happens?* Wrong bot, or your ID doesn't match Step 7.
2. **`/pools bonk`** — one API call, no scoring.
3. **`/check So11111111111111111111111111111111111111112`** — three sources,
   gates only.
4. **`/score So11111111111111111111111111111111111111112`** — all five.
   Keyless sources showing "unavailable" is correct behaviour.
5. **`/screen`** — **~100 API calls.** Run once, confirm the ranked list, then
   leave it alone until you understand your quota.

Watch the PowerShell window while testing — full errors appear there, not in
Telegram.

---

## Step 12 — Database (optional, later)

The bot runs fine without it. You need Postgres only when you want history and
weight tuning.

Install PostgreSQL for Windows, then:

```powershell
createdb tokenscore
psql tokenscore -f tokenscore\storage\schema.sql
```

Note `-f` rather than `<` — PowerShell's redirection doesn't feed `psql`
correctly.

**Check:** `psql tokenscore -c "\dt"` lists nine tables.

The DB write layer isn't built yet, so these stay empty for now.

---

## Every new PowerShell window needs

```powershell
cd $HOME\projects
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
venv\Scripts\Activate.ps1
. .\env.ps1
```

Worth saving as `start.ps1` so it's one command.

---

## What the scores mean

A high score means no *structural* red flags were found — no mint authority,
adequate liquidity, no honeypot pattern. It does not mean safe, and it is not
a recommendation. Deployers test against these same public scanners before
launching, and a token can pass every check and still be a slow rug.

Treat it as a filter that removes obvious traps. Size positions on the
assumption that anything passing can still go to zero.
