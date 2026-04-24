# pages/2_Discount_Planner.py
# Discount Planner — Steam Commercial Suite
# Build and optimize your year-1 Steam discount calendar.
# Steam-only. No Xbox, no PlayStation.

from __future__ import annotations
import math
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import altair as alt
import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from revenue_optimizer import (
    load_steam_calendar,
    get_upcoming_sales,
    build_discount_calendar,
    get_asp_factor,
    STEAM_SHARE,
    VAT_FACTOR,
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Discount Planner", page_icon="🗓️", layout="wide")
st.title("🗓️ Discount Planner")
st.caption(
    "Build your year-1 Steam discount calendar. Participate in platform sales, add custom promotions, "
    "align with your content roadmap, and project blended ASP and net revenue impact."
)

# ── Load calendar ──────────────────────────────────────────────────────────────
@st.cache_data
def _load_calendar() -> dict:
    return load_steam_calendar()

CALENDAR = _load_calendar()
COOLDOWN_DAYS      = CALENDAR.get("discount_rules", {}).get("cooldown_days", 30)
NEW_RELEASE_WINDOW = CALENDAR.get("discount_rules", {}).get("new_release_window_days", 30)

# ── Formatters ─────────────────────────────────────────────────────────────────
def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v / 1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}K"
    return f"${v:.0f}"

def fmt_units(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)

# ── Weekend anchoring ──────────────────────────────────────────────────────────
def snap_to_thursday(d: date) -> date:
    """
    Snap a date forward to the nearest Thursday (weekday=3).
    Mon→Thu same week, Fri/Sat/Sun→Thu next week.
    This anchors sale windows to capture the Fri–Sat–Sun peak buying days.
    """
    wd = d.weekday()     # 0=Mon … 6=Sun
    if wd <= 3:          # Mon, Tue, Wed, Thu → snap to Thursday of current week
        return d + timedelta(days=3 - wd)
    else:                # Fri, Sat, Sun → snap to Thursday of next week
        return d + timedelta(days=10 - wd)

# ── Cooldown conflict checker ──────────────────────────────────────────────────
def find_cooldown_conflicts(events: list[dict]) -> list[str]:
    """Return human-readable strings for every pair of events within COOLDOWN_DAYS of each other."""
    def as_date(v) -> date:
        return v if isinstance(v, date) else datetime.strptime(v, "%Y-%m-%d").date()

    sorted_evs = sorted(events, key=lambda e: as_date(e["start_date"]))
    conflicts = []
    for i in range(1, len(sorted_evs)):
        prev = sorted_evs[i - 1]
        curr = sorted_evs[i]
        gap  = (as_date(curr["start_date"]) - as_date(prev["end_date"])).days
        if gap < COOLDOWN_DAYS:
            conflicts.append(
                f"**{curr['name']}** starts {gap}d after **{prev['name']}** ends "
                f"(need ≥{COOLDOWN_DAYS}d cooldown)"
            )
    return conflicts

# ── Gap optimizer ──────────────────────────────────────────────────────────────
def find_gap_suggestions(
    all_events: list[dict],
    launch_date: date,
    months: int = 12,
    min_gap_days: int = 45,
    sale_length: int = 11,
) -> list[dict]:
    """
    Find calendar gaps between selected events and suggest custom sales.
    Custom sale start is snapped to Thursday. Cooldown enforced on both sides.
    """
    def as_date(v) -> date:
        return v if isinstance(v, date) else datetime.strptime(v, "%Y-%m-%d").date()

    try:
        from dateutil.relativedelta import relativedelta
        year_end = launch_date + relativedelta(months=months)
    except ImportError:
        year_end = launch_date + timedelta(days=365)

    first_ok = launch_date + timedelta(days=NEW_RELEASE_WINDOW)
    sorted_evs = sorted(all_events, key=lambda e: as_date(e["start_date"]))

    segments: list[tuple[date, date]] = []
    prev_end = first_ok
    for ev in sorted_evs:
        ev_start = as_date(ev["start_date"])
        ev_end   = as_date(ev["end_date"])
        segments.append((prev_end, ev_start))
        prev_end = ev_end
    segments.append((prev_end, year_end))

    suggestions = []
    for gap_start, gap_end in segments:
        gap_days = (gap_end - gap_start).days
        if gap_days < min_gap_days:
            continue
        # Place sale near midpoint, snapped to Thursday
        mid       = gap_start + timedelta(days=gap_days // 2)
        sale_start = snap_to_thursday(mid - timedelta(days=sale_length // 2))
        sale_end   = sale_start + timedelta(days=sale_length - 1)

        # Verify cooldown on both sides
        left_ok  = (sale_start - gap_start).days >= COOLDOWN_DAYS
        right_ok = (gap_end - sale_end).days     >= COOLDOWN_DAYS
        in_range = sale_start >= first_ok

        if left_ok and right_ok and in_range:
            suggestions.append({
                "name":         f"Custom Sale ({sale_start.strftime('%b %d')})",
                "start_date":   sale_start,
                "end_date":     sale_end,
                "discount_pct": 20.0,
                "type":         "custom",
                "source":       "auto",
            })

    return suggestions

# ── Session-state initialiser ──────────────────────────────────────────────────
def _ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]

_ss("dp_custom_sales",        [])
_ss("dp_roadmap",             [])
_ss("dp_platform_sel",        {})   # {ev_key: {"participate": bool, "discount_pct": float, "ev": dict}}

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🎮 Game Setup")

    game_name = st.text_input(
        "Game Name",
        value=st.session_state.get("ro_game_name", "My Game"),
        key="dp_game_name",
    )

    launch_date: date = st.date_input(
        "Launch Date",
        value=st.session_state.get("dp_launch_date_val", date(2026, 9, 1)),
        min_value=date(2025, 1, 1),
        max_value=date(2028, 12, 31),
        key="dp_launch_date",
    )
    st.session_state["dp_launch_date_val"] = launch_date

    launch_price: float = st.number_input(
        "Launch Price (USD)",
        min_value=0.99, max_value=79.99,
        value=float(st.session_state.get("ro_base_price", 19.99)),
        step=5.0, format="%.2f",
        key="dp_launch_price",
    )

    quality_tier: str = st.selectbox(
        "Quality Tier",
        ["indie", "aa", "aaa"],
        index=["indie", "aa", "aaa"].index(
            st.session_state.get("ro_tier", "indie")
        ),
        format_func=lambda x: x.upper() if x == "aa" else x.title(),
        key="dp_quality_tier",
    )

    sentiment: str = st.selectbox(
        "Expected Reception",
        ["very_positive", "mostly_positive", "mixed"],
        index=["very_positive", "mostly_positive", "mixed"].index(
            st.session_state.get("ro_sentiment", "mostly_positive")
        ),
        format_func=lambda x: x.replace("_", " ").title(),
        key="dp_sentiment",
    )

    units_p50: int = st.number_input(
        "Expected Year-1 Units (P50)",
        min_value=100, max_value=10_000_000,
        value=int(st.session_state.get("ro_units_p50", 5_000)),
        step=500,
        help="Used for revenue impact projections. Pull from Revenue Optimizer.",
        key="dp_units_p50",
    )

    st.divider()
    st.caption(
        "**Tip:** Values auto-populated from Revenue Optimizer when available. "
        "Navigate there first to populate your game details."
    )

# Derived: earliest discount date
first_discount_date = launch_date + timedelta(days=NEW_RELEASE_WINDOW)

# ── Fetch upcoming platform events ─────────────────────────────────────────────
upcoming = get_upcoming_sales(launch_date, CALENDAR, months_ahead=13)

tentpoles       = [e for e in upcoming if e["type"] == "tentpole"]
all_themed      = [e for e in upcoming if e["type"] == "themed"]
user_genres     = st.session_state.get("ro_genres", [])

def is_genre_eligible(ev: dict) -> bool:
    eligible = ev.get("eligible_genres", [])
    if not eligible or not user_genres:
        return True
    return any(
        g.lower() in elig.lower() or elig.lower() in g.lower()
        for g in user_genres
        for elig in eligible
    )

eligible_themed   = [e for e in all_themed if is_genre_eligible(e)]
ineligible_themed = [e for e in all_themed if not is_genre_eligible(e)]

# ── SECTION 1 — Platform Sales ─────────────────────────────────────────────────
st.subheader("1️⃣  Platform Sales")

no_disc_note = (
    f"The 30-day new release window closes **{first_discount_date.strftime('%b %d, %Y')}**. "
    "Steam prevents discounting before this date."
)
st.caption(no_disc_note)

if not upcoming:
    st.info(
        "No Steam sales found in the 12 months after your launch date. "
        "Adjust your launch date in the sidebar or update the calendar config."
    )

# Helper: render one event row and persist to dp_platform_sel
def _render_event_row(ev: dict, default_participate: bool, default_discount: int) -> None:
    ev_key = f"{ev['type']}_{ev['name']}_{ev['start_date']}"
    prev   = st.session_state.dp_platform_sel.get(ev_key, {})

    dur         = (ev["end_date"] - ev["start_date"]).days
    disc_range  = ev.get("discount_range", [15, 50])
    disc_min    = disc_range[0]
    disc_max    = min(disc_range[1], 75)
    saved_disc  = int(prev.get("discount_pct", default_discount))
    saved_part  = prev.get("participate", default_participate)

    c_chk, c_name, c_dates, c_slide, c_price = st.columns([0.06, 0.28, 0.22, 0.28, 0.16])

    participate = c_chk.checkbox(
        "in", value=saved_part, key=f"chk_{ev_key}", label_visibility="collapsed"
    )

    c_name.markdown(f"**{ev['name']}**")
    if ev.get("eligible_genres"):
        c_name.caption(", ".join(ev["eligible_genres"][:4]) + ("…" if len(ev["eligible_genres"]) > 4 else ""))

    c_dates.markdown(
        f"{ev['start_date'].strftime('%b %d')} – {ev['end_date'].strftime('%b %d, %Y')}"
    )
    c_dates.caption(f"{dur}d")

    if participate:
        discount = c_slide.slider(
            "disc", disc_min, disc_max, saved_disc, 5,
            key=f"disc_{ev_key}", format="%d%%", label_visibility="collapsed",
        )
    else:
        discount = saved_disc
        c_slide.caption(f"~~{disc_min}–{disc_max}%~~  (not participating)")

    sale_price = launch_price * (1 - discount / 100)
    c_price.metric("Sale Price", f"${sale_price:.2f}", delta=f"−{discount}%", delta_color="inverse")

    st.session_state.dp_platform_sel[ev_key] = {
        "ev":            ev,
        "participate":   participate,
        "discount_pct":  discount,
    }

# Tentpole seasonal sales
if tentpoles:
    st.markdown("**🏔️ Seasonal Sales** — open to all games")
    for ev in tentpoles:
        _render_event_row(ev, default_participate=True, default_discount=33)

# Eligible themed fests
if eligible_themed:
    genre_label = " / ".join(user_genres) if user_genres else "your genres"
    st.markdown(
        f"**🎪 Genre-Eligible Themed Fests** — {len(eligible_themed)} matching {genre_label}"
    )
    for ev in eligible_themed:
        _render_event_row(ev, default_participate=False, default_discount=20)

# Ineligible fests (collapsed)
if ineligible_themed:
    with st.expander(f"⬜ {len(ineligible_themed)} themed fests not matching your genre"):
        for ev in ineligible_themed:
            st.caption(
                f"**{ev['name']}** — {ev['start_date'].strftime('%b %d')}–{ev['end_date'].strftime('%b %d')}: "
                + (ev.get("notes") or "No notes")
            )

# Collect selected platform events
selected_platform: list[dict] = [
    {
        "name":         s["ev"]["name"],
        "start_date":   s["ev"]["start_date"],
        "end_date":     s["ev"]["end_date"],
        "discount_pct": float(s["discount_pct"]),
        "type":         s["ev"]["type"],
        "source":       "platform",
    }
    for s in st.session_state.dp_platform_sel.values()
    if s["participate"]
]

# ── SECTION 2 — Custom Sales ───────────────────────────────────────────────────
st.divider()
st.subheader("2️⃣  Custom Sales")
st.caption(
    "Add your own promotional windows. Dates are auto-anchored to **Thursday** starts "
    "to capture the Friday–Saturday–Sunday peak buying window."
)

with st.expander("➕ Add Custom Sale", expanded=(len(st.session_state.dp_custom_sales) == 0)):
    a1, a2, a3, a4 = st.columns([2.2, 1.5, 1.5, 1])

    cs_name = a1.text_input("Sale Name", value="Promo Sale", key="cs_name_in")

    cs_raw_start: date = a2.date_input(
        "Start Date",
        value=first_discount_date + timedelta(days=14),
        min_value=first_discount_date,
        max_value=launch_date + timedelta(days=400),
        key="cs_start_in",
        help="Will snap to the nearest Thursday on or after this date.",
    )
    cs_snapped = snap_to_thursday(cs_raw_start)
    cs_end     = cs_snapped + timedelta(days=10)   # 11-day window: Thu → Sun+1week

    if cs_snapped != cs_raw_start:
        a2.caption(f"📌 Snapped → **{cs_snapped.strftime('%a %b %d')}**")

    a3.markdown(f"**End:** {cs_end.strftime('%a %b %d, %Y')}")
    a3.caption("11-day window (Thu–Sun + following week)")

    cs_disc = a4.number_input("Discount %", min_value=10, max_value=75, value=20, step=5, key="cs_disc_in")

    if st.button("Add Sale ➕", type="primary", key="cs_add_btn"):
        # Duplicate check
        exists = any(
            cs["name"] == cs_name and cs["start_date"] == cs_snapped
            for cs in st.session_state.dp_custom_sales
        )
        if exists:
            st.warning("A sale with this name and start date already exists.")
        else:
            st.session_state.dp_custom_sales.append({
                "name":         cs_name,
                "start_date":   cs_snapped,
                "end_date":     cs_end,
                "discount_pct": float(cs_disc),
                "type":         "custom",
                "source":       "custom",
            })
            st.rerun()

# List existing custom sales
if st.session_state.dp_custom_sales:
    hdr = st.columns([2.5, 2, 1.2, 0.6, 0.6, 0.5])
    hdr[0].caption("Name"); hdr[1].caption("Dates"); hdr[2].caption("Discount → Price")
    hdr[3].caption("Days"); hdr[4].caption("Source"); hdr[5].caption("")

    for i, cs in enumerate(list(st.session_state.dp_custom_sales)):
        r = st.columns([2.5, 2, 1.2, 0.6, 0.6, 0.5])
        r[0].markdown(f"**{cs['name']}**")
        start = cs["start_date"]
        end   = cs["end_date"]
        r[1].markdown(f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}")
        r[2].markdown(f"**{cs['discount_pct']:.0f}% off** → ${launch_price * (1 - cs['discount_pct']/100):.2f}")
        r[3].markdown(f"{(end - start).days + 1}d")
        src_label = "🤖 auto" if cs.get("source") == "auto" else "✏️ manual"
        r[4].markdown(src_label)
        if r[5].button("🗑", key=f"del_cs_{i}"):
            st.session_state.dp_custom_sales.pop(i)
            st.rerun()

# ── SECTION 3 — Content Roadmap ────────────────────────────────────────────────
st.divider()
st.subheader("3️⃣  Content Roadmap")
st.caption(
    "Mark major updates, DLC, and events. The optimizer will align discount windows with "
    "content releases to amplify the Steam algorithmic traffic boost."
)

with st.expander("➕ Add Milestone"):
    rm1, rm2, rm3 = st.columns([2.5, 1.8, 1])
    rm_name = rm1.text_input("Milestone", value="Major Update v1.1", key="rm_name_in")
    rm_date: date = rm2.date_input(
        "Expected Date",
        value=launch_date + timedelta(days=90),
        min_value=launch_date,
        max_value=launch_date + timedelta(days=400),
        key="rm_date_in",
    )
    rm_type = rm3.selectbox("Type", ["Update", "DLC", "Patch", "Event"], key="rm_type_in")

    if st.button("Add Milestone ➕", key="rm_add_btn"):
        st.session_state.dp_roadmap.append({
            "name": rm_name, "date": rm_date, "type": rm_type
        })
        st.rerun()

if st.session_state.dp_roadmap:
    for i, rm in enumerate(list(st.session_state.dp_roadmap)):
        rc = st.columns([3, 2, 1.2, 0.5])
        rc[0].markdown(f"**{rm['name']}**")
        rc[1].markdown(rm["date"].strftime("%b %d, %Y"))
        rc[2].markdown(f"`{rm['type']}`")
        if rc[3].button("🗑", key=f"del_rm_{i}"):
            st.session_state.dp_roadmap.pop(i)
            st.rerun()

# ── SECTION 4 — Auto-Optimizer ────────────────────────────────────────────────
st.divider()
st.subheader("4️⃣  Auto-Optimize")
st.caption(
    "Scans your calendar for gaps ≥45 days and suggests custom sales. "
    "All suggestions respect the 30-day cooldown rule and snap to Thursday starts."
)

opt_col1, opt_col2 = st.columns([2, 1])
with opt_col1:
    aggressiveness = st.select_slider(
        "Aggressiveness",
        options=["Conservative", "Moderate", "Aggressive"],
        value=st.session_state.get("dp_aggressiveness", "Moderate"),
        key="dp_aggressiveness",
        help=(
            "Conservative: fills gaps ≥55 days, 15% discount. "
            "Moderate: ≥45 days, 25% discount. "
            "Aggressive: ≥35 days, 33% discount."
        ),
    )

with opt_col2:
    st.write("")
    run_optimizer = st.button("🔧 Auto-Fill Gaps", type="secondary", use_container_width=True)

if run_optimizer:
    all_cur = selected_platform + st.session_state.dp_custom_sales
    params = {
        "Conservative": (55, 15),
        "Moderate":     (45, 25),
        "Aggressive":   (35, 33),
    }
    min_gap, disc_default = params[aggressiveness]

    suggestions = find_gap_suggestions(all_cur, launch_date, months=12, min_gap_days=min_gap)

    added = 0
    for sug in suggestions:
        already = any(
            abs((sug["start_date"] - cs["start_date"]).days) < 20
            for cs in st.session_state.dp_custom_sales
        )
        if not already:
            sug["discount_pct"] = float(disc_default)
            st.session_state.dp_custom_sales.append(sug)
            added += 1

    if added:
        st.success(f"Added **{added}** suggested sale(s). Adjust discounts in the Custom Sales section above.")
    else:
        st.info("No eligible gaps found — your calendar is well-optimized already.")
    st.rerun()

# ── Build master event list ─────────────────────────────────────────────────────
def _as_date(v) -> date:
    return v if isinstance(v, date) else datetime.strptime(v, "%Y-%m-%d").date()

all_events: list[dict] = sorted(
    selected_platform + st.session_state.dp_custom_sales,
    key=lambda e: _as_date(e["start_date"])
)

# ── Cooldown warnings ──────────────────────────────────────────────────────────
conflicts = find_cooldown_conflicts(all_events)
if conflicts:
    st.warning("⚠️ **Steam Cooldown Violations** — fix before submitting discounts via Steamworks:")
    for c in conflicts:
        st.markdown(f"  • {c}")

# ── SECTION 5 — Calendar View (Gantt) ─────────────────────────────────────────
st.divider()
st.subheader("5️⃣  Calendar View")

year_end = launch_date + timedelta(days=365)

chart_rows: list[dict] = []

# No-discount zone
chart_rows.append({
    "event":    "🔒 New Release Window",
    "start":    pd.Timestamp(launch_date),
    "end":      pd.Timestamp(first_discount_date),
    "category": "No-Discount Zone",
    "label":    "No discounts (30d)",
    "order":    0,
})

# Roadmap milestones (1-day markers)
for rm in st.session_state.dp_roadmap:
    chart_rows.append({
        "event":    f"📌 {rm['name']}",
        "start":    pd.Timestamp(rm["date"]),
        "end":      pd.Timestamp(rm["date"] + timedelta(days=1)),
        "category": f"Roadmap: {rm['type']}",
        "label":    rm["type"],
        "order":    1,
    })

# Discount events
for ev in all_events:
    s = _as_date(ev["start_date"])
    e = _as_date(ev["end_date"])
    cat_map = {
        "tentpole": "Tentpole Sale",
        "themed":   "Themed Fest",
        "custom":   "Custom Sale",
    }
    cat = cat_map.get(ev.get("type", "custom"), "Custom Sale")
    chart_rows.append({
        "event":    ev["name"],
        "start":    pd.Timestamp(s),
        "end":      pd.Timestamp(e + timedelta(days=1)),   # inclusive end
        "category": cat,
        "label":    f"{ev['discount_pct']:.0f}% off → ${launch_price * (1 - ev['discount_pct']/100):.2f}",
        "order":    2,
    })

if len(chart_rows) > 1:
    df_chart = pd.DataFrame(chart_rows).sort_values(["order", "start"])

    color_domain = [
        "Tentpole Sale", "Themed Fest", "Custom Sale",
        "No-Discount Zone",
        "Roadmap: Update", "Roadmap: DLC", "Roadmap: Patch", "Roadmap: Event",
    ]
    color_range = [
        "#1565C0", "#2E7D32", "#E65100",
        "#CFD8DC",
        "#7B1FA2", "#AD1457", "#00796B", "#F57F17",
    ]

    # Ensure all categories in df are in the domain
    for cat in df_chart["category"].unique():
        if cat not in color_domain:
            color_domain.append(cat)
            color_range.append("#546E7A")

    gantt = alt.Chart(df_chart).mark_bar(
        cornerRadiusEnd=4, cornerRadiusStart=4, height=18
    ).encode(
        x=alt.X("start:T", title="Date", axis=alt.Axis(format="%b %Y", tickCount="month")),
        x2="end:T",
        y=alt.Y(
            "event:N", title=None, sort=None,
            axis=alt.Axis(labelLimit=220, labelFontSize=12),
        ),
        color=alt.Color(
            "category:N",
            scale=alt.Scale(domain=color_domain, range=color_range),
            legend=alt.Legend(title="Event Type", orient="bottom", columns=4),
        ),
        tooltip=[
            alt.Tooltip("event:N",    title="Event"),
            alt.Tooltip("start:T",    title="Start",    format="%b %d, %Y"),
            alt.Tooltip("end:T",      title="End",      format="%b %d, %Y"),
            alt.Tooltip("label:N",    title="Details"),
            alt.Tooltip("category:N", title="Type"),
        ],
    ).properties(
        height=max(260, len(chart_rows) * 30),
        title=alt.TitleParams(
            text=f"{st.session_state.get('dp_game_name', 'My Game')} — Year-1 Discount Calendar",
            fontSize=14, fontWeight="bold",
        ),
    )

    # Year-end dashed rule
    rule_df = pd.DataFrame([{"date": pd.Timestamp(year_end)}])
    year_rule = alt.Chart(rule_df).mark_rule(
        strokeDash=[5, 4], color="#90A4AE", strokeWidth=1.5
    ).encode(
        x="date:T",
        tooltip=[alt.Tooltip("date:T", title="Year 1 End", format="%b %d, %Y")],
    )

    # Launch marker
    launch_df = pd.DataFrame([{"date": pd.Timestamp(launch_date)}])
    launch_rule = alt.Chart(launch_df).mark_rule(
        color="#880E4F", strokeWidth=2
    ).encode(
        x="date:T",
        tooltip=[alt.Tooltip("date:T", title="Launch Date", format="%b %d, %Y")],
    )

    st.altair_chart((gantt + year_rule + launch_rule), use_container_width=True)
    st.caption(
        "Pink line = launch date · Dashed line = year-1 end. "
        "Hover over bars for discount details."
    )
else:
    st.info("No sales selected yet. Participate in platform sales or add custom sales above to see the calendar.")

# ── SECTION 6 — Revenue Impact ────────────────────────────────────────────────
st.divider()
st.subheader("6️⃣  Revenue Impact")

asp_base      = get_asp_factor(quality_tier, sentiment)
net_per_unit_fp = launch_price * asp_base * STEAM_SHARE * VAT_FACTOR
baseline_net    = units_p50 * net_per_unit_fp

ri_col1, ri_col2, ri_col3 = st.columns(3)

with ri_col1:
    st.markdown("**📌 No Discounts (Baseline)**")
    st.metric("Blended ASP",         f"${launch_price * asp_base:.2f}")
    st.metric("Year-1 Net (P50)",    fmt_usd(baseline_net))
    st.caption(f"ASP factor: {asp_base:.1%} ({quality_tier}, {sentiment.replace('_', ' ')})")

if all_events:
    cal_result = build_discount_calendar(
        launch_price=launch_price,
        quality_tier=quality_tier,
        sentiment=sentiment,
        selected_events=all_events,
    )

    # Revenue with discount calendar — use blended_asp (time-weighted price) × quality/sentiment ASP
    calendar_net = units_p50 * cal_result.blended_asp * asp_base * STEAM_SHARE * VAT_FACTOR
    delta_net    = calendar_net - baseline_net
    delta_pct    = (delta_net / baseline_net) * 100 if baseline_net else 0

    with ri_col2:
        st.markdown("**📅 Your Discount Calendar**")
        st.metric(
            "Blended ASP",
            f"${cal_result.blended_asp * asp_base:.2f}",
            delta=f"−{(1 - cal_result.blended_asp / launch_price)*100:.1f}% vs full price",
            delta_color="inverse",
        )
        st.metric(
            "Year-1 Net (P50)",
            fmt_usd(calendar_net),
            delta=f"{delta_pct:+.1f}% vs no discounts",
            delta_color="normal" if delta_pct >= 0 else "inverse",
        )
        st.caption(
            f"Blended list price: ${cal_result.blended_asp:.2f} × "
            f"ASP {asp_base:.1%} × 70% Steam × 88% VAT"
        )

    with ri_col3:
        st.markdown("**📊 Calendar Stats**")
        disc_share = cal_result.discount_days / 365
        st.metric("Events Selected",   len(all_events))
        st.metric("Discount Days",     f"{cal_result.discount_days} / 365 ({disc_share:.0%})")
        st.metric("Blended ASP Factor", f"{cal_result.blended_asp_factor:.3f}")

    # Event breakdown
    st.markdown("**Event Breakdown**")
    breakdown = []
    for ev in cal_result.events:
        sale_p = launch_price * ev.sale_price_factor
        dur    = ev.duration_days
        breakdown.append({
            "Event":         ev.name,
            "Dates":         f"{ev.start_date.strftime('%b %d')} – {ev.end_date.strftime('%b %d, %Y')}",
            "Days":          dur,
            "Discount":      f"{ev.discount_pct:.0f}%",
            "Sale Price":    f"${sale_p:.2f}",
            "Revenue Share": f"{dur / 365:.1%} of year",
        })
    st.dataframe(pd.DataFrame(breakdown), use_container_width=True, hide_index=True)

    # Strategy health indicator
    st.markdown("---")
    if disc_share > 0.25:
        st.warning(
            f"⚠️ **High discount exposure** — your game is on sale **{disc_share:.0%}** of the year. "
            "Heavy discounting can erode perceived value and buyer willingness to pay at full price. "
            "Consider trimming to <20% of the year."
        )
    elif disc_share < 0.08:
        st.info(
            f"📊 **Low visibility cadence** — your game is on sale **{disc_share:.0%}** of the year. "
            "Each sale event resets your visibility in Steam's algorithm. "
            "Consider 2–3 more promotional windows."
        )
    else:
        st.success(
            f"✅ **Healthy discount cadence** — {disc_share:.0%} of the year on sale. "
            "Good balance of algorithm-driven visibility and price integrity."
        )

else:
    with ri_col2:
        st.metric("Year-1 Net (P50) — Baseline", fmt_usd(baseline_net))
    st.info("Add sales above to compare your discount calendar against the no-discount baseline.")

# ── SECTION 7 — Send to Revenue Optimizer ─────────────────────────────────────
st.divider()
c_send, c_clear = st.columns([2, 1])

with c_send:
    if st.button("📤 Send Calendar to Revenue Optimizer", type="primary", use_container_width=True):
        st.session_state["ro_discount_events"] = [
            {
                "name":         ev["name"],
                "start_date":   _as_date(ev["start_date"]),
                "end_date":     _as_date(ev["end_date"]),
                "discount_pct": ev["discount_pct"],
                "type":         ev.get("type", "custom"),
            }
            for ev in all_events
        ]
        st.success(
            f"✅ **{len(all_events)} events sent to Revenue Optimizer.** "
            "Navigate there to see the updated revenue projections with your discount calendar."
        )

with c_clear:
    if st.button("🗑️ Clear All Custom Sales", type="secondary", use_container_width=True):
        st.session_state.dp_custom_sales = []
        st.session_state.dp_platform_sel = {}
        st.session_state.dp_roadmap = []
        st.rerun()
