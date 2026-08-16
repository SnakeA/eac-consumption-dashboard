"""Thin client for the EAC Distribution meter-reading portal API."""
import base64
import json
import time

import pandas as pd
import requests

BASE_URL = "https://meterreading-dso.eac.com.cy"


def _decode_jwt(token: str) -> dict:
    """Read a JWT's payload without verifying it.

    Signature verification is the server's job - we only need `exp` (to know
    when to re-login) and `sub` (a stable per-account id). Returns {} if the
    token isn't decodable, so callers must handle missing keys.
    """
    try:
        payload_b64 = token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        return {}


class SessionExpired(RuntimeError):
    """The JWT has run out and there is no password held to get a new one."""


class EacClient:
    def __init__(self, email: str, password: str):
        self.email = email
        self.password = password
        self._token = None
        self._token_exp = 0
        self._user_id = None
        self._session = requests.Session()

    def forget_password(self):
        """Drop the password, keeping only the JWT.

        Lets a caller that took the password from a login form avoid holding it
        for the life of the session. The client keeps working until the token
        expires (EAC issues 24h tokens), then raises SessionExpired instead of
        silently re-logging-in, because there is nothing left to log in with.
        """
        self.password = None

    @property
    def is_authenticated(self) -> bool:
        """True while the current token is still usable."""
        return bool(self._token) and time.time() <= self._token_exp - 30

    def _login(self):
        if not self.password:
            raise SessionExpired("Session expired - log in again.")
        resp = self._session.post(
            f"{BASE_URL}/api/portal/login",
            json={"email": self.email, "password": self.password},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        # Response shape isn't fully confirmed - handle the common cases.
        if isinstance(data, str):
            token = data
        elif isinstance(data, dict):
            token = data.get("token") or data.get("accessToken") or data.get("jwt")
        else:
            token = None
        if not token:
            raise RuntimeError(f"Could not find JWT in login response: {data!r}")
        self._token = token
        claims = _decode_jwt(token)
        self._token_exp = claims.get("exp") or (time.time() + 300)
        self._user_id = claims.get("sub")

    def _auth_headers(self) -> dict:
        if not self._token or time.time() > self._token_exp - 30:
            self._login()
        return {"Authorization": f"Bearer {self._token}"}

    @property
    def user_id(self) -> str:
        """Stable per-account id, taken from the JWT's `sub` claim.

        Used to scope cache keys and database rows so one session can never
        serve another account's data. Logs in if there's no token yet.
        """
        if not self._user_id:
            self._auth_headers()
        if not self._user_id:
            raise RuntimeError("JWT has no `sub` claim - cannot identify the account.")
        return str(self._user_id)

    def get_service_points(self) -> list:
        resp = self._session.get(
            f"{BASE_URL}/api/portal/servicePoints",
            headers=self._auth_headers(),
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def get_service_point_detail(self, sp_id: str) -> dict:
        resp = self._session.get(
            f"{BASE_URL}/api/portal/servicePoints/{sp_id}",
            headers=self._auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def discover_channels(self, sp_id: str) -> list[dict]:
        """List measuring channels for a service point's meter(s).

        servicePoints/{spId} returns a list of physical meters (one per
        install/replacement), each with configurationsList -> mcList, where
        mcList entries carry the channel id under the plain key "id" (not
        "mcId" - that name is only used by the readings/list request body).
        A meter's current configuration is the one with no endDate.
        """
        devices = self.get_service_point_detail(sp_id)
        found = []
        for device in devices:
            serial_number = device.get("serialNumber")
            for config in device.get("configurationsList", []):
                active = not config.get("endDate")
                for mc in config.get("mcList", []):
                    found.append(
                        {
                            "mc_id": mc.get("id"),
                            "label": f"{serial_number} · {mc.get('type')} ({mc.get('uom')})"
                            + ("" if active else " [inactive]"),
                            "serial_number": serial_number,
                            "active": active,
                            "type": mc.get("type"),
                        }
                    )
        return found

    def get_readings(
        self, sp_id: str, mc_id: str, start_date, end_date, truncate_to_date: bool = True
    ) -> pd.DataFrame:
        """Fetch readings between two dates (inclusive).

        start_date / end_date: datetime.date or datetime.datetime.
        Returns a DataFrame with columns: date, reading (cumulative meter
        value), consumption (server-computed delta - matches the portal's
        own Consumption/Production tab exactly). `reading` is NaN for
        channels that don't report a cumulative value (e.g. the 30-min
        load-profile channels only report the interval delta). Pass
        truncate_to_date=False to keep time-of-day (needed for load-profile
        channels, which report multiple records per day).

        The window `readings/list` actually applies drifts with daylight saving.
        It returns naive local timestamps, and the `Z` we send is wrong by the
        local offset - two hours in winter, three in summer - so the effective
        start lands at 00:00 in winter but 01:00 in summer. Because daily
        readings sit exactly on midnight, that one hour is enough to include or
        exclude a whole day: winter ranges come back a day early (ask for 1-5
        Feb, get 31 Jan - 4 Feb). Rather than guess how the server parses
        offsets, request a day of slack on each side and select the correct rows
        here. Note the slack costs ~96 extra load-profile records against the
        API's ~1000-record cap, so keep load-profile windows under ~18 days.
        """
        # A reading is stamped at the *end* of the interval it measures, so
        # consumption for end_date arrives stamped end_date + 1 day at midnight:
        # the range is open at the bottom and closed at the top.
        lo = pd.Timestamp(start_date).normalize()
        hi = pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1)
        body = {
            "spId": sp_id,
            "mcId": mc_id,
            "startDate": (lo - pd.Timedelta(days=1)).isoformat() + "Z",
            "endDate": (hi + pd.Timedelta(days=1)).isoformat() + "Z",
        }
        resp = self._session.post(
            f"{BASE_URL}/api/portal/readings/list",
            json=body,
            headers={**self._auth_headers(), "Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        # Top level is a list wrapping one object per requested channel.
        channel_block = (data[0] if data else {}) if isinstance(data, list) else data
        records = channel_block.get("readings", []) if isinstance(channel_block, dict) else []
        if not records:
            return pd.DataFrame(columns=["date", "reading", "consumption"])

        df = pd.json_normalize(records).rename(
            columns={"dt": "date", "reading": "reading", "value": "consumption"}
        )
        if "date" not in df.columns:
            raise RuntimeError(
                f"Unexpected readings shape for mcId={mc_id}. "
                f"Columns found: {list(df.columns)}. Sample record: {records[0]}"
            )
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df["date"] > lo) & (df["date"] <= hi)].copy()
        if df.empty:
            return pd.DataFrame(columns=["date", "reading", "consumption"])
        if truncate_to_date:
            # Label each row with the day it measures, not the midnight it is
            # stamped at. Measured on 2026-08-01: the 48 load-profile intervals
            # summed to 3.91 kWh and the daily row stamped 08-02 read 4, while
            # 08-01 read 9 - so the stamp is the interval end and a bar labelled
            # from it is a day late.
            df["date"] = (df["date"] - pd.Timedelta(days=1)).dt.date
        df["reading"] = pd.to_numeric(df["reading"]) if "reading" in df.columns else pd.NA
        df["consumption"] = pd.to_numeric(df["consumption"])
        return df[["date", "reading", "consumption"]].sort_values("date").reset_index(drop=True)
