# Divar Finder

Persian Telegram bot + web dashboard for monitoring new Divar home-for-sale listings in Shiraz.

## Important about speed
Divar does not provide a generally available official public real-estate listing push API. The app therefore checks the configured search page on a short polling interval (default 20 seconds) and sends newly discovered listings. Delivery speed depends on Divar page accessibility, server/network latency and rate limits; this is near-real-time polling, not a guarantee of instant notification. Use a respectful interval and stop monitoring if the site blocks automated access. Do not bypass CAPTCHAs, login walls or access controls.

## Features
- Persian RTL web dashboard
- Telegram bot commands: `/start`, `/latest`, `/filters`, `/help`
- Per-source toggles for Divar, public Telegram channels, Sheypoor, Kilid, Iranfile, MelkRadar, Zoomila, and Instagram
- Search URL, maximum price (default 11 billion toman), minimum/maximum area, bedrooms, parking and neighborhoods
- Optional official Divar Kenar API integration to load the current Shiraz district names in the Telegram neighborhood picker
- Polling interval, deduplication by listing URL/ID, SQLite persistence
- Docker Compose deployment
- Dashboard protected by an admin password; Telegram commands restricted to configured chat IDs

## Quick install (copy and paste)

On a fresh Ubuntu/Debian server with Docker Compose installed, copy and run this block. It clones the project, creates a secure dashboard password and secret key, then starts the panel on port **9000**.

```bash
git clone https://github.com/Arshak888/Divar-finder.git
cd Divar-finder
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path(".env")
s = p.read_text()
password = secrets.token_urlsafe(16)
secret = secrets.token_urlsafe(48)
s = s.replace("ADMIN_PASSWORD=please_set_a_long_random_password", f"ADMIN_PASSWORD={password}")
s = s.replace("SECRET_KEY=replace_with_a_long_random_secret", f"SECRET_KEY={secret}")
p.write_text(s)
print("\nDashboard login password (save this now):", password)
PY
docker compose up -d --build
```

When the build finishes, open `http://YOUR_SERVER_IP:9000` and sign in using the password printed by the script. In the dashboard, open **تنظیمات ربات تلگرام** to enter the bot token and numeric Telegram admin IDs, then save/restart the bot. You can also edit server settings with `nano .env` and apply them using `docker compose up -d --build`.

**Requirements:** Git, Python 3, and Docker with the Docker Compose plugin must already be installed. Allow inbound TCP port 9000 in your server firewall/security group. For public access, use HTTPS through a reverse proxy. Never commit `.env` or share the generated password/token.

## Official Divar neighborhood API (Kenar)
The Telegram neighborhood selector can load the current district labels from Divar's official Kenar API.

1. Create or rotate an API key in the Divar Kenar developer dashboard. If a key has been pasted into a chat, issue a replacement and revoke the exposed key.
2. Set `DIVAR_API_KEY` as an environment secret on the host where the app runs. For Replit, use the project's Secrets/environment variables panel. For Docker, add it to the server-side `.env` file. Do not paste the key into source code or commit it to GitHub.
3. Rebuild/restart the app. On startup/use of the neighborhood picker, check logs for `Divar official districts loaded: N`. If the API is unavailable or the key lacks access, the app logs a diagnostic and keeps the fallback list.
4. The request uses `GET https://open-api.divar.ir/v1/open-platform/assets/district/shiraz` with the `X-API-Key` header. The successful response's `districts[].display` values are used in the picker and cached in memory for up to one hour.

Official docs: https://divar-ir.github.io/kenar-docs/openapi-doc/assets-get-districts

Important: this endpoint supplies neighborhood names for the selector; it does not fetch property listings. Listing collection remains dependent on the configured Divar search page and its accessibility. The current neighborhood filter matches selected labels against the listing title/location text, so loading official names alone does not guarantee that every listing in those neighborhoods will be found.

## Source selection and Instagram
Use the source checkboxes in the dashboard to enable or disable each source independently. The selected sources are saved in SQLite settings.

Instagram collection uses the official Instagram Graph API, not HTML scraping. To enable it, configure `INSTAGRAM_ACCESS_TOKEN`, `INSTAGRAM_BUSINESS_ID`, and a comma-separated list of public professional account usernames in `INSTAGRAM_PROPERTY_ACCOUNTS` in `.env`, then rebuild/restart the service. The token/business account must have the required Meta permissions and Business Discovery access to the target accounts. Personal/private accounts and accounts not available through the API cannot be collected. If credentials are absent, Instagram is skipped without interrupting other sources.

The public-web adapters for Sheypoor, Kilid, Iranfile, MelkRadar and Zoomila depend on those sites being discoverable and accessible; search engines can rate-limit or return no results. The dashboard toggles control whether they run, but cannot guarantee results when a site or search engine blocks requests. Check `docker compose logs --tail=200 divar-finder` for per-source status.

## Divar search URL
Create the desired search on Divar in your browser and paste its URL into the dashboard. The collector parses accessible listing links and visible text from the search page. Divar may change its HTML or restrict automated access; check the dashboard status/logs if collection stops. The app intentionally does not bypass access controls.

## Development
Python 3.11+: `pip install -r requirements.txt && uvicorn app:app --host 0.0.0.0 --port 8080`
