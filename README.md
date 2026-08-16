# EAC Consumption Dashboard

Local Streamlit dashboard for your own EAC (Electricity Authority of Cyprus)
meter readings — pulls data from the same API the meterreading-dso portal
uses and charts daily/weekly/monthly consumption.

## Setup

```bash
cd eac-consumption-dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py
```

Opens at http://localhost:8501, and asks for your EAC portal email and password.

To skip that form while developing, copy `.env.example` to `.env`, fill in
`EAC_EMAIL` / `EAC_PASSWORD` and set `EAC_DEV_AUTOLOGIN=1`. Don't set that flag
on anything other people can reach — it signs every visitor in as that account.

## Notes

- Nothing is stored: the password is exchanged for a JWT and immediately
  discarded, the token lives in the Streamlit session only, and when it expires
  (~24h) you're asked to log in again. `.env` is git-ignored — never commit it.
- `readings/list` response shapes (login response, service point detail,
  reading fields) were inferred from the portal's own UI/network calls but
  not exhaustively verified — if `app.py` errors out on first run, the error
  messages print the raw JSON keys seen, which is usually enough to adjust
  the field-name matching in `eac_client.py`.
- Remote access (e.g. checking from your phone) is left up to you — Tailscale
  is the simplest option since it doesn't expose the app publicly.
