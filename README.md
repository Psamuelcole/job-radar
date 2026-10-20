# Job Radar

Checks remote job feeds every 15 minutes, keeps fresh GTM and marketing roles open to Nigeria, skips jobs already sent, and sends new matches to Telegram. Sends a daily application count at 8pm Lagos time.

Runs free on GitHub Actions (public repository).

## Setup

In the repository, go to Settings > Secrets and variables > Actions > New repository secret, and add:

- `TELEGRAM_BOT_TOKEN`: your Telegram bot token (from BotFather)
- `TELEGRAM_CHAT_ID`: your Telegram chat ID
- `DAILY_COUNT_CSV_URL` (optional): the published CSV link of the "Daily Count" tab in your job tracker

Then go to Actions > Job Radar > Run workflow to test it once.

## Files

- `radar.py`: the radar itself
- `seen.json`: links already sent, updated automatically after each run
- `.github/workflows/radar.yml`: the schedule
