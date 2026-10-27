# Divar Finder

Persian Telegram bot + web dashboard for monitoring new Divar home-for-sale listings in Shiraz.

## Important about speed
Divar does not provide a generally available official public real-estate listing push API. The app therefore checks the configured search page on a short polling interval (default 20 seconds) and sends newly discovered listings. Delivery speed depends on Divar page accessibility, server/network latency and rate limits; this is near-real-time polling, not a guarantee of instant notification. Use a respectful interval and stop monitoring if the site blocks automated access. Do not bypass CAPTCHAs, login walls or access controls.

## Features
- Persian RTL web dashboard
- Telegram bot commands: `/start`, `/latest`, `/filters`, `/help`
- Search URL, maximum price (default 11 billion toman), minimum/maximum area, bedrooms, parking and neighborhoods
- Polling interval, deduplication by listing URL/ID, SQLite persistence
- Docker Compose deployment
- Dashboard protected by an admin password; Telegram commands restricted to configured chat IDs

## Quick install
1. Copy `.env.example` to `.env` and set `BOT_TOKEN`, `ADMIN_PASSWORD`, and `ALLOWED_TELEGRAM_IDS`.
2. Set a strong `SECRET_KEY` and `BASE_URL` if applicable.
3. Run `docker compose up -d --build`.
4. Open `http://YOUR_SERVER_IP:8080`, sign in with the admin password, and configure the Divar search URL and filters.
5. Send `/start` to your Telegram bot. Add your numeric Telegram user/chat ID to `ALLOWED_TELEGRAM_IDS`.

Use HTTPS via a reverse proxy before exposing the dashboard publicly. Never commit `.env` or your bot token to GitHub.

## Divar search URL
Create the desired search on Divar in your browser and paste its URL into the dashboard. The collector parses accessible listing links and visible text from the search page. Divar may change its HTML or restrict automated access; check the dashboard status/logs if collection stops. The app intentionally does not bypass access controls.

## Development
Python 3.11+: `pip install -r requirements.txt && uvicorn app:app --host 0.0.0.0 --port 8080`
