import datetime as dt
import os

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
from dotenv import load_dotenv

import billing_periods as bp
from eac_client import EacClient

load_dotenv()

# The meter exposes 11 channels, most of which are zero or not billable for a
# domestic customer. These are the only ones worth offering by default - see
# docs/channels.md for the measured evidence and for what to re-enable if solar
# is ever installed.
CHANNEL_ORDER = ["S-KWH-24H", "S-KWH-NORMAL", "S-KWH-OFFPEAK"]
CHANNEL_NAMES = {
    "S-KWH-24H": "Total consumption",
    "S-KWH-NORMAL": "Peak-rate only",
    "S-KWH-OFFPEAK": "Off-peak only",
}

st.set_page_config(page_title="My electricity", page_icon="⚡", layout="wide")

TITLE = "⚡ My electricity"
TAGLINE = "Understand when and how your home uses energy."
PRIMARY = "#176B87"
ACCENT = "#F4B942"
MUTED = "#7A8A93"


# Auto-login from .env is opt-in via EAC_DEV_AUTOLOGIN=1, not automatic on the
# variables being present: a deployment that happens to have a .env sitting next
# to it would otherwise sign every visitor in as that account.
DEV_AUTOLOGIN = os.environ.get("EAC_DEV_AUTOLOGIN") == "1"


# The client is per-session, NOT @st.cache_resource: that cache is shared across
# every session on the server, so a single cached client would serve one account's
# data to everyone. Likewise every @st.cache_data function below takes user_key,
# which does nothing but scope the cache entry to one account - without it the
# argument-less/sp-keyed caches collide across users.
def _log_login_failure(label: str, exc: Exception) -> None:
    """Record why a login failed, without recording who tried.

    The email is deliberately left out: on a shared deployment the logs belong
    to whoever runs the app, and a visitor's address is not theirs to collect.
    Community Cloud buffers stdout, hence the explicit flush.
    """
    print(f"[login] failed: {label}: {exc}", flush=True)


def _authenticate(email: str, password: str) -> str | None:
    """Exchange credentials for a JWT and keep only the JWT.

    The password is dropped as soon as it has been spent (see
    EacClient.forget_password), so nothing but a ~24h token lives in the
    session, and nothing at all is written to disk. When that token expires the
    session ends and the visitor logs in again.

    Returns None on success, or the message to show the visitor. Failures are
    told apart rather than collapsed into "wrong password", which was actively
    misleading once this ran anywhere but a laptop: a deployed copy reaches EAC
    from a datacentre in another country, so being blocked or unable to connect
    is at least as likely as a typo. Deployments also force
    showErrorDetails=false, so the log line above is the only place the
    underlying error survives.
    """
    client = EacClient(email, password)
    try:
        # Assigned, not left bare: Streamlit's "magic" rewrites a bare expression
        # into st.write() and would print the account email onto the page.
        _ = client.user_id  # forces the login round-trip
    except requests.HTTPError as exc:
        response = exc.response
        status = response.status_code if response is not None else None
        # The body matters more than the code here. Measured against the live
        # portal: bad credentials come back 403, not 401, with a JSON body whose
        # "path" is /api/portal/login. A network-level block would also be 403 -
        # so the code alone can't tell a typo from a firewall, and the body is
        # logged to settle it.
        body = (response.text[:300] if response is not None else "")
        _log_login_failure(f"HTTP {status}", RuntimeError(body or str(exc)))
        if status in (400, 401, 403):
            return "Login failed — check your email and password."
        return f"EAC returned an unexpected error (HTTP {status})."
    except requests.RequestException as exc:
        _log_login_failure(type(exc).__name__, exc)
        return "Could not reach the EAC portal from this server."
    except Exception as exc:
        _log_login_failure(type(exc).__name__, exc)
        return f"Unexpected error while signing in ({type(exc).__name__})."
    client.forget_password()
    st.session_state.client = client
    st.session_state.pop("session_ended", None)
    return None


def get_client() -> EacClient:
    client = st.session_state.get("client")
    if client is not None and client.is_authenticated:
        return client

    if client is not None:
        st.session_state.pop("client", None)
        st.session_state.session_ended = "expired"

    # Once a session has ended, the form is always shown - otherwise dev
    # auto-login would sign you straight back in and "Log out" would do nothing.
    ended = st.session_state.get("session_ended")

    if DEV_AUTOLOGIN and not ended:
        email, password = os.environ.get("EAC_EMAIL"), os.environ.get("EAC_PASSWORD")
        if email and password and _authenticate(email, password) is None:
            return st.session_state.client
        st.error("EAC_DEV_AUTOLOGIN is set but EAC_EMAIL / EAC_PASSWORD did not work.")
        st.stop()

    # The login page is its own screen: centred, no sidebar (nothing below has
    # run yet, so no widgets exist to render there) and nothing else on it.
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        st.title(TITLE)
        st.caption(TAGLINE)
        if ended == "expired":
            st.warning("Your session expired. Please log in again.")
        elif ended == "logout":
            st.success("Signed out.")
        # Neither field is given a key: a keyed widget keeps its value in
        # st.session_state, and the password has no business living there. That
        # rules out literally disabling the button while the request is in
        # flight - doing so needs the submit handled on a later run, which needs
        # the credentials to survive a rerun. Marking the run busy and rendering
        # a disabled button in the same pass does not work either: a disabled
        # submit button reports submitted=False, so the login never fires and
        # the form sticks on "Signing in..." forever. A spinner plus the guard
        # below gives the same protection without holding the password.
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Log in", type="primary", width="stretch")
        if submitted and not st.session_state.get("logging_in"):
            if not email or not password:
                st.error("Enter both your email and password.")
            else:
                st.session_state.logging_in = True
                try:
                    with st.spinner("Signing in…"):
                        problem = _authenticate(email, password)
                finally:
                    # Cleared even if the round-trip raises, or a stray failure
                    # would lock the form for the rest of the session.
                    st.session_state.logging_in = False
                if problem:
                    st.error(problem)
                else:
                    st.rerun()
        st.caption(
            "Use your EAC portal account. Credentials go straight to "
            "meterreading-dso.eac.com.cy — nothing is stored here, and you are "
            "signed out when the session ends."
        )
    st.stop()


def header(user_key: str):
    """Title on the left, the signed-in account and a way out on the right."""
    title_col, account_col = st.columns([3, 1], vertical_alignment="center")
    with title_col:
        st.title(TITLE)
        st.caption(TAGLINE)
    with account_col:
        st.caption(f"Signed in as  \n**{user_key}**")
        if st.button("Log out", width="stretch"):
            # Only the session is dropped. st.cache_data is shared server-wide
            # and keyed by user_key, so clearing it here would evict other
            # people's entries too.
            st.session_state.pop("client", None)
            st.session_state.session_ended = "logout"
            st.rerun()
    st.divider()


def current_period(periods: list[dict], today: dt.date) -> dict | None:
    """Return the billing period containing today, when one is known."""
    index = bp.find_period_index(periods, today)
    return periods[index] if index is not None else None


def style_chart(fig, *, y_title: str, show_legend: bool = False):
    """Apply a quiet, consistent dashboard treatment to Plotly charts."""
    fig.update_layout(
        margin=dict(l=8, r=8, t=16, b=8),
        legend_title_text="",
        showlegend=show_legend,
        hovermode="x unified",
    )
    fig.update_xaxes(title=None, showgrid=False)
    fig.update_yaxes(title=y_title, gridcolor="rgba(122, 138, 147, 0.16)")
    return fig


@st.cache_data(ttl=3600, show_spinner=False)
def load_service_points(_client: EacClient, user_key: str):
    return _client.get_service_points()


@st.cache_data(ttl=3600, show_spinner=False)
def load_channels(_client: EacClient, user_key: str, sp_id: str):
    return _client.discover_channels(sp_id)


@st.cache_data(ttl=1800, show_spinner="Fetching readings...")
def load_readings(
    _client: EacClient, user_key: str, sp_id: str, mc_id: str,
    start_date: dt.date, end_date: dt.date,
):
    return _client.get_readings(sp_id, mc_id, start_date, end_date)


@st.cache_data(ttl=1800, show_spinner="Fetching load profile...")
def load_intraday(_client: EacClient, user_key: str, sp_id: str, mc_id: str, day: dt.date):
    return _client.get_readings(sp_id, mc_id, day, day, truncate_to_date=False)


client = get_client()
user_key = client.user_id
header(user_key)
service_points = load_service_points(client, user_key)

if not service_points:
    st.warning("No service points found on this account.")
    st.stop()

today = dt.date.today()
# Read-only for now. `ensure_coverage` projects future cycles in memory but
# nothing is written back: billing_periods.json is one global file with no user
# scoping (billing_periods.py:12), so persisting one visitor's view would rewrite
# everyone else's chart. It also lives next to the source, which fails on hosts
# that mount the code read-only. Periods become per-user once they move into
# SQLite alongside the readings.
periods = bp.ensure_coverage(bp.load_periods(), today)
active_period = current_period(periods, today)

sp_labels = {sp.get("spId", sp.get("id", str(sp))): sp for sp in service_points}
with st.sidebar:
    st.subheader("Your view")
    sp_id = st.selectbox(
        "Property",
        options=list(sp_labels.keys()),
        format_func=lambda key: f"{sp_labels[key].get('address') or 'Service point'} · {key}"[:60],
    )

channels = load_channels(client, user_key, sp_id)
if not channels:
    st.error("We couldn't find any meter channels for this property.")
    with st.expander("Technical details"):
        st.json(client.get_service_point_detail(sp_id))
    st.stop()

with st.sidebar.expander("Advanced meter options"):
    show_all_channels = st.toggle(
        "Show technical channels",
        value=False,
        help="Includes export, apparent-power, reactive-power and inactive meter channels.",
    )

if show_all_channels:
    def channel_rank(ch):
        if ch["type"] in CHANNEL_ORDER and ch["active"]:
            return (0, CHANNEL_ORDER.index(ch["type"]))
        return (1 if ch["active"] else 2, 0)

    channel_options = [(ch["label"], ch["mc_id"]) for ch in sorted(channels, key=channel_rank)]
else:
    active_by_type = {ch["type"]: ch for ch in channels if ch["active"]}
    channel_options = [
        (CHANNEL_NAMES[channel_type], active_by_type[channel_type]["mc_id"])
        for channel_type in CHANNEL_ORDER
        if channel_type in active_by_type
    ]
    if not channel_options:
        channel_options = [(ch["label"], ch["mc_id"]) for ch in channels]

mc_key_map = dict(channel_options)
with st.sidebar:
    channel_label = st.selectbox("Energy type", options=list(mc_key_map.keys()))
    range_name = st.selectbox(
        "Period",
        ["Current bill", "Last 30 days", "Last 3 months", "Last 12 months", "Custom"],
    )

    if range_name == "Current bill" and active_period:
        start_date = active_period["start"]
        end_date = today
    elif range_name == "Last 30 days":
        start_date, end_date = today - dt.timedelta(days=29), today
    elif range_name == "Last 12 months":
        start_date, end_date = today - dt.timedelta(days=364), today
    elif range_name == "Custom":
        date_cols = st.columns(2)
        start_date = date_cols[0].date_input("From", value=today - dt.timedelta(days=90))
        end_date = date_cols[1].date_input("To", value=today)
    else:
        start_date, end_date = today - dt.timedelta(days=89), today

    if start_date > end_date:
        st.error("The start date must be before the end date.")
        st.stop()

    if st.button("Refresh readings", type="primary", width="stretch"):
        st.cache_data.clear()
        st.rerun()

mc_id = mc_key_map[channel_label]
readings = load_readings(client, user_key, sp_id, mc_id, start_date, end_date)

if readings.empty:
    st.info("No readings are available for this period yet. Try an earlier or wider range.")
    st.stop()

daily = readings.copy()
latest_date = pd.Timestamp(daily["date"].max()).date()
lag_days = max((today - latest_date).days, 0)

st.subheader(channel_label)
range_col, freshness_col = st.columns([3, 2])
range_col.caption(f"{start_date:%d %b %Y} – {end_date:%d %b %Y}")
freshness_col.caption(
    f"Latest EAC data: {latest_date:%d %b %Y}"
    + (f" · {lag_days} days behind" if lag_days else " · up to date")
)

last_year_readings = load_readings(
    client,
    user_key,
    sp_id,
    mc_id,
    start_date - dt.timedelta(days=365),
    end_date - dt.timedelta(days=365),
)

total = daily["consumption"].sum()
average = daily["consumption"].mean()
latest_cumulative = readings["reading"].dropna()
latest_value = f"{latest_cumulative.iloc[-1]:,.0f} kWh" if not latest_cumulative.empty else "Not reported"

metric_cols = st.columns(4)
metric_cols[0].metric("Energy used", f"{total:,.0f} kWh", border=True)
metric_cols[1].metric("Daily average", f"{average:.1f} kWh", border=True)
metric_cols[2].metric("Latest meter reading", latest_value, border=True)
if last_year_readings.empty:
    metric_cols[3].metric("Compared with last year", "No data", border=True)
else:
    ly_total = last_year_readings["consumption"].sum()
    delta = (total - ly_total) / ly_total * 100 if ly_total else None
    metric_cols[3].metric(
        "Compared with last year",
        f"{ly_total:,.0f} kWh then",
        delta=f"{delta:+.1f}%" if delta is not None else None,
        delta_color="inverse",
        border=True,
    )

overview_tab, intraday_tab, data_tab = st.tabs(["Overview", "Half-hour usage", "Data & settings"])

with overview_tab:
    st.markdown("#### Daily consumption")
    fig_daily = px.bar(daily, x="date", y="consumption", color_discrete_sequence=[PRIMARY])
    if not last_year_readings.empty:
        shifted = last_year_readings.copy()
        shifted["date"] = pd.to_datetime(shifted["date"]) + pd.DateOffset(years=1)
        fig_daily.add_scatter(
            x=shifted["date"],
            y=shifted["consumption"],
            mode="lines",
            name="Last year",
            line=dict(color=ACCENT, dash="dot", width=2),
        )
    # Label every boundary, because an unexplained vertical line reads as a
    # glitch. The annotation sits inside the plot rather than above it - the
    # chart margins are tight enough now that anything above gets clipped.
    boundaries = [p for p in periods if start_date <= p["start"] <= end_date]
    for period in boundaries:
        fig_daily.add_vline(
            x=pd.Timestamp(period["start"]),
            line_dash="dot",
            line_color=MUTED,
            opacity=0.5,
            annotation_text=f"{period['start']:%d %b}",
            annotation_position="top left",
            annotation_font_size=10,
            annotation_font_color=MUTED,
        )
    style_chart(fig_daily, y_title="kWh", show_legend=not last_year_readings.empty)
    st.plotly_chart(fig_daily, width="stretch")
    if boundaries:
        estimated = sum(1 for p in boundaries if p["source"] == "estimated")
        note = "Dotted lines mark the start of a billing period."
        if estimated:
            note += " Boundaries after your last confirmed bill are estimated."
        st.caption(note)

    daily["rolling_7d"] = daily["consumption"].rolling(7, min_periods=1).mean()
    monthly = daily.assign(month_period=pd.to_datetime(daily["date"]).dt.to_period("M"))
    monthly = monthly.groupby("month_period", as_index=False)["consumption"].sum()
    monthly["month"] = monthly["month_period"].dt.strftime("%b %Y")

    trend_col, monthly_col = st.columns(2)
    with trend_col:
        st.markdown("#### Your recent trend")
        fig_rolling = px.line(
            daily, x="date", y="rolling_7d", color_discrete_sequence=[PRIMARY], markers=False
        )
        fig_rolling.update_traces(line_width=3)
        style_chart(fig_rolling, y_title="7-day average (kWh/day)")
        st.plotly_chart(fig_rolling, width="stretch")
    with monthly_col:
        st.markdown("#### Monthly totals")
        fig_monthly = px.bar(
            monthly, x="month", y="consumption", color_discrete_sequence=[ACCENT]
        )
        fig_monthly.update_xaxes(type="category")
        style_chart(fig_monthly, y_title="kWh")
        st.plotly_chart(fig_monthly, width="stretch")

with intraday_tab:
    st.markdown("#### See when your home uses energy")
    st.caption("Half-hour readings usually arrive sooner than daily meter totals.")
    lp_channel = next(
        (ch for ch in channels if ch["type"] == "KWH-30MIN-LP-IMP" and ch["active"]),
        None,
    )
    if lp_channel is None:
        st.info("No half-hour load profile is available for this property.")
    else:
        intraday_day = st.date_input(
            "Choose a day",
            value=today - dt.timedelta(days=3),
            max_value=today,
            key="intraday_day",
        )
        intraday = load_intraday(client, user_key, sp_id, lp_channel["mc_id"], intraday_day)
        if intraday.empty:
            st.info("No half-hour data is available for this day yet. Try an earlier date.")
        else:
            peak = intraday.loc[intraday["consumption"].idxmax()]
            baseload = intraday["consumption"].min()
            intraday_metrics = st.columns(3)
            intraday_metrics[0].metric("Day total", f"{intraday['consumption'].sum():.2f} kWh", border=True)
            intraday_metrics[1].metric(
                "Busiest half-hour",
                f"{peak['consumption']:.2f} kWh",
                f"at {peak['date']:%H:%M}",
                delta_color="off",
                border=True,
            )
            intraday_metrics[2].metric(
                "Quietest half-hour",
                f"{baseload:.2f} kWh",
                f"≈ {baseload * 2 * 1000:.0f} W baseload",
                delta_color="off",
                border=True,
            )
            if len(intraday) < 48:
                st.warning(
                    f"This is a partial day: {len(intraday)} of 48 half-hour readings are available."
                )
            fig_intraday = px.bar(
                intraday, x="date", y="consumption", color_discrete_sequence=[ACCENT]
            )
            fig_intraday.update_xaxes(tickformat="%H:%M", title="Time")
            style_chart(fig_intraday, y_title="kWh per half-hour")
            st.plotly_chart(fig_intraday, width="stretch")

with data_tab:
    st.markdown("#### Download your readings")
    export_data = daily[["date", "reading", "consumption"]].copy()
    st.download_button(
        "Download CSV",
        export_data.to_csv(index=False).encode("utf-8"),
        file_name=f"eac-readings-{start_date}-{end_date}.csv",
        mime="text/csv",
    )
    st.dataframe(export_data, width="stretch", hide_index=True)

    with st.expander("Billing periods"):
        st.caption(
            "Confirmed dates come from your EAC bills; estimated ones continue the "
            "usual cycle. Editing these is coming once each account has its own copy."
        )
        st.dataframe(
            pd.DataFrame(periods),
            column_config={
                "start": st.column_config.DateColumn("From (inclusive)"),
                "end": st.column_config.DateColumn("To (exclusive)"),
                "source": st.column_config.TextColumn("Source"),
            },
            width="stretch",
            hide_index=True,
        )
