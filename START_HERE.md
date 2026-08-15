# Start here

Ignore every other file for now. Do these three things. Nothing else.

Estimated time: 20 minutes.

---

## 1. Unzip this folder

Right-click the zip → **Extract All** → extract to:

```
C:\Users\YourName\projects
```

When you're done you should have:

```
C:\Users\YourName\projects\tokenscore-bot\tokenscore\
```

That's a folder called `tokenscore` **inside** a folder called
`tokenscore-bot`. Two levels. If you only see one, extract again.

---

## 2. Install Python

Go to **python.org/downloads** and download Python for Windows.

Run the installer. On the very first screen there's a checkbox at the bottom:

> ☑ **Add python.exe to PATH**

**Tick it.** If you miss it, nothing later will work and the error message
won't tell you why.

Then click Install Now.

**Check it worked:** open PowerShell (Start menu → type "PowerShell" → Enter)
and run:

```powershell
py -3 --version
```

You should see `Python 3.13.x` or similar.

If you get an error or the Microsoft Store opens, the PATH checkbox was
missed. Uninstall Python, reinstall, tick the box.

---

## 3. Create your Telegram bot

This part is all inside the Telegram app. No terminal.

**Get the bot token:**

1. Search for **@BotFather**
2. Tap Start
3. Send `/newbot`
4. It asks for a name → type anything, e.g. `My Screener`
5. It asks for a username → must end in `bot`, e.g. `kenneth_screener_bot`
6. It replies with a long code like `7891234567:AAHx-abcdefGH...`

**Save that code somewhere.** That's your bot token.

**Get your user ID:**

1. Search for **@userinfobot**
2. Tap Start
3. It instantly replies with a number like `812345678`

**Save that number too.**

---

## Stop here

You now have:

- [ ] Folder extracted to `C:\Users\YourName\projects\tokenscore-bot`
- [ ] `py -3 --version` prints a version number
- [ ] A bot token (long code with a colon)
- [ ] Your user ID (just digits)

Once all four are ticked, come back and say so. The next part is four commands
and then the bot is running.

Don't open `FULL_GUIDE.md` yet — it covers steps 4 through 12 and will only
make this feel bigger than it is.
