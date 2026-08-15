Set-Location "$HOME\projects\tokenscore-bot"
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
. .\venv\Scripts\Activate.ps1
. .\env.ps1
python -m tokenscore.bot.main
