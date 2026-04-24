# pages/0_Comp_Set_Builder.py
# Comp Set Builder — Steam Commercial Suite
# Find comparable Steam games, estimate their year-1 performance via the Boxleiter method,
# and benchmark your game against them.
# Output: P10 / P50 / P90 unit estimates → pushed to Revenue Optimizer via session state.

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import math
import streamlit as st
import pandas as pd
import altair as alt

from src.steam_client import SteamClient, parse_app_details, parse_review_summary, SteamAPIError
from src.estimator import Estimator
from src.revenue_optimizer import STEAM_SHARE, VAT_FACTOR, get_asp_factor

st.set_page_config(page_title="Comp Set Builder", page_icon="🎯", layout="wide")

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
    "Turn-Based RPG", "Turn-Based Strategy", "Visual Novel", "Other"
]
TIER_LABELS = {"indie": "Indie", "aa": "AA", "aaa": "AAA"}

# ── Formatters ─────────────────────────────────────────────────────────────────
def fmt_units(n):
    if n is None: return "—"
    n = int(n)
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.0f}K"
    return str(n)

def fmt_usd(v):
    if v is None: return "—"
    if v >= 1_000_000: return f"${v/1_000_000:.2f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:.0f}"

def sentiment_label(ratio: float) -> str:
    if ratio >= 0.95: return "Overwhelmingly Positive"
    if ratio >= 0.85: return "Very Positive"
    if ratio >= 0.70: return "Mostly Positive"
    if ratio >= 0.40: return "Mixed"
    return "Mostly Negative"

def sentiment_emoji(ratio: float) -> str:
    if ratio >= 0.85: return "🟢"
    if ratio >= 0.70: return "🟡"
    return "🔴"

def map_steam_genre(steam_genres: list) -> str:
    """Best-match a Steam genres list to our taxonomy."""
    for sg in steam_genres:
        for og in GENRES:
            if sg.lower() in og.lower() or og.lower() in sg.lower():
                return og
    return "Other"

# ── Cached data fetchers ───────────────────────────────────────────────────────
@st.cache_data(ttl=3600, show_spinner=False)
def search_steam(query: str) -> list:
    try:
        return SteamClient().search_games(query, max_results=10)
    except Exception:
        return []

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_comp_data(appid: int) -> dict:
    try:
        client = SteamClient()
        raw_details = client.get_app_details(appid)
        raw_reviews = client.get_review_summary(appid)
        details = parse_app_details(raw_details)
        reviews = parse_review_summary(raw_reviews)
        # Convert datetime to string for cache serialization
        if details.get("release_date"):
            details["release_date_str"] = details["release_date"].strftime("%b %d, %Y")
            details["release_date"] = None
        return {"details": details, "reviews": reviews, "error": None}
    except SteamAPIError as e:
        return {"details": None, "reviews": None, "error": str(e)}
    except Exception as e:
        return {"details": None, "reviews": None, "error": f"Unexpected error: {e}"}

# Year-1 review share by age — what fraction of lifetime reviews fell in year 1?
# Heuristic: games front-load ~50-60% of lifetime reviews in year 1.
# Used to back-calculate estimated year-1 reviews for older comps.
YEAR1_REVIEW_SHARE = {
    0:  1.00,   # < 12 months: in year 1, use actual count
    12: 0.55,   # 1-2 years old: ~55% of reviews were in year 1
    24: 0.48,   # 2-3 years old
    36: 0.42,   # 3-5 years old
    60: 0.35,   # 5+ years old
}

def estimate_year1_reviews(total_reviews: int, age_months: float | None) -> tuple[int, bool]:
    """
    Return (estimated_year1_reviews, was_adjusted).
    If age < 12 months, returns actual count unchanged.
    For older games, applies a year-1 share factor to back-calculate year-1 reviews.
    """
    if age_months is None or age_months < 12:
        return total_reviews, False
    for min_age in sorted(YEAR1_REVIEW_SHARE.keys(), reverse=True):
        if age_months >= min_age:
            factor = YEAR1_REVIEW_SHARE[min_age]
            return max(1, int(total_reviews * factor)), True
    return total_reviews, False


def run_estimation(data: dict, user_tier: str, genre: str,
                   is_game_pass: bool, is_early_access: bool, is_short_game: bool):
    if data.get("error") or not data.get("details") or not data.get("reviews"):
        return None
    try:
        details = data["details"].copy()
        reviews = data["reviews"].copy()
        age_months = details.get("age_months")

        # Year-1 adjustment: for older games, estimate year-1 review count
        total_reviews = reviews.get("total_reviews", 0)
        yr1_reviews, was_adjusted = estimate_year1_reviews(total_reviews, age_months)
        if was_adjusted:
            reviews = reviews.copy()
            reviews["total_reviews"]  = yr1_reviews
            reviews["total_positive"] = max(1, int(yr1_reviews * reviews.get("sentiment_ratio", 0.8)))
            reviews["total_negative"] = yr1_reviews - reviews["total_positive"]
            reviews["_yr1_adjusted"]  = True
            reviews["_yr1_factor"]    = yr1_reviews / total_reviews if total_reviews > 0 else 1.0

        return Estimator().estimate(
            parsed_details=details,
            parsed_reviews=reviews,
            genre=genre,
            quality_tier=user_tier,
            is_game_pass=is_game_pass,
            is_early_access=is_early_access,
            is_short_game=is_short_game,
        )
    except Exception:
        return None

# ── Session state init ─────────────────────────────────────────────────────────
if "csb_comps"   not in st.session_state: st.session_state.csb_comps   = []
if "csb_results" not in st.session_state: st.session_state.csb_results = {}
if "csb_search"  not in st.session_state: st.session_state.csb_search  = []

# ── Sidebar ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.header("🎮 Your Game")
    my_name = st.text_input("Game Name", value=st.session_state.get("ro_game_name", ""),
                             placeholder="e.g. Hollow Knight")
    saved_genres = st.session_state.get("ro_genres", ["Action RPG"])
    if isinstance(saved_genres, str): saved_genres = [saved_genres]
    my_genres = st.multiselect("Genre(s)", GENRES,
                                default=[g for g in saved_genres if g in GENRES] or ["Action RPG"])
    if not my_genres: my_genres = ["Action RPG"]
    my_tier = st.radio("Quality Tier", ["indie", "aa", "aaa"],
                        format_func=lambda x: TIER_LABELS[x],
                        index=["indie","aa","aaa"].index(st.session_state.get("ro_tier","indie")),
                        horizontal=True)
    my_price = st.number_input("Your Target Price (USD)", min_value=0.99, max_value=99.99,
                                value=float(st.session_state.get("ro_base_price", 19.99)),
                                step=5.0, format="%.2f")

    st.session_state["ro_game_name"]  = my_name
    st.session_state["ro_genres"]     = my_genres
    st.session_state["ro_tier"]       = my_tier
    st.session_state["ro_base_price"] = my_price

    st.divider()
    st.caption(
        "📊 Build a comp set of 5–10 released Steam games similar to yours. "
        "Their review counts feed the Boxleiter estimation model to produce a "
        "unit benchmark for your game."
    )

# ── Page header ────────────────────────────────────────────────────────────────
st.title("🎯 Comp Set Builder")
st.caption(
    "Find comparable released Steam games → estimate their performance → "
    "benchmark your game and send P10/P50/P90 unit estimates to the Revenue Optimizer."
)

# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Add comps
# ═══════════════════════════════════════════════════════════════════════════════
st.subheader("1️⃣ Add Comparable Games")

tab_search, tab_appid = st.tabs(["🔍 Search by Name", "🔢 Enter App ID Directly"])

with tab_search:
    with st.form("search_form", clear_on_submit=False):
        q = st.text_input("Search Steam", placeholder="e.g. Hades, Dead Cells, Hollow Knight")
        searched = st.form_submit_button("Search", type="primary")

    if searched and q.strip():
        with st.spinner("Searching Steam…"):
            st.session_state.csb_search = search_steam(q.strip())

    if st.session_state.csb_search:
        existing_ids = {c["appid"] for c in st.session_state.csb_comps}
        results = [r for r in st.session_state.csb_search if r["appid"] not in existing_ids]
        if not results:
            st.info("All results already in your comp set.")
        else:
            for r in results:
                c1, c2 = st.columns([5, 1])
                price_str = f"${r['price_usd']:.2f}" if r.get("price_usd") else "Free/Unknown"
                c1.markdown(f"**{r['name']}** — {price_str} · App ID: `{r['appid']}`")
                if c2.button("Add", key=f"add_{r['appid']}"):
                    st.session_state.csb_comps.append({
                        "appid":          r["appid"],
                        "name":           r["name"],
                        "user_tier":      my_tier,
                        "genre":          my_genres[0] if my_genres else "Other",
                        "is_game_pass":   False,
                        "is_early_access": False,
                        "is_short_game":  False,
                        "weight":         1.0,
                    })
                    st.session_state.csb_results.pop(r["appid"], None)
                    st.rerun()

with tab_appid:
    with st.form("appid_form"):
        col_id, col_name = st.columns([2, 3])
        manual_id   = col_id.number_input("Steam App ID", min_value=1, value=None,
                                           placeholder="e.g. 1145360")
        manual_name = col_name.text_input("Display Name (optional)", placeholder="Hades")
        add_manual  = st.form_submit_button("Add by App ID", type="primary")

    if add_manual and manual_id:
        existing_ids = {c["appid"] for c in st.session_state.csb_comps}
        if int(manual_id) in existing_ids:
            st.warning("This App ID is already in your comp set.")
        else:
            st.session_state.csb_comps.append({
                "appid":          int(manual_id),
                "name":           manual_name.strip() or f"App {manual_id}",
                "user_tier":      my_tier,
                "genre":          my_genres[0] if my_genres else "Other",
                "is_game_pass":   False,
                "is_early_access": False,
                "is_short_game":  False,
                "weight":         1.0,
            })
            st.session_state.csb_results.pop(int(manual_id), None)
            st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Comp list + configuration
# ═══════════════════════════════════════════════════════════════════════════════
if not st.session_state.csb_comps:
    st.info("No comps added yet. Use the search above to find comparable games.")
    st.stop()

st.divider()
st.subheader(f"2️⃣ Your Comp Set ({len(st.session_state.csb_comps)} games)")
st.caption("Set quality tier, primary genre, and any distortion flags for each comp.")

for i, comp in enumerate(st.session_state.csb_comps):
    with st.expander(f"**{comp['name']}** — App ID {comp['appid']}", expanded=False):
        ec1, ec2, ec3 = st.columns([2, 2, 3])

        new_tier = ec1.selectbox("Quality Tier", ["indie", "aa", "aaa"],
                                  format_func=lambda x: TIER_LABELS[x],
                                  index=["indie","aa","aaa"].index(comp["user_tier"]),
                                  key=f"tier_{comp['appid']}")
        new_genre = ec2.selectbox("Primary Genre (for estimate)", GENRES,
                                   index=GENRES.index(comp["genre"]) if comp["genre"] in GENRES else GENRES.index("Other"),
                                   key=f"genre_{comp['appid']}")

        with ec3:
            gp  = st.checkbox("Game Pass Day-1",   value=comp["is_game_pass"],   key=f"gp_{comp['appid']}")
            ea  = st.checkbox("Early Access",       value=comp["is_early_access"],key=f"ea_{comp['appid']}")
            sg  = st.checkbox("Short Game (<10h)",  value=comp["is_short_game"],  key=f"sg_{comp['appid']}")

        weight = st.slider(
            "Similarity Weight",
            min_value=0.25, max_value=2.0,
            value=float(comp.get("weight", 1.0)),
            step=0.25,
            key=f"wt_{comp['appid']}",
            help="How closely this comp resembles your game. "
                 "2x = very similar, pull it toward your benchmark. "
                 "0.25x = loosely comparable, reduce its influence.",
            format="%.2fx"
        )
        weight_label = "🎯 Very similar" if weight >= 1.75 else (
                       "✅ Similar" if weight >= 1.0 else
                       "📉 Loosely comparable" if weight >= 0.5 else "⬇️ Low influence")
        st.caption(f"Weight: **{weight:.2f}x** — {weight_label}")

        # Update comp config
        st.session_state.csb_comps[i]["user_tier"]       = new_tier
        st.session_state.csb_comps[i]["genre"]           = new_genre
        st.session_state.csb_comps[i]["is_game_pass"]    = gp
        st.session_state.csb_comps[i]["is_early_access"] = ea
        st.session_state.csb_comps[i]["is_short_game"]   = sg
        st.session_state.csb_comps[i]["weight"]          = weight

        if st.button("🗑️ Remove", key=f"remove_{comp['appid']}"):
            st.session_state.csb_comps.pop(i)
            st.session_state.csb_results.pop(comp["appid"], None)
            st.rerun()

st.caption(
    "💡 **Game Pass:** Suppresses Steam unit estimates (~0.39x factor — only Steam buyers, "
    "not all GP players). **Early Access:** Widens uncertainty bands. "
    "**Short Game:** Adjusts for higher refund rates."
)

# ── Analyze button ──────────────────────────────────────────────────────────────
st.divider()
n_comps = len(st.session_state.csb_comps)
if st.button(f"🔬 Analyze {n_comps} Comp{'s' if n_comps != 1 else ''}", type="primary", use_container_width=True):
    progress = st.progress(0, text="Fetching Steam data…")
    errors = []
    for i, comp in enumerate(st.session_state.csb_comps):
        progress.progress((i + 0.5) / n_comps, text=f"Fetching {comp['name']}…")
        data = fetch_comp_data(comp["appid"])
        if data["error"]:
            errors.append(f"**{comp['name']}**: {data['error']}")
            st.session_state.csb_results[comp["appid"]] = {"data": data, "estimate": None}
        else:
            # Auto-update name from Steam if we used a placeholder
            if data["details"] and comp["name"].startswith("App "):
                st.session_state.csb_comps[i]["name"] = data["details"].get("name", comp["name"])
            # Auto-suggest genre from Steam data if still using default
            if data["details"] and comp["genre"] == "Other":
                steam_genres = data["details"].get("genres", [])
                st.session_state.csb_comps[i]["genre"] = map_steam_genre(steam_genres)
                comp["genre"] = st.session_state.csb_comps[i]["genre"]
            estimate = run_estimation(data, comp["user_tier"], comp["genre"],
                                       comp["is_game_pass"], comp["is_early_access"],
                                       comp["is_short_game"])
            st.session_state.csb_results[comp["appid"]] = {"data": data, "estimate": estimate}
        progress.progress((i + 1) / n_comps)

    progress.empty()
    if errors:
        st.warning("Some comps had errors:\n\n" + "\n\n".join(errors))
    st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: Results
# ═══════════════════════════════════════════════════════════════════════════════
if not st.session_state.csb_results:
    st.stop()

st.divider()
st.subheader("3️⃣ Est. Year-1 Performance")
st.caption(
    "All estimates are calibrated to **year-1 launch performance** — "
    "the window that matters for launch strategy. "
    "For games older than 12 months, reviews are back-calculated to their estimated year-1 count "
    "(~50% of lifetime reviews occur in year 1). Labeled with 🔄 where adjusted."
)

# Build results table
rows = []
valid_p50s = []

for comp in st.session_state.csb_comps:
    appid = comp["appid"]
    result = st.session_state.csb_results.get(appid)
    if not result:
        continue

    data = result.get("data", {})
    est  = result.get("estimate")
    det  = data.get("details") or {}
    rev  = data.get("reviews") or {}

    name  = det.get("name") or comp["name"]
    price = det.get("current_price_usd")
    reviews_total  = rev.get("total_reviews", 0)
    sentiment      = rev.get("sentiment_ratio", 0.0)
    release_str    = det.get("release_date_str", "—")
    age_months     = det.get("age_months")
    yr1_reviews, was_yr1_adjusted = estimate_year1_reviews(reviews_total, age_months)
    flags = []
    if comp["is_game_pass"]:    flags.append("GP")
    if comp["is_early_access"]: flags.append("EA")
    if comp["is_short_game"]:   flags.append("Short")
    if was_yr1_adjusted:        flags.append("🔄 Yr1 est.")

    if est and est.primary:
        p = est.primary
        units_p10 = int(p.units_low)
        units_p50 = int(p.units_mid)
        units_p90 = int(p.units_high)
        multiplier = est.effective_multiplier

        asp = get_asp_factor(comp["user_tier"], "mostly_positive")
        net_p50 = units_p50 * (price or 0) * asp * STEAM_SHARE * VAT_FACTOR if price else None

        valid_p50s.append(units_p50)
        rows.append({
            "Title":         name,
            "Price":         f"${price:.2f}" if price else "—",
            "Released":      release_str,
            "Reviews":       f"{yr1_reviews:,}" + (f" (of {reviews_total:,})" if was_yr1_adjusted else ""),
            "Sentiment":     f"{sentiment_emoji(sentiment)} {sentiment:.0%}",
            "Tier":          TIER_LABELS[comp["user_tier"]],
            "Genre":         comp["genre"],
            "Multiplier":    f"{multiplier:.1f}x",
            "P10 Units":     fmt_units(units_p10),
            "P50 Units":     fmt_units(units_p50),
            "P90 Units":     fmt_units(units_p90),
            "Est. Net Rev (P50)": fmt_usd(net_p50),
            "Flags":         ", ".join(flags) if flags else "—",
            "_p50_raw":      units_p50,
            "_price_raw":    price or 0,
        })
    else:
        err = data.get("error", "No estimate available")
        rows.append({
            "Title":     name,
            "Price":     "—", "Released": "—", "Reviews": "—",
            "Sentiment": "—", "Tier":     TIER_LABELS[comp["user_tier"]],
            "Genre":     comp["genre"], "Multiplier": "—",
            "P10 Units": "—", "P50 Units": "Error", "P90 Units": "—",
            "Est. Net Rev (P50)": "—",
            "Flags":     f"⚠️ {err}",
            "_p50_raw":  0, "_price_raw": 0,
        })

if not rows:
    st.info("No results yet. Click Analyze above.")
    st.stop()

display_cols = ["Title", "Price", "Released", "Reviews", "Sentiment",
                "Tier", "Genre", "Multiplier", "P10 Units", "P50 Units",
                "P90 Units", "Est. Net Rev (P50)", "Flags"]
df = pd.DataFrame(rows)
st.dataframe(df[display_cols], hide_index=True, use_container_width=True)

with st.expander("ℹ️ How to read this table"):
    st.markdown("""
- **Multiplier**: the Boxleiter factor used (`estimated units = reviews × multiplier`).
  Adjusted for tier, genre, sentiment, volume decay, and any distortion flags.
- **P10 / P50 / P90**: pessimistic / expected / optimistic unit estimate for each comp.
  These are *lifetime-to-date* estimates based on current review count, not projections.
- **Est. Net Rev (P50)**: P50 units × current price × ASP factor × 70% Steam share × 88% VAT.
- **Flags**: GP = Game Pass day-1 (Steam units suppressed); EA = Early Access; Short = <10h playtime.
- **Young titles** (< 12 months old) will show lower review counts — estimates are partial-year only.
    """)

# ── Chart: P50 unit estimates per comp ────────────────────────────────────────
if len([r for r in rows if r["_p50_raw"] > 0]) >= 2:
    chart_df = pd.DataFrame([
        {"Title": r["Title"], "P50 Units (K)": r["_p50_raw"] / 1000,
         "P10 (K)": rows[i]["_p50_raw"] * 0.4 / 1000,
         "P90 (K)": rows[i]["_p50_raw"] * 2.0 / 1000}
        for i, r in enumerate(rows) if r["_p50_raw"] > 0
    ]).sort_values("P50 Units (K)", ascending=False)

    bar = alt.Chart(chart_df).mark_bar(color="#2563eb").encode(
        y=alt.Y("Title:N", sort="-x", title=None),
        x=alt.X("P50 Units (K):Q", title="Estimated Units (K)"),
        tooltip=[
            alt.Tooltip("Title:N"),
            alt.Tooltip("P50 Units (K):Q", format=".1f", title="P50 (K)"),
        ]
    ).properties(height=max(180, len(chart_df) * 40))

    st.altair_chart(bar, use_container_width=True)
    st.caption("P50 estimated total units per comp (sorted). Based on current review count × Boxleiter multiplier.")

# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: Benchmarking + your game's forecast
# ═══════════════════════════════════════════════════════════════════════════════
if len(valid_p50s) < 2:
    st.info("Add and analyze at least 2 comps with valid estimates to see benchmarking.")
    st.stop()

st.divider()
st.subheader("4️⃣ Where Does Your Game Land?")

# Build weighted p50 list — repeat values proportional to weight for percentile math
weighted_p50s = []
for comp in st.session_state.csb_comps:
    result = st.session_state.csb_results.get(comp["appid"])
    if not result: continue
    est = result.get("estimate")
    if not est or not est.primary: continue
    weight = comp.get("weight", 1.0)
    units = int(est.primary.units_mid)
    # Repeat with weight (round to nearest 0.25 steps = 4 repetitions max)
    reps = max(1, round(weight * 4))
    weighted_p50s.extend([units] * reps)

weighted_p50s_sorted = sorted(weighted_p50s)
n = len(weighted_p50s_sorted)
valid_p50s_sorted = sorted(valid_p50s)
nv = len(valid_p50s_sorted)
p25_val = weighted_p50s_sorted[max(0, math.ceil(n * 0.25) - 1)]
p50_val = weighted_p50s_sorted[math.ceil(n * 0.50) - 1]
p75_val = weighted_p50s_sorted[min(n - 1, math.ceil(n * 0.75) - 1)]

bk1, bk2, bk3, bk4 = st.columns(4)
bk1.metric("Comp Set Min",    fmt_units(min(valid_p50s)))
bk2.metric("25th Percentile", fmt_units(p25_val))
bk3.metric("Median",          fmt_units(p50_val))
bk4.metric("75th Percentile", fmt_units(p75_val))

st.caption("Distribution of P50 unit estimates across your comp set.")

st.markdown("**Set your game's expected positioning within the comp set:**")

positioning = st.select_slider(
    "My game will perform at…",
    options=["Bottom Quartile", "Lower-Mid", "Median", "Upper-Mid", "Top Quartile"],
    value="Median",
    help="Where you expect your game to land relative to these comps. "
         "Be honest — most games land at or below median."
)

# Map positioning → P50
pos_map = {
    "Bottom Quartile": min(valid_p50s),
    "Lower-Mid":       int((min(valid_p50s) + p25_val) / 2),
    "Median":          p50_val,
    "Upper-Mid":       int((p75_val + p50_val) / 2),
    "Top Quartile":    p75_val,
}
derived_p50 = pos_map[positioning]
derived_p10 = max(100, int(derived_p50 * 0.40))
derived_p90 = int(derived_p50 * 2.50)

# Comp set median price
comp_prices = [r["_price_raw"] for r in rows if r["_price_raw"] > 0]
median_comp_price = sorted(comp_prices)[len(comp_prices) // 2] if comp_prices else my_price

dc1, dc2, dc3, dc4 = st.columns(4)
dc1.metric("Your Derived P10 (pessimistic)", fmt_units(derived_p10))
dc2.metric("Your Derived P50 (expected)",    fmt_units(derived_p50))
dc3.metric("Your Derived P90 (optimistic)",  fmt_units(derived_p90))
dc4.metric("Comp Set Median Price",          f"${median_comp_price:.2f}")

st.info(
    f"📊 **{positioning}** positioning against your {n} comps → "
    f"**{fmt_units(derived_p50)} units** P50. "
    f"P10 = {fmt_units(derived_p10)} · P90 = {fmt_units(derived_p90)}. "
    f"Adjust your price in the Revenue Optimizer to see how it affects net revenue."
)

# ── Revenue preview at target price ───────────────────────────────────────────
from src.revenue_optimizer import get_asp_factor as _asp

asp_factor = _asp(my_tier, "mostly_positive")
rev_p50 = derived_p50 * my_price * asp_factor * STEAM_SHARE * VAT_FACTOR
rev_p10 = derived_p10 * my_price * asp_factor * STEAM_SHARE * VAT_FACTOR
rev_p90 = derived_p90 * my_price * asp_factor * STEAM_SHARE * VAT_FACTOR

st.markdown(f"""
<div style="background: linear-gradient(135deg, #eff6ff, #dbeafe);
            border: 1px solid #3b82f6; border-radius: 10px;
            padding: 16px 20px; margin: 12px 0;">
  <h4 style="color: #1d4ed8; margin: 0 0 8px 0;">
    📈 Year-1 Net Revenue Preview at ${my_price:.2f}
  </h4>
  <p style="font-size: 26px; font-weight: bold; color: #1d4ed8; margin: 0;">
    {fmt_usd(rev_p50)}
    <span style="font-size: 13px; font-weight: normal; color: #1e40af;">P50 · {positioning}</span>
  </p>
  <p style="color: #1e40af; margin: 8px 0 0 0; font-size: 13px;">
    Range: {fmt_usd(rev_p10)} (P10) → {fmt_usd(rev_p90)} (P90) ·
    ASP {asp_factor:.0%} × 70% Steam share × 88% VAT
  </p>
</div>
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: Send to Revenue Optimizer
# ═══════════════════════════════════════════════════════════════════════════════
st.divider()
st.subheader("5️⃣ Send to Revenue Optimizer")
st.caption(
    "This will pre-fill the Revenue Optimizer with your derived P10/P50/P90 estimates, "
    "game name, tier, and target price."
)

col_send, col_info = st.columns([2, 3])
with col_send:
    if st.button("→ Send to Revenue Optimizer", type="primary", use_container_width=True):
        st.session_state["ro_units_p50"]  = derived_p50
        st.session_state["ro_units_p10"]  = derived_p10
        st.session_state["ro_units_p90"]  = derived_p90
        st.session_state["ro_game_name"]  = my_name
        st.session_state["ro_genres"]     = my_genres
        st.session_state["ro_tier"]       = my_tier
        st.session_state["ro_base_price"] = my_price
        st.success(
            f"✅ Sent! Navigate to **Revenue Optimizer** to see the revenue curve "
            f"for {my_name or 'your game'} at ${my_price:.2f} with "
            f"P50 = {fmt_units(derived_p50)} units."
        )

with col_info:
    st.markdown(f"""
    **What gets sent:**
    - Game: *{my_name or '(unnamed)'}* · {TIER_LABELS[my_tier]} · {' / '.join(my_genres)}
    - P10: {fmt_units(derived_p10)} units
    - P50: {fmt_units(derived_p50)} units
    - P90: {fmt_units(derived_p90)} units
    - Base Price: ${my_price:.2f}
    """)
