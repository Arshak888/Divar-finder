# Multi-source roadmap: Divar Finder

## Goal
Keep the existing Telegram bot, filters, SQLite history and Divar polling intact while adding independently switchable listing sources for home sales in Shiraz.

## Source strategy
1. Divar: existing adapter; preserve as baseline.
2. Sheypoor: add an adapter only after validating current public search-page structure and permitted access.
3. Specialist real-estate websites: implement one adapter per verified site. Do not assume every site shares a schema.
4. Public Instagram posts: use approved Meta API/access paths where eligible, or monitor explicitly supplied public account/feed URLs only where permitted. Do not promise global Instagram search; public visibility does not imply API availability. Do not bypass login, rate limits, CAPTCHAs, or access controls.
5. Public Telegram channels: use Telegram Bot API for channels the bot is permitted to access. A bot cannot silently read every public channel; configure channels explicitly.

## Pipeline
Each adapter returns a normalized record with source, source-local id, title, price, area, bedrooms, location, URL, source-provided created_at, and a cross-source fingerprint. The aggregator should run adapters independently, isolate per-source failures, apply shared filters, deduplicate, then save and notify. One source outage must not stop other sources.

## Delivery phases
- Phase 1: adapter interface, normalized schema, source health/error reporting and cross-source deduplication.
- Phase 2: verified Sheypoor adapter.
- Phase 3: verified specialist-site adapters.
- Phase 4: opt-in Telegram public-channel sources.
- Phase 5: Instagram integration only within access granted by Meta or a permitted public feed.

## Acceptance criteria
- Existing Divar behavior and Telegram filter controls continue to work.
- Each source can be toggled independently.
- One source failure does not interrupt the others.
- Every message identifies the source and links to the original listing.
- Exact creation dates are never fabricated; if unavailable, display «تاریخ انتشار در منبع موجود نیست».
- Duplicate listings from multiple sources produce one notification while retaining source links.
- Logs report fetched/matched/new counts per source without logging credentials or private content.
