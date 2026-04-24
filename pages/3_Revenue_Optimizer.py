# Revenue Optimizer — Steam Commercial Suite
# Answers: "What price maximizes my Steam revenue, and how does my discount calendar affect it?"
# Inputs:  game description + P10/P50/P90 unit estimate + optional dev cost
# Outputs: revenue curve across price tiers + discount calendar impact + breakeven line

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
import pandas as pd
import altair as alt
from datetime import date, datetime

from src.revenue_optimizer import (
    STEAM_PRICE_TIERS,
    DEFAULT_ELASTICITY,
    GENRE_ELASTICITY_DEFAULTS,
    get_genre_elasticity,
    build_price_curve,
    build_discount_calendar,
    compute_breakeven,
    load_steam_calendar,
    get_upcoming_sales,
    STEAM_SHARE,
    VAT_FACTOR,
    get_asp_factor,
)

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(page_title="Revenue Optimizer", page_icon="📈", layout="wide")

# ── Constants ──────────────────────────────────────────────────────────────────
GENRES = [
    "Action", "Action-Adventure", "Action RPG", "Adventure", "Battle Royale",
    "City Builder", "Co-op Shooter", "Extraction Shooter", "FPS", "Fighting",
    "Horror / Survival Horror", "Immersive Sim", "Life Sim", "Looter Shooter",
    "MMORPG", "MOBA", "Metroidvania", "Open World RPG", "Platformer",
    "Puzzle", "Racing", "Real-Time Strategy (RTS)", "Rhythm",
    "Roguelike / Roguelite", "Role-Playing (JRPG)", "Role-Playing (WRPG)",
    "Sandbox / Survival Craft", "Simulation", "Soulslike",
    "Sports", "Stealth", "Strategy (4X / Grand Strategy)",
    "Tactical RPG / Strategy", "Third-Person Shooter", "Tower Defense",
    "Turn-Based RPG", "Turn-Based Strategy", "Visual Novel"
]

TIER_LABELS = {"indie": "Indie", "aa": "AA", "aaa": "AAA"}
SENTIMENT_LABELS = {
    "very_positive":    "Very Positive (85%+ positive reviews)",
    "mostly_positive":  "Mostly Positive (70–84%)",
    "mixed":            "Mixed (40–69%)",
}

CALENDAR = load_steam_calendar()

# ── Helper formatters ──────────────────────────────────────────────────────────
def fmt_units(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)

def fmt_usd(v: float) -> str:
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if v >= 1_000:
        return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def tier_color(is_base: bool, is_best: bool) -> str:
    if is_best:  return "#16a34a"   # green
    if is_base:  return "#2563eb"   # blue
    return "#64748b"                # slate

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🎮 Game")
    game_name = st.text_input("Game Name", value=st.session_state.get("ro_game_name", ""), placeholder="e.g. Hollow Knight")
    saved_genres = st.session_state.get("ro_genres", ["Action RPG"])
    if isinstance(saved_genres, str):
        saved_genres = [saved_genres]
    genres    = st.multiselect("Genre(s)", GENRES, default=[g for g in saved_genres if g in GENRES] or ["Action RPG"])
    if not genres:
        genres = ["Action RPG"]
    tier      = st.radio("Quality Tier", ["indie", "aa", "aaa"], format_func=lambda x: TIER_LABELS[x],
                         index=["indie","aa","aaa"].index(st.session_state.get("ro_tier","indie")),
                         horizontal=True)
    sentiment = st.radio("Expected Reviews", list(SENTIMENT_LABELS.keys()),
                         format_func=lambda x: SENTIMENT_LABELS[x],
                         index=1)

    st.divider()
    st.header("📊 Unit Forecast")
    st.caption("Enter your year-1 unit estimates. Use Launch Ledger output or your own model.")

    default_p50 = st.session_state.get("ro_units_p50", 25000)
    units_p50 = st.number_input("P50 Units  (expected)", min_value=100, max_value=10_000_000,
                                 value=default_p50, step=1000, format="%d")
    col_p10, col_p90 = st.columns(2)
    with col_p10:
        units_p10 = st.number_input("P10 (low)", min_value=100, max_value=10_000_000,
                                     value=int(units_p50 * 0.4), step=500, format="%d")
    with col_p90:
        units_p90 = st.number_input("P90 (high)", min_value=100, max_value=10_000_000,
                                     value=int(units_p50 * 2.5), step=1000, format="%d")

    st.caption("💡 P10 = pessimistic scenario · P90 = optimistic")

    st.divider()
    st.header("⚙️ Assumptions")

    base_price_str = st.select_slider(
        "Base Launch Price (USD)",
        options=[f"${p:.2f}" for p in STEAM_PRICE_TIERS],
        value=f"${st.session_state.get('ro_base_price', 19.99):.2f}"
    )
    base_price = float(base_price_str.replace("$", ""))

    genre_suggested_elasticity = get_genre_elasticity(genres)
    elasticity = st.slider(
        "Price Elasticity",
        min_value=-1.6, max_value=-0.3,
        value=float(st.session_state.get("ro_elasticity", genre_suggested_elasticity)),
        step=0.1,
        help="How sensitive buyers are to price changes. -1.0 = 10% price hike → 10% fewer units. "
             "More negative = more price-sensitive audience (mass-market). "
             "Less negative = less price-sensitive (niche/enthusiast)."
    )
    st.session_state["ro_elasticity"] = elasticity
    elasticity_label = "⚠️ Low sensitivity — may overstate revenue at higher prices" if elasticity > -0.7 else (
                       "✅ Typical competitive genre sensitivity" if elasticity <= -1.1 else
                       "📊 Moderate sensitivity — typical for indie/AA")
    st.caption(f"Genre suggested: **{genre_suggested_elasticity:.1f}** · Current: {elasticity:.1f} · {elasticity_label}")

    st.divider()
    st.header("💰 Breakeven (optional)")
    dev_cost_input = st.number_input("Total Dev Cost (USD)", min_value=0, value=0, step=10000, format="%d",
                                      help="Enter your total development cost to see a breakeven overlay.")
    dev_cost = float(dev_cost_input) if dev_cost_input > 0 else None

    st.divider()
    st.header("📅 Launch Date")
    launch_date_input = st.date_input("Expected Launch Date", value=date(2026, 9, 1),
                                       min_value=date(2026, 1, 1), max_value=date(2027, 12, 31))

    # Persist to session state
    st.session_state["ro_game_name"]  = game_name
    st.session_state["ro_genres"]     = genres
    st.session_state["ro_tier"]       = tier
    st.session_state["ro_units_p50"]  = units_p50
    st.session_state["ro_base_price"] = base_price

# ── Main panel ─────────────────────────────────────────────────────────────────
st.title("📈 Revenue Optimizer")
st.caption(
    f"**{game_name or 'Your Game'}** · {TIER_LABELS[tier]} · {' / '.join(genres)} · "
    f"Base price ${base_price:.2f} · P50 {fmt_units(units_p50)} units"
)

if units_p50 < 100:
    st.warning("Enter your unit estimates in the sidebar to begin.")
    st.stop()

# ── Section 1: Price Tier Revenue Curve ───────────────────────────────────────
st.subheader("💵 Revenue at Price Tiers")
st.caption(
    f"Units adjusted per price tier using elasticity = {elasticity:.1f}. "
    f"ASP = {get_asp_factor(tier, sentiment):.0%} of MSRP (blended year-1, post-discount). "
    f"Net = gross × {STEAM_SHARE:.0%} Steam share × {VAT_FACTOR:.0%} VAT factor."
)
with st.expander("ℹ️ How are the ASP factor and elasticity calculated?"):
    st.markdown(f"""
**Blended ASP Factor ({get_asp_factor(tier, sentiment):.0%})**

This is a *static calibration factor* — not a real-time calculation from your specific discount schedule.
It represents the **year-1 average selling price as a fraction of MSRP**, and accounts for three things:

| Source of discount | Typical impact |
|---|---|
| Seasonal sale events (Summer, Winter, etc.) | −10% to −30% on MSRP |
| Regional pricing (Steam suggests lower prices for EM markets) | −5% to −15% |
| Refunds (Steam's 2-hour policy) | −1% to −3% |

The factor was calibrated from benchmark titles tracked in Launch Ledger (Apr 2026) across indie, AA, and AAA tiers.
It is **not** derived from unit-split data (units sold at full price vs. discount price) — that level of
per-game sales velocity data is not publicly available on Steam.

The **Discount Calendar section below** provides a more granular simulation: it weights actual days at each
sale price against full-price days to compute a calendar-specific blended ASP for your chosen events.

**Price Elasticity ({elasticity:.1f})**

Standard price elasticity of demand: for every 10% price increase, units sold change by `elasticity × 10%`.
At −0.8 (default): a 10% price hike → ~8% fewer units sold (slightly inelastic — buyers aren't extremely sensitive).
Research range for Steam games: **−0.5** (niche/premium titles) to **−1.5** (mass-market/casual titles).
This is a model assumption — your game's actual elasticity depends on genre, comp set, and audience.
    """)


curve = build_price_curve(
    base_units_p10=float(units_p10),
    base_units_p50=float(units_p50),
    base_units_p90=float(units_p90),
    base_price=base_price,
    quality_tier=tier,
    sentiment=sentiment,
    elasticity=elasticity,
)

best_tier = next((r for r in curve if r.revenue_maximizing), curve[0])
base_tier = next((r for r in curve if r.is_base_price), curve[0])

# ── KPI strip ──────────────────────────────────────────────────────────────────
k1, k2, k3, k4 = st.columns(4)
k1.metric("Revenue-Maximizing Price", f"${best_tier.price:.2f}",
          delta=f"{(best_tier.price - base_price):+.2f} vs base" if best_tier.price != base_price else "= your base price")
k2.metric("P50 Net Revenue at Best Price", fmt_usd(best_tier.net_p50))
k3.metric("P50 Net Revenue at Base Price", fmt_usd(base_tier.net_p50))
k4.metric("Revenue Uplift (base → best)",
          f"{((best_tier.net_p50 / base_tier.net_p50) - 1)*100:+.1f}%" if base_tier.net_p50 > 0 else "—")

st.divider()

# ── Revenue curve table ────────────────────────────────────────────────────────
table_rows = []
for r in curve:
    label_parts = []
    if r.is_base_price:       label_parts.append("◀ base")
    if r.revenue_maximizing:  label_parts.append("★ best")
    label = "  ".join(label_parts)

    table_rows.append({
        "Price":         f"${r.price:.2f}",
        "Flag":          label,
        "P10 Units":     fmt_units(r.units_p10),
        "P50 Units":     fmt_units(r.units_p50),
        "P90 Units":     fmt_units(r.units_p90),
        "P10 Net Rev":   fmt_usd(r.net_p10),
        "P50 Net Rev":   fmt_usd(r.net_p50),
        "P90 Net Rev":   fmt_usd(r.net_p90),
        "Blended ASP":   f"${r.blended_price:.2f}",
        "_net_p50_raw":  r.net_p50,
        "_is_base":      r.is_base_price,
        "_is_best":      r.revenue_maximizing,
    })

table_df = pd.DataFrame(table_rows)

def highlight_row(row):
    if row["_is_best"] and row["_is_base"]:
        return ["background-color: #dcfce7; font-weight: bold"] * len(row)
    elif row["_is_best"]:
        return ["background-color: #dcfce7; font-weight: bold"] * len(row)
    elif row["_is_base"]:
        return ["background-color: #dbeafe"] * len(row)
    return [""] * len(row)

display_cols = ["Price", "Flag", "P10 Units", "P50 Units", "P90 Units", "P10 Net Rev", "P50 Net Rev", "P90 Net Rev", "Blended ASP"]
st.dataframe(
    table_df[display_cols + ["_is_best", "_is_base"]].style.apply(highlight_row, axis=1),
    column_config={
        "_is_best": None,
        "_is_base": None,
        "Flag": st.column_config.TextColumn("", width="small"),
    },
    hide_index=True,
    use_container_width=True,
)
st.caption("🟦 Blue = your base price · 🟩 Green = revenue-maximizing tier")

# ── Revenue curve chart ────────────────────────────────────────────────────────
chart_data = pd.DataFrame([{
    "Price Tier": f"${r.price:.2f}",
    "P10 Net":    r.net_p10 / 1000,
    "P50 Net":    r.net_p50 / 1000,
    "P90 Net":    r.net_p90 / 1000,
    "order":      r.price,
} for r in curve])

chart_data = chart_data.sort_values("order")

price_order = chart_data["Price Tier"].tolist()

base = alt.Chart(chart_data).encode(
    x=alt.X("Price Tier:N", sort=price_order, title="Launch Price (USD)"),
)

bar_p50 = base.mark_bar(color="#2563eb", width=30).encode(
    y=alt.Y("P50 Net:Q", title="Year-1 Net Revenue ($K)"),
    tooltip=[
        alt.Tooltip("Price Tier:N", title="Price"),
        alt.Tooltip("P50 Net:Q",   title="P50 Net ($K)", format=".1f"),
        alt.Tooltip("P10 Net:Q",   title="P10 Net ($K)", format=".1f"),
        alt.Tooltip("P90 Net:Q",   title="P90 Net ($K)", format=".1f"),
    ]
)

# P10/P90 error bars
error = base.mark_errorbar(color="#94a3b8", thickness=2, ticks=True).encode(
    y=alt.Y("P10 Net:Q", title=""),
    y2="P90 Net:Q",
)

# Highlight best price tier
best_marker = alt.Chart(pd.DataFrame([{
    "Price Tier": f"${best_tier.price:.2f}",
    "y": best_tier.net_p50 / 1000,
}])).mark_point(color="#16a34a", size=120, shape="triangle-up", filled=True).encode(
    x=alt.X("Price Tier:N", sort=price_order),
    y=alt.Y("y:Q"),
    tooltip=[alt.Tooltip("Price Tier:N", title="Revenue-maximizing price")],
)

chart = (bar_p50 + error + best_marker).properties(height=380).configure_axis(
    labelFontSize=12, titleFontSize=13
)

st.altair_chart(chart, use_container_width=True)
st.caption("Bars = P50. Error bars = P10 (pessimistic) to P90 (optimistic). ▲ = revenue-maximizing tier.")

# ── Positioning insight ────────────────────────────────────────────────────────
if best_tier.price > base_price:
    diff_pct = (best_tier.price / base_price - 1) * 100
    st.info(
        f"📊 **Pricing opportunity detected:** The revenue-maximizing price is **${best_tier.price:.2f}** — "
        f"{diff_pct:.0f}% above your base price. At elasticity {elasticity:.1f}, the extra margin "
        f"outweighs the unit loss. Consider whether your comp set supports this premium."
    )
elif best_tier.price < base_price:
    diff_pct = (1 - best_tier.price / base_price) * 100
    st.info(
        f"📊 **Price sensitivity signal:** The revenue-maximizing price is **${best_tier.price:.2f}** — "
        f"{diff_pct:.0f}% below your base. At elasticity {elasticity:.1f}, the volume gained from "
        f"a lower price outweighs the per-unit margin loss."
    )
else:
    st.success(f"✅ Your base price **${base_price:.2f}** is already the revenue-maximizing tier.")

st.divider()

# ── Section 2: Breakeven ───────────────────────────────────────────────────────
if dev_cost and dev_cost > 0:
    st.subheader("💰 Breakeven Analysis")

    be_units = compute_breakeven(dev_cost, base_price, tier, sentiment)
    asp = get_asp_factor(tier, sentiment)
    net_per_unit = base_price * asp * STEAM_SHARE * VAT_FACTOR

    be1, be2, be3 = st.columns(3)
    be1.metric("Dev Cost", fmt_usd(dev_cost))
    be2.metric(f"Units to Break Even at ${base_price:.2f}", fmt_units(int(be_units)))
    be3.metric("Net Revenue per Unit", f"${net_per_unit:.2f}")

    # Compare to P50
    if units_p50 >= be_units:
        surplus_pct = (units_p50 / be_units - 1) * 100
        st.success(
            f"✅ **P50 ({fmt_units(units_p50)} units) covers dev cost** with {surplus_pct:.0f}% surplus "
            f"({fmt_units(int(units_p50 - be_units))} units above breakeven)."
        )
    elif units_p90 >= be_units:
        st.warning(
            f"⚠️ **P50 misses breakeven** by {fmt_units(int(be_units - units_p50))} units. "
            f"P90 ({fmt_units(units_p90)}) covers it — you need an optimistic outcome."
        )
    else:
        st.error(
            f"🔴 **Even your P90 ({fmt_units(units_p90)}) doesn't reach breakeven** "
            f"({fmt_units(int(be_units))} units needed). Consider: lower scope, higher price, "
            f"or publisher funding."
        )

    # Breakeven across price tiers
    be_rows = []
    for r in curve:
        be_n = compute_breakeven(dev_cost, r.price, tier, sentiment)
        be_rows.append({
            "Price":             f"${r.price:.2f}",
            "Break-Even Units":  fmt_units(int(be_n)),
            "P50 vs Breakeven":  f"{'✅' if units_p50 >= be_n else '❌'} {fmt_units(units_p50)} / {fmt_units(int(be_n))}",
            "_base":             r.is_base_price,
            "_best":             r.revenue_maximizing,
        })

    be_df = pd.DataFrame(be_rows)
    st.markdown("**Breakeven units by price tier:**")
    st.dataframe(be_df[["Price", "Break-Even Units", "P50 vs Breakeven"]].style.apply(
        lambda row: ["background-color: #dcfce7" if be_rows[row.name]["_best"] else
                     "background-color: #dbeafe" if be_rows[row.name]["_base"] else "" for _ in row], axis=1
    ), hide_index=True, use_container_width=True)

    st.divider()

# ── Section 3: Steam Discount Calendar ────────────────────────────────────────
st.subheader("📅 Steam Discount Calendar Impact")
st.caption(
    "Select which Steam sales you'll participate in and set your discount depth. "
    "See how your discount strategy affects your year-1 blended revenue."
)

# Load upcoming sales relative to launch date
upcoming = get_upcoming_sales(launch_date_input, CALENDAR, months_ahead=13)

if not upcoming:
    st.info("No Steam sales found in the 12 months after your launch date. Check your launch date or update the calendar config.")
else:
    # Genre-relevance filter for themed fests
    def is_genre_eligible(event: dict) -> bool:
        if event["type"] == "tentpole":
            return True
        eligible = event.get("eligible_genres", [])
        if not eligible:
            return True
        return any(
            g_sel.lower() in g_elig.lower() or g_elig.lower() in g_sel.lower()
            for g_sel in genres
            for g_elig in eligible
        )

    tentpoles = [e for e in upcoming if e["type"] == "tentpole"]
    themed    = [e for e in upcoming if e["type"] == "themed"]
    eligible_themed = [e for e in themed if is_genre_eligible(e)]
    ineligible_themed = [e for e in themed if not is_genre_eligible(e)]

    st.markdown("### 🔥 Seasonal Sales (open to all games)")
    selected_events = []

    for ev in tentpoles:
        duration = (ev["end_date"] - ev["start_date"]).days
        col_check, col_name, col_dates, col_slider = st.columns([0.5, 3, 2, 3])
        with col_check:
            include = st.checkbox("", key=f"ev_{ev['name']}", value=True, label_visibility="collapsed")
        with col_name:
            st.markdown(f"**{ev['name']}**")
            if ev.get("notes"):
                st.caption(ev["notes"])
        with col_dates:
            st.caption(f"{ev['start_date'].strftime('%b %d')} – {ev['end_date'].strftime('%b %d, %Y')}  ({duration}d)")
        with col_slider:
            if include:
                depth = st.slider(
                    "Discount", 10, 75, 33, 5,
                    key=f"depth_{ev['name']}",
                    format="%d%%"
                )
                selected_events.append({**ev, "discount_pct": depth})
        if not include:
            st.session_state.pop(f"depth_{ev['name']}", None)

    if eligible_themed:
        genre_label = " / ".join(genres)
        st.markdown(f"### 🎪 Genre-Eligible Themed Fests ({len(eligible_themed)} for {genre_label})")
        st.caption("Fests where at least one of your genres qualifies. Themed fests can meaningfully boost discovery.")

        for ev in eligible_themed:
            duration = (ev["end_date"] - ev["start_date"]).days
            col_check, col_name, col_dates, col_slider = st.columns([0.5, 3, 2, 3])
            with col_check:
                include = st.checkbox("", key=f"ev_{ev['name']}", value=False, label_visibility="collapsed")
            with col_name:
                st.markdown(f"**{ev['name']}**")
                if ev.get("notes"):
                    st.caption(ev["notes"])
            with col_dates:
                st.caption(f"{ev['start_date'].strftime('%b %d')} – {ev['end_date'].strftime('%b %d, %Y')}  ({duration}d)")
            with col_slider:
                if include:
                    depth = st.slider(
                        "Discount", 10, 75, 20, 5,
                        key=f"depth_{ev['name']}",
                        format="%d%%"
                    )
                    selected_events.append({**ev, "discount_pct": depth})

    if ineligible_themed:
        with st.expander(f"⬜ {len(ineligible_themed)} themed fests not matching your genre"):
            for ev in ineligible_themed:
                st.caption(f"{ev['name']} — {ev['start_date'].strftime('%b %d')}–{ev['end_date'].strftime('%b %d')}: {ev.get('notes','')}")

    st.divider()

    # ── Calendar impact calculation ────────────────────────────────────────────
    if selected_events:
        cal_result = build_discount_calendar(
            launch_price=base_price,
            quality_tier=tier,
            sentiment=sentiment,
            selected_events=selected_events,
        )

        st.markdown("### 📊 Blended Revenue Impact")

        ci1, ci2, ci3, ci4 = st.columns(4)
        ci1.metric("Sale Events Selected",    len(selected_events))
        ci2.metric("Total Discount Days",      cal_result.discount_days,
                   delta=f"{cal_result.discount_days} of 365 days ({cal_result.discount_days/365*100:.0f}%)")
        ci3.metric("Blended ASP",              f"${cal_result.blended_asp:.2f}",
                   delta=f"−{(1 - cal_result.blended_asp / base_price)*100:.1f}% vs full price")
        ci4.metric("Year-1 ASP Factor",        f"{cal_result.blended_asp_factor:.1%}")

        # Revenue at base price with this discount calendar
        adj_net_p50 = units_p50 * cal_result.blended_asp * STEAM_SHARE * VAT_FACTOR
        base_net_p50 = units_p50 * base_price * get_asp_factor(tier, sentiment) * STEAM_SHARE * VAT_FACTOR
        # Note: base_net_p50 already assumes typical discounting via ASP factor
        # The calendar result shows the *specific* discount cadence chosen

        st.markdown(f"""
        <div style="background: linear-gradient(135deg, #f0fdf4, #dcfce7);
                    border: 1px solid #22c55e; border-radius: 10px;
                    padding: 16px 20px; margin: 12px 0;">
            <h4 style="color: #15803d; margin: 0 0 8px 0;">
                📈 Estimated Year-1 Net Revenue with Selected Discount Calendar
            </h4>
            <p style="font-size: 28px; font-weight: bold; color: #15803d; margin: 0;">
                {fmt_usd(adj_net_p50)}
                <span style="font-size: 14px; font-weight: normal; color: #166534;">
                  (P50 · ${base_price:.2f} launch · {len(selected_events)} sales)
                </span>
            </p>
            <p style="color: #166534; margin: 8px 0 0 0; font-size: 13px;">
                Blended ASP: ${cal_result.blended_asp:.2f} ×
                {units_p50:,} P50 units ×
                70% Steam share ×
                88% VAT factor
            </p>
        </div>
        """, unsafe_allow_html=True)

        # Discount calendar timeline
        st.markdown("### 🗓️ Selected Discount Events")
        cal_rows = []
        for ev in sorted(selected_events, key=lambda e: e["start_date"]):
            duration = (ev["end_date"] - ev["start_date"]).days
            sale_price = base_price * (1 - ev["discount_pct"] / 100)
            cal_rows.append({
                "Event":          ev["name"],
                "Dates":          f"{ev['start_date'].strftime('%b %d')} – {ev['end_date'].strftime('%b %d')}",
                "Days":           duration,
                "Discount":       f"{ev['discount_pct']:.0f}%",
                "Sale Price":     f"${sale_price:.2f}",
                "Type":           ev["type"].title(),
            })
        st.dataframe(pd.DataFrame(cal_rows), hide_index=True, use_container_width=True)

        # Cooldown check
        if selected_events:
            sorted_evs = sorted(selected_events, key=lambda e: e["start_date"])
            cooldown_warnings = []
            for i in range(1, len(sorted_evs)):
                prev_end   = sorted_evs[i-1]["end_date"]
                curr_start = sorted_evs[i]["start_date"]
                gap = (curr_start - prev_end).days
                if gap < 30:
                    cooldown_warnings.append(
                        f"⚠️ **{sorted_evs[i]['name']}** starts {gap}d after **{sorted_evs[i-1]['name']}** ends — "
                        f"Steam requires 30d cooldown between discounts."
                    )
            if cooldown_warnings:
                st.warning("\n\n".join(cooldown_warnings))
            else:
                st.success("✅ All selected events respect Steam's 30-day cooldown rule.")

    else:
        st.info("Select at least one sale event above to see calendar impact.")

# ── Section 4: Summary card ────────────────────────────────────────────────────
st.divider()
st.subheader("📋 Decision Summary")

asp_factor = get_asp_factor(tier, sentiment)
summary_rows = [
    ("Game",              game_name or "—"),
    ("Tier / Genre",      f"{TIER_LABELS[tier]} · {' / '.join(genres)}"),
    ("Base Launch Price", f"${base_price:.2f}"),
    ("Revenue-Max Price", f"${best_tier.price:.2f}"),
    ("P50 Units",         fmt_units(units_p50)),
    ("P50 Net Rev (base price)", fmt_usd(base_tier.net_p50)),
    ("P50 Net Rev (best price)", fmt_usd(best_tier.net_p50)),
    ("Blended ASP Factor",       f"{asp_factor:.1%}"),
    ("Elasticity Used",   f"{elasticity:.1f}"),
]
if dev_cost:
    be = compute_breakeven(dev_cost, base_price, tier, sentiment)
    summary_rows.append(("Dev Cost / Breakeven", f"{fmt_usd(dev_cost)} / {fmt_units(int(be))} units"))

summary_df = pd.DataFrame(summary_rows, columns=["Parameter", "Value"])
st.dataframe(summary_df, hide_index=True, use_container_width=True)

st.caption(
    "💡 Net revenue = units × blended ASP × 70% Steam share × 88% VAT factor. "
    "ASP factor is a static calibration from benchmark titles (Launch Ledger, Apr 2026) — "
    "it approximates the effect of seasonal discounts, regional pricing, and refunds on year-1 realized price. "
    "Elasticity is a model assumption; adjust it in the sidebar to reflect your audience's price sensitivity."
)
