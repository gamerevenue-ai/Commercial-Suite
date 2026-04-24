# src/revenue_optimizer.py
# Core logic for the Revenue Optimizer page.
# Computes revenue curves across Steam price tiers using price elasticity.
# All Steam economics: 70% developer share, blended 12% VAT (88% net factor).

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional
import yaml
import os

# ── Steam economics ────────────────────────────────────────────────────────────
STEAM_SHARE = 0.70       # Developer keeps 70% of gross
VAT_FACTOR  = 0.88       # Blended global VAT net factor (1 - 0.12 blended rate)

# Standard Steam price tiers (USD) — $10 increments at the low end, then $5/$10 steps
STEAM_PRICE_TIERS = [4.99, 9.99, 14.99, 19.99, 24.99, 29.99, 34.99, 39.99, 49.99, 59.99, 69.99, 79.99]

# Default price elasticity of demand for games on Steam.
# -0.8 = a 10% price increase → ~8% fewer units sold (slightly inelastic).
# Research range: -0.5 (premium/niche) to -1.5 (mass market).
DEFAULT_ELASTICITY = -0.8

# ── ASP blending factors ───────────────────────────────────────────────────────
# Year-1 blended ASP as a fraction of launch MSRP.
# Calibrated from Launch Ledger benchmark titles (Apr 2026).
# Accounts for: discount events, regional pricing, refunds.
ASP_FACTORS = {
    ("indie", "very_positive"): 0.95,
    ("indie", "mostly_positive"): 0.92,
    ("indie", "mixed"): 0.82,
    ("aa",    "very_positive"): 0.97,
    ("aa",    "mostly_positive"): 0.95,
    ("aa",    "mixed"): 0.88,
    ("aaa",   "very_positive"): 0.95,
    ("aaa",   "mostly_positive"): 0.93,
    ("aaa",   "mixed"): 0.85,
}


# ── Data classes ───────────────────────────────────────────────────────────────

@dataclass
class PriceTierResult:
    price: float
    units_p10: int
    units_p50: int
    units_p90: int
    asp_factor: float
    blended_price: float           # MSRP × ASP factor (before discount calendar adj)
    gross_p10: float
    gross_p50: float
    gross_p90: float
    net_p10: float
    net_p50: float
    net_p90: float
    is_base_price: bool = False
    revenue_maximizing: bool = False


@dataclass
class DiscountEvent:
    name: str
    start_date: date
    end_date: date
    discount_pct: float            # e.g. 33 = 33% off
    event_type: str                # "tentpole" | "themed" | "next_fest"

    @property
    def duration_days(self) -> int:
        return max(1, (self.end_date - self.start_date).days)

    @property
    def sale_price_factor(self) -> float:
        return 1.0 - (self.discount_pct / 100.0)


@dataclass
class DiscountCalendarResult:
    """Revenue impact of a chosen discount calendar over 12 months."""
    events: list[DiscountEvent]
    launch_price: float
    base_asp_factor: float
    year_days: int = 365

    # Computed fields
    discount_days: int = field(init=False, default=0)
    blended_asp: float = field(init=False, default=0.0)
    blended_asp_factor: float = field(init=False, default=0.0)

    def __post_init__(self):
        self.discount_days = sum(e.duration_days for e in self.events)
        full_price_days = max(0, self.year_days - self.discount_days)

        # Weighted blended price across all days
        discount_revenue = sum(
            e.duration_days * self.launch_price * e.sale_price_factor
            for e in self.events
        )
        full_revenue = full_price_days * self.launch_price
        self.blended_asp = (full_revenue + discount_revenue) / self.year_days
        self.blended_asp_factor = (self.blended_asp / self.launch_price) * self.base_asp_factor


@dataclass
class RevenueOptimizationResult:
    game_name: str
    base_price: float
    quality_tier: str
    sentiment: str
    elasticity: float
    price_curve: list[PriceTierResult]
    discount_calendar: Optional[DiscountCalendarResult]
    dev_cost: Optional[float]
    breakeven_units: Optional[float]     # at base price, P50 ASP
    breakeven_price_tier: Optional[float] # first tier where P50 net > dev cost


# ── Core functions ─────────────────────────────────────────────────────────────

def get_asp_factor(quality_tier: str, sentiment: str) -> float:
    key = (quality_tier.lower(), sentiment.lower())
    return ASP_FACTORS.get(key, 0.88)


def adjust_units_for_price(
    base_units: float,
    base_price: float,
    target_price: float,
    elasticity: float = DEFAULT_ELASTICITY,
) -> float:
    """
    Estimate units at target_price given base_units at base_price.
    Uses arc elasticity: % change in units = elasticity × % change in price.
    """
    if base_price <= 0 or target_price <= 0 or base_units <= 0:
        return base_units
    price_ratio = target_price / base_price
    return base_units * (price_ratio ** elasticity)


def select_display_tiers(base_price: float, n_below: int = 3, n_above: int = 3) -> list[float]:
    """Return the n_below tiers below and n_above tiers above the base price."""
    idx = min(range(len(STEAM_PRICE_TIERS)), key=lambda i: abs(STEAM_PRICE_TIERS[i] - base_price))
    low  = max(0, idx - n_below)
    high = min(len(STEAM_PRICE_TIERS), idx + n_above + 1)
    return STEAM_PRICE_TIERS[low:high]


def build_price_curve(
    base_units_p10: float,
    base_units_p50: float,
    base_units_p90: float,
    base_price: float,
    quality_tier: str,
    sentiment: str = "mostly_positive",
    elasticity: float = DEFAULT_ELASTICITY,
    price_tiers: Optional[list[float]] = None,
) -> list[PriceTierResult]:
    """
    Compute revenue at each price tier for P10 / P50 / P90 unit estimates.
    Returns list of PriceTierResult, one per tier, sorted low→high.
    """
    if price_tiers is None:
        price_tiers = select_display_tiers(base_price)

    asp = get_asp_factor(quality_tier, sentiment)
    results = []
    best_net_p50 = -1.0
    best_idx = 0

    for i, tier in enumerate(price_tiers):
        u10 = adjust_units_for_price(base_units_p10, base_price, tier, elasticity)
        u50 = adjust_units_for_price(base_units_p50, base_price, tier, elasticity)
        u90 = adjust_units_for_price(base_units_p90, base_price, tier, elasticity)

        blended = tier * asp
        g10 = u10 * blended
        g50 = u50 * blended
        g90 = u90 * blended
        n10 = g10 * STEAM_SHARE * VAT_FACTOR
        n50 = g50 * STEAM_SHARE * VAT_FACTOR
        n90 = g90 * STEAM_SHARE * VAT_FACTOR

        results.append(PriceTierResult(
            price=tier,
            units_p10=int(round(u10)),
            units_p50=int(round(u50)),
            units_p90=int(round(u90)),
            asp_factor=asp,
            blended_price=blended,
            gross_p10=g10, gross_p50=g50, gross_p90=g90,
            net_p10=n10,   net_p50=n50,   net_p90=n90,
            is_base_price=abs(tier - base_price) < 0.01,
        ))

        if n50 > best_net_p50:
            best_net_p50 = n50
            best_idx = i

    results[best_idx].revenue_maximizing = True
    return results


def build_discount_calendar(
    launch_price: float,
    quality_tier: str,
    sentiment: str,
    selected_events: list[dict],   # each: {name, start_date, end_date, discount_pct, type}
) -> DiscountCalendarResult:
    """
    Given a list of selected discount events with user-chosen depths,
    compute the blended year-1 ASP and its revenue impact.
    """
    base_asp = get_asp_factor(quality_tier, sentiment)
    events = []
    for ev in selected_events:
        start = ev["start_date"] if isinstance(ev["start_date"], date) else datetime.strptime(ev["start_date"], "%Y-%m-%d").date()
        end   = ev["end_date"]   if isinstance(ev["end_date"],   date) else datetime.strptime(ev["end_date"],   "%Y-%m-%d").date()
        events.append(DiscountEvent(
            name=ev["name"],
            start_date=start,
            end_date=end,
            discount_pct=float(ev["discount_pct"]),
            event_type=ev.get("type", "tentpole"),
        ))

    return DiscountCalendarResult(
        events=events,
        launch_price=launch_price,
        base_asp_factor=base_asp,
    )


def compute_breakeven(
    dev_cost: float,
    launch_price: float,
    quality_tier: str,
    sentiment: str,
) -> float:
    """Units needed at launch price to recover dev cost (net revenue = dev cost)."""
    asp = get_asp_factor(quality_tier, sentiment)
    net_per_unit = launch_price * asp * STEAM_SHARE * VAT_FACTOR
    if net_per_unit <= 0:
        return float("inf")
    return dev_cost / net_per_unit


def load_steam_calendar(config_path: Optional[str] = None) -> dict:
    if config_path is None:
        config_path = os.path.join(os.path.dirname(__file__), "..", "config", "steam_calendar.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_upcoming_sales(
    launch_date: date,
    calendar: dict,
    months_ahead: int = 12,
) -> list[dict]:
    """
    Return all Steam sales (seasonal + themed fests) falling within
    `months_ahead` months after `launch_date`, excluding events before
    the 30-day new release window closes.
    """
    from dateutil.relativedelta import relativedelta
    window_start = launch_date + relativedelta(days=30)   # can't discount within 30 days of launch
    window_end   = launch_date + relativedelta(months=months_ahead)

    events = []

    # Seasonal sales
    for year_str, sales in calendar.get("seasonal_sales", {}).items():
        for s in sales:
            start = datetime.strptime(s["start"], "%Y-%m-%d").date()
            end   = datetime.strptime(s["end"],   "%Y-%m-%d").date()
            if start >= window_start and start <= window_end:
                events.append({
                    "name":         s["name"],
                    "start_date":   start,
                    "end_date":     end,
                    "type":         "tentpole",
                    "discount_range": s.get("typical_discount_range", [20, 50]),
                    "notes":        s.get("notes", ""),
                })

    # Themed fests
    for s in calendar.get("themed_fests_2026", []):
        start = datetime.strptime(s["start"], "%Y-%m-%d").date()
        end   = datetime.strptime(s["end"],   "%Y-%m-%d").date()
        if start >= window_start and start <= window_end:
            events.append({
                "name":            s["name"],
                "start_date":      start,
                "end_date":        end,
                "type":            "themed",
                "discount_range":  [20, 40],
                "eligible_genres": s.get("eligible_genres", []),
                "notes":           s.get("notes", ""),
            })

    events.sort(key=lambda e: e["start_date"])
    return events
