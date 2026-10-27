# Divar Finder

Persian Telegram bot + web dashboard for monitoring new Divar home-for-sale listings in Shiraz.

## Important about speed
Divar does not provide a generally available official public real-estate listing push API. The app therefore checks the configured search page on a short polling interval (default 20 seconds) and sends newly discovered listings. Delivery speed depends on Divar page accessibility, server/network latency and rate limits; this is near-real-time polling, not a guarantee of instant notification. Use a respectful interval and stop monitoring if the site blocks automated access. Do not bypass CAPTCHAs, login walls or access controls.

## Features
- Persian RTL web dashboard
- Telegram bot commands: `/start`, `/latest`, `/filters`, `/help`
- Per-source toggles for Divar, public Telegram channels, Sheypoor, Kilid, Iranfile, MelkRadar, Zoomila, and Instagram
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

## Source selection and Instagram
Use the source checkboxes in the dashboard to enable or disable each source independently. The selected sources are saved in SQLite settings.

Instagram collection uses the official Instagram Graph API, not HTML scraping. To enable it, configure `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ID`, and a comma-separated list of public professional account usernames in `INSTAGRAM_PROPERTY_ACCOUNTS` in `.env`, then rebuild/restart the service. The token/business account must have the required Meta permissions and Business Discovery access to the target accounts. Personal/private accounts and accounts not available through the API cannot be collected. If credentials are absent, Instagram is skipped without interrupting other sources.

The public-web adapters for Sheypoor, Kilid, Iranfile, MelkRadar and Zoomila depend on those sites being discoverable and accessible; search engines can rate-limit or return no results. The dashboard toggles control whether they run, but cannot guarantee results when a site or search engine blocks requests. Check `docker compose logs --tail=200 divar-finder` for per-source status.


## Divar search URL
Create the desired search on Divar in your browser and paste its URL into the dashboard. The collector parses accessible listing links and visible text from the search page. Divar may change its HTML or restrict automated access; check the dashboard status/logs if collection stops. The app intentionally does not bypass access controls.

## Development
Python 3.11+: `pip install -r requirements.txt && uvicorn app:app --host 0.0.0.0 --port 8080`
