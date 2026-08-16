# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A local, single-user Streamlit dashboard that charts electricity consumption from a personal
EAC (Electricity Authority of Cyprus) account, by calling the same private API the
meterreading-dso web portal uses. There is no backend of its own — `app.py` talks directly to
the remote API via `eac_client.py`.

## Setup & running

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py   # opens at http://localhost:8501, asks you to log in
```

To skip the login form locally, put `EAC_EMAIL` / `EAC_PASSWORD` in `.env` and
set `EAC_DEV_AUTOLOGIN=1`. The flag is deliberately opt-in rather than implied
by the variables being present: a deployment with a stray `.env` would otherwise
sign every visitor in as that account.

There is no test suite, linter, or build step configured for this project.

## Architecture

Two files:

- **`eac_client.py`** — `EacClient` wraps the EAC portal's REST API (`BASE_URL =
  https://meterreading-dso.eac.com.cy`). Handles login (`POST /api/portal/login`), JWT
  storage/expiry (decoded client-side from the token's `exp` claim, refreshed ~30s before
  expiry via `_auth_headers()`), and the three read endpoints: service points, service point
  detail (physical meters/channels), and `readings/list` (actual consumption data). Returns
  a `pandas.DataFrame` from `get_readings`.
- **`app.py`** — Streamlit UI. Wires sidebar selectors (service point → measuring channel →
  date range) to `EacClient` calls, then renders daily/rolling-average/monthly charts with
  Plotly.

Caching layers (important when changing data-loading logic): `st.cache_resource` holds the
single `EacClient` instance; `st.cache_data` wraps `load_service_points` (1h TTL),
`load_channels` (1h TTL), and `load_readings` (30min TTL). The sidebar "Refresh data" button
calls `st.cache_data.clear()`.

## The API is reverse-engineered, not documented

This is the most important thing to know when touching `eac_client.py`. The EAC portal has no
public API docs — response shapes (login response, service point detail, `mcList` channel
entries, `readings/list` records) were inferred from the portal's own network calls and are
**not exhaustively verified**. Consequences:

- Code defensively checks multiple possible key names (e.g. `token`/`accessToken`/`jwt` in
  the login response) rather than assuming one shape.
- When something breaks, prefer widening the field-matching logic over assuming the API
  contract — errors are written to surface the raw JSON keys seen (see `RuntimeError` messages
  in `get_readings` and the "Raw service point detail (debug)" expander in `app.py`) so the
  actual shape can be read from there.
- `discover_channels` relies on a specific quirk: a measuring channel's id is under the key
  `"id"` in `servicePoints/{spId}` responses, but the same value is called `"mcId"` in the
  `readings/list` request body. Don't conflate the two when editing this logic.
- A meter's *active* configuration is the one with no `endDate` in `configurationsList`.
- Not every channel returns the same fields: the 30-minute load-profile channels omit the
  cumulative `reading` value and report multiple records per day, which is why
  `get_readings` has a `truncate_to_date` flag and tolerates a missing `reading` column.

## Timestamps are interval-end, and the API's window drifts with DST

Two related things `get_readings` compensates for. Both are measured, not assumed — don't
"simplify" them away.

- **A reading is stamped at the end of the interval it measures.** The daily row stamped
  `2 Aug 00:00` is consumption *during 1 Aug*: on 2026-08-01 the 48 load-profile intervals
  summed to 3.91 kWh, the daily row stamped 08-02 read 4, and 08-01 read 9. So
  `truncate_to_date=True` labels each row with `ts` minus a day.
- **`readings/list` applies a window that shifts with daylight saving.** It returns naive local
  timestamps and we send `...Z`, which is wrong by the local offset (UTC+2 winter, UTC+3
  summer), so the effective start lands at 00:00 in winter but 01:00 in summer. Since daily
  readings sit exactly on midnight, that one hour includes or excludes a whole day — winter
  ranges used to come back a day early (ask for 1–5 Feb, get 31 Jan–4 Feb). `get_readings` now
  requests a day of slack on each side and selects `start < ts <= end + 1 day` locally, rather
  than guessing how the server parses offsets. The slack costs ~96 load-profile records against
  the API's ~1000-record cap, so keep load-profile windows under ~18 days.

`db.fetch_readings` applies the same bounds and the same day-shift, so the archive and a live
API call return identical frames. That means ingestion must store *raw* stamps —
`get_readings(..., truncate_to_date=False)` for every channel, daily ones included — or the
day gets shifted off twice.

The portal also has a **data-sharing API** (`share/grant`, `share/revoke`, and unauthenticated
`public/share/view` + `report/sharedreport/{html,xlsx}` keyed on a share token). It was
evaluated as a way to avoid handling passwords and rejected: shared reports return only the
`S-KWH-24H` channel as HTML/XLSX — no peak/off-peak split, no 30-minute load profile, no JSON.
Don't re-investigate it as a replacement for `readings/list`.

## Which measuring channels matter

`docs/channels.md` records what each of the meter's 11 channels actually carries, measured
against the live account. Read it before changing channel handling — several of the
name-based assumptions you'd naturally make are wrong (notably, the `EXP` suffix means
different things on the kWh and reactive channels). It also documents the per-channel
freshness difference (daily channels lag ~10 days, the load profile ~3) and lists what to
re-enable if solar is ever installed.

## Credentials

Nothing is persisted. `app.py` shows a login form, `_authenticate` exchanges the credentials
for a JWT and calls `EacClient.forget_password()` immediately, and the client lives in
`st.session_state` — per-session, never `st.cache_resource`, which is shared server-wide and
would hand one account's data to everyone. When the token expires (~24h) `_login` raises
`SessionExpired` rather than silently re-authenticating, because there is no password left to
do it with; the app catches that by checking `is_authenticated` and re-shows the form.

`EAC_EMAIL` / `EAC_PASSWORD` in `.env` (git-ignored, never commit) are only read when
`EAC_DEV_AUTOLOGIN=1` is also set.

**A browser refresh ends the session and this is deliberate.** `st.session_state` is
per-websocket, so a reload discards the JWT and re-shows the form. Widget interaction (date
range, channel, Refresh data) reruns over the existing connection and keeps the session — only
a hard reload, a closed tab or the ~24h expiry loses it. Don't "fix" this by putting the JWT in
a cookie: Streamlit can't set one natively, so it means a third-party component holding a
bearer token where page scripts can read it. If refresh-survival is ever wanted, key a
server-side token store on Cloudflare Access's verified `Cf-Access-Authenticated-User-Email`
header (rejecting requests that lack it) so the token never reaches the browser at all.

Every `@st.cache_data` function takes a `user_key` argument it never uses. Its only job is to
scope the cache entry to one account — `st.cache_data` is shared across sessions, so without it
the argument-less and `sp_id`-keyed caches collide between users. Keep passing it.
