Pocket Option OTC Signal Bot
Telegram bot for an EMA + RSI + volatility confluence engine. Runs in
manual paste mode: Pocket Option OTC pairs are a synthetic feed with no
legitimate public API, so you paste recent candles from the platform and the
bot replies with a signal — or tells you it's staying out.
Files
`signal_engine.py` — confluence logic (votes-based, abstains on disagreement)
`formatter.py` — Telegram message formatting (HTML parse mode)
`bot.py` — bot commands + message handling + SQLite outcome logging
`requirements.txt`, `Procfile` — deployment
1. Create the bot on Telegram
Message @BotFather on Telegram
`/newbot`, follow the prompts, copy the token it gives you
2. Run locally (optional, to test before deploying)
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in BOT_TOKEN
export $(cat .env | xargs)   # or use python-dotenv in bot.py
python bot.py
```
3. Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit: confluence engine + Telegram bot"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/pocket-signal-bot.git
git push -u origin main
```
`.env` and `signals.db` are gitignored — never commit your bot token.
4. Deploy to Railway
railway.app → New Project → Deploy from GitHub repo
Select this repo
Railway auto-detects Python + the `Procfile` and runs `python bot.py` as a worker
In the Railway project → Variables, add:
`BOT_TOKEN` = your token from BotFather
`CHANNEL_ID` (optional) = your channel, see below
`STAKE_AMOUNT` (optional) = e.g. `$10`
Deploy. Check the Logs tab for `Bot starting (manual paste mode...)`
Posting signals to a Telegram channel
Create a channel in Telegram: pencil icon → New Channel → name it → Public or Private
Open the channel → Manage Channel → Administrators → Add Admin → search for your bot's username → add it, with Post Messages enabled
Get the channel ID:
Public channel → use `@yourchannelusername`
Private channel → forward any message from it to @RawDataBot or @userinfobot, which will show a numeric ID like `-1001234567890`
Set `CHANNEL_ID` to that value in Railway's Variables tab
Once set, every signal (fired or abstain) posts to the channel automatically, in
addition to replying wherever the candles were pasted.
Persisting outcome logs across deploys
Railway's default filesystem is ephemeral — `signals.db` resets on every
redeploy. If you want win/loss history to survive redeploys, attach a
Railway Volume and point
`DB_PATH` at a file inside it (e.g. `/data/signals.db`).
Using the bot
```
/signal EURUSD-OTC 1m
```
then paste candles, oldest first, one per line:
```
1.08421,1.08430,1.08415,1.08427
1.08427,1.08440,1.08420,1.08435
...
```
The bot replies with a CALL/PUT signal + confidence + vote breakdown, or an
abstain with the reason. After the trade settles:
```
/log win
```
or
```
/log loss
```
`/stats` shows your win rate for that chat.
Design notes
The engine abstains when fewer than 2 of 3 indicators agree — quiet by
design, not a bug.
Confidence % is the real fraction of agreeing indicators (2/3 ≈ 67%,
3/3 = 100%), not a fabricated number.
Flat stake only. No martingale ladder.
