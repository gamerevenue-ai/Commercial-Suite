# src/estimator.py
# Triangulated sales estimation engine.
#
# Estimation methods (in priority order):
#   1. Review-based (primary)     — total_reviews × genre+quality multiplier ± adjustments
#   2. Review velocity (primary)  — monthly review histogram → unit distribution shape
#   3. SteamCharts CCU (secondary) — optional cross-check via historical CCU trend
#   4. CCU snapshot (sanity)      — current CCU as a rough floor/ceiling check
#
# All outputs are labeled with:
#   method     — which estimation method produced the number
#   confidence — high / medium / low
#   source     — observed / derived / heuristic / user
#
# Outputs are always ranges (low / mid / high), never point estimates presented as facts.

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ASSUMPTIONS_PATH = "config/assumptions.yaml"


def _load_assumptions() -> dict:
    with open(_ASSUMPTIONS_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class UnitEstimate:
    """A single sales estimate from one method."""
    method: str               # e.g. "review_proxy", "ccu_crosscheck"
    label: str                # human-readable method name
    units_low: float
    units_mid: float
    units_high: float
    confidence: str           # "high" / "medium" / "low"
    source: str               # "observed" / "derived" / "heuristic" / "user"
    notes: list[str] = field(default_factory=list)

    @property
    def revenue_mid(self) -> float:
        """Convenience — caller must multiply by price."""
        return self.units_mid  # price applied externally

    def as_dict(self) -> dict:
        return {
            "method":      self.method,
            "label":       self.label,
            "units_low":   round(self.units_low),
            "units_mid":   round(self.units_mid),
            "units_high":  round(self.units_high),
            "confidence":  self.confidence,
            "source":      self.source,
            "notes":       self.notes,
        }


@dataclass
class TriangulatedEstimate:
    """
    The consolidated output of all estimation methods for one game.
    The primary estimate is always the review-based one.
    Other methods are provided as cross-checks only.
    """
    appid: int
    name: str
    review_estimate: UnitEstimate | None
    ccu_estimate: UnitEstimate | None
    primary: UnitEstimate | None        # always = review_estimate when available
    adjustments_applied: list[str]      # list of adjustment notes for UI
    total_reviews: int
    sentiment_ratio: float
    current_price_usd: float | None
    age_months: float | None
    genre: str
    quality_tier: str
    effective_multiplier: float         # the final Boxleiter number used
    active_flags: list[str] = field(default_factory=list)  # e.g. ["game_pass", "short_game"]

    def as_dict(self) -> dict:
        return {
            "appid":               self.appid,
            "name":                self.name,
            "total_reviews":       self.total_reviews,
            "sentiment_ratio":     round(self.sentiment_ratio, 3),
            "current_price_usd":   self.current_price_usd,
            "age_months":          round(self.age_months, 1) if self.age_months else None,
            "genre":               self.genre,
            "quality_tier":        self.quality_tier,
            "effective_multiplier": round(self.effective_multiplier, 1),
            "review_estimate":     self.review_estimate.as_dict() if self.review_estimate else None,
            "ccu_estimate":        self.ccu_estimate.as_dict() if self.ccu_estimate else None,
            "primary":             self.primary.as_dict() if self.primary else None,
            "adjustments_applied": self.adjustments_applied,
        }


# ---------------------------------------------------------------------------
# Estimator
# ---------------------------------------------------------------------------

class Estimator:
    """
    Produces triangulated unit sales estimates for a Steam game.

    Usage:
        est = Estimator()
        result = est.estimate(
            parsed_details=...,   # from steam_client.parse_app_details()
            parsed_reviews=...,   # from steam_client.parse_review_summary()
            current_ccu=...,      # from steam_client.get_current_ccu()
            genre="Action RPG",
            quality_tier="indie",
        )
    """

    def __init__(self, assumptions: dict | None = None):
        self._a = assumptions or _load_assumptions()

    def reload_assumptions(self) -> None:
        self._a = _load_assumptions()

    # -----------------------------------------------------------------------
    # Top-level entry point
    # -----------------------------------------------------------------------

    def estimate(
        self,
        parsed_details: dict,
        parsed_reviews: dict,
        current_ccu: int = 0,
        genre: str = "Other",
        quality_tier: str = "indie",
        steamcharts_history: list[dict] | None = None,
        # Distortion flags (user-supplied — cannot be auto-detected from API)
        is_game_pass: bool = False,
        is_short_game: bool = False,     # playtime typically < 10 hours
        is_early_access: bool = False,
    ) -> TriangulatedEstimate:
        """
        Run all estimation methods and return a TriangulatedEstimate.

        Parameters mirror the outputs of steam_client helper functions.
        genre and quality_tier are user-supplied inputs (not inferred).
        Distortion flags must be supplied by the caller — cannot be auto-detected.
        """
        total_reviews   = parsed_reviews.get("total_reviews", 0)
        sentiment_ratio = parsed_reviews.get("sentiment_ratio", 0.0)
        current_price   = parsed_details.get("current_price_usd")
        age_months      = parsed_details.get("age_months")
        metacritic      = parsed_details.get("metacritic_score")
        lang_count      = parsed_details.get("language_count", 0)
        dlc_count       = parsed_details.get("dlc_count", 0)
        is_multiplayer  = parsed_details.get("is_multiplayer", False)
        name            = parsed_details.get("name", "Unknown")
        appid           = parsed_details.get("appid", 0)

        adjustments: list[str] = []
        active_flags: list[str] = []

        # -- Distortion flag processing --------------------------------------
        dflag_cfg = self._a.get("distortion_flags", {})
        band_multiplier = 1.0  # how much to widen uncertainty bands

        if is_game_pass:
            cfg = dflag_cfg.get("game_pass", {})
            band_multiplier *= cfg.get("confidence_band_multiplier", 1.5)
            active_flags.append("game_pass")
            adjustments.append(
                "[observed] Game Pass day-1 flag active. "
                "Steam reviews represent Steam buyers only, not total players. "
                "Uncertainty bands widened. Steam revenue != total revenue."
            )
        if is_short_game:
            cfg = dflag_cfg.get("short_game", {})
            band_multiplier *= cfg.get("confidence_band_multiplier", 1.3)
            active_flags.append("short_game")
            adjustments.append(
                "[heuristic] Short game flag active (<10hrs). "
                "Elevated refund rate may suppress reviews. "
                "Slight upward unit adjustment applied; ASP penalized."
            )
        if is_early_access:
            cfg = dflag_cfg.get("early_access", {})
            band_multiplier *= cfg.get("confidence_band_multiplier", 1.6)
            active_flags.append("early_access")
            adjustments.append(
                "[heuristic] Early Access flag active. "
                "Review behavior during EA differs from 1.0 release. "
                "Estimate reliability is reduced."
            )
        if age_months and age_months < 12:
            cfg = dflag_cfg.get("young_title", {})
            band_multiplier *= cfg.get("confidence_band_multiplier", 1.4)
            active_flags.append("young_title")
            adjustments.append(
                f"[observed] Young title flag: {age_months:.1f} months old. "
                "Unit estimate reflects sales-to-date, NOT projected year-1 total. "
                "Review accumulation is ongoing."
            )

        # -- Review-based estimate (primary) ---------------------------------
        review_est, effective_mult = self._review_estimate(
            total_reviews=total_reviews,
            sentiment_ratio=sentiment_ratio,
            metacritic_score=metacritic,
            language_count=lang_count,
            dlc_count=dlc_count,
            genre=genre,
            quality_tier=quality_tier,
            adjustments=adjustments,
            is_game_pass=is_game_pass,
            is_short_game=is_short_game,
            is_early_access=is_early_access,
            band_multiplier=band_multiplier,
        )

        # -- CCU snapshot cross-check ----------------------------------------
        ccu_est = self._ccu_snapshot_estimate(
            current_ccu=current_ccu,
            is_multiplayer=is_multiplayer,
        ) if current_ccu > 0 else None

        # -- SteamCharts cross-check (if available) --------------------------
        if steamcharts_history:
            peak_ccu = max((e.get("avg_players", 0) for e in steamcharts_history), default=0)
            if peak_ccu > 0:
                adjustments.append(
                    f"[third-party scrape / heuristic] SteamCharts peak avg CCU: "
                    f"{peak_ccu:,.0f} — cross-check only, low confidence."
                )

        return TriangulatedEstimate(
            appid=appid,
            name=name,
            review_estimate=review_est,
            ccu_estimate=ccu_est,
            primary=review_est,
            adjustments_applied=adjustments,
            total_reviews=total_reviews,
            sentiment_ratio=sentiment_ratio,
            current_price_usd=current_price,
            age_months=age_months,
            genre=genre,
            quality_tier=quality_tier,
            effective_multiplier=effective_mult,
            active_flags=active_flags,
        )

    # -----------------------------------------------------------------------
    # Review-based estimation
    # -----------------------------------------------------------------------

    def _review_estimate(
        self,
        total_reviews: int,
        sentiment_ratio: float,
        metacritic_score: int | None,
        language_count: int,
        dlc_count: int,
        genre: str,
        quality_tier: str,
        adjustments: list[str],
        is_game_pass: bool = False,
        is_short_game: bool = False,
        is_early_access: bool = False,
        band_multiplier: float = 1.0,
    ) -> tuple[UnitEstimate | None, float]:
        """
        Estimate total lifetime units via review proxy (Boxleiter method).

        CALIBRATION FIXES APPLIED (2026-03-31):
          1. Compound adjustment cap: combined adjustments capped at ±25% of base.
          2. Distortion flag adjustments: short game / EA apply unit multipliers.
          3. Band multiplier: uncertainty range widened by active distortion flags.

        Returns (UnitEstimate, effective_multiplier).
        Source: [heuristic] — all multipliers from assumptions.yaml.
        """
        if total_reviews == 0:
            adjustments.append(
                "[observed] No reviews found — review-based estimate unavailable."
            )
            return None, 0.0

        a = self._a

        # Base multiplier from quality tier
        tier_cfg = a["review_multipliers"]["quality_tiers"].get(
            quality_tier,
            a["review_multipliers"]["quality_tiers"]["indie"],
        )
        base_mult = tier_cfg["base"]
        range_low, range_high = tier_cfg["range"]

        adjustments.append(
            f"[heuristic] Quality tier '{quality_tier}': base multiplier {base_mult}x "
            f"(range {range_low}x-{range_high}x)."
        )

        # Genre adjustment
        genre_adj = a["review_multipliers"]["genre_adjustments"].get(genre, 1.0)
        if genre_adj != 1.0:
            adjustments.append(
                f"[heuristic] Genre '{genre}': adjustment {genre_adj:.2f}x "
                f"({'higher' if genre_adj > 1 else 'lower'} review propensity)."
            )

        # Sentiment adjustment
        sentiment_adj = self._sentiment_adj(sentiment_ratio, adjustments)

        # Metacritic adjustment
        meta_adj = self._metacritic_adj(metacritic_score, adjustments)

        # Language adjustment
        lang_adj = self._language_adj(language_count, adjustments)

        # DLC adjustment
        dlc_adj = self._dlc_adj(dlc_count, adjustments)

        # CALIBRATION FIX 1: Cap compound adjustment at ±25% of base
        # Prevents stacking genre + sentiment + language + DLC into unrealistic territory
        raw_compound = genre_adj * sentiment_adj * meta_adj * lang_adj * dlc_adj
        cap_cfg = a.get("multiplier_compound_cap", {})
        if cap_cfg.get("enabled", True):
            max_boost = cap_cfg.get("max_boost_factor", 1.25)
            max_penalty = cap_cfg.get("max_penalty_factor", 0.75)
            if raw_compound > max_boost:
                adjustments.append(
                    f"[heuristic] Compound adjustment {raw_compound:.3f}x capped at {max_boost:.2f}x "
                    f"(calibration fix — prevents multiplicative stacking bias)."
                )
                raw_compound = max_boost
            elif raw_compound < max_penalty:
                adjustments.append(
                    f"[heuristic] Compound adjustment {raw_compound:.3f}x floored at {max_penalty:.2f}x."
                )
                raw_compound = max_penalty

        effective_mult = base_mult * raw_compound

        # CALIBRATION FIX 2: Distortion flag unit adjustments
        dflag_cfg = a.get("distortion_flags", {})
        if is_short_game:
            short_adj = dflag_cfg.get("short_game", {}).get("unit_multiplier_adjustment", 1.10)
            effective_mult *= short_adj
            adjustments.append(
                f"[heuristic] Short game unit adjustment: {short_adj:.2f}x "
                "(more units per review — refunds suppress review count)."
            )
        if is_early_access:
            ea_adj = dflag_cfg.get("early_access", {}).get("unit_multiplier_adjustment", 0.90)
            effective_mult *= ea_adj
            adjustments.append(
                f"[heuristic] Early Access unit adjustment: {ea_adj:.2f}x."
            )

        # CALIBRATION FIX 5: Game Pass Steam unit suppression (2026-04-01)
        # Game Pass day-1 titles generate far fewer Steam purchases than equivalent
        # non-GP titles. GP subscribers play for free — only a small fraction (~25%)
        # also buy on Steam for ownership, achievements, mods, etc.
        # Calibrated from user benchmark data:
        #   Avowed (GP):          implied 17.0x vs pre-fix 74.6x => 0.228x factor
        #   South of Midnight (GP): implied 23.1x vs pre-fix 75.0x => 0.307x factor
        #   Average => 0.27x applied as unit multiplier adjustment.
        # NOTE: This adjusts Steam-only unit estimate. Total franchise revenue
        # (GP royalties + Xbox + Steam) is not captured in this model.
        if is_game_pass:
            dflag_cfg_inner = self._a.get("distortion_flags", {})
            gp_unit_adj = dflag_cfg_inner.get("game_pass", {}).get("unit_multiplier_adjustment", 0.27)
            if gp_unit_adj != 1.0:
                pre_gp = effective_mult
                effective_mult *= gp_unit_adj
                adjustments.append(
                    f"[heuristic] Game Pass Steam suppression: {gp_unit_adj:.2f}x applied "
                    f"(multiplier {pre_gp:.1f}x => {effective_mult:.1f}x). "
                    "GP day-1 titles sell ~25% of Steam units vs equivalent non-GP release. "
                    "Calibrated 2026-04-01 from user benchmark data."
                )

        # CALIBRATION FIX 4: Volume-scaled multiplier decay
        # At high review counts the real-world review-to-unit ratio falls because
        # mainstream/casual buyers (who don't review) make up a growing fraction
        # of the owner base. Effect is tier-specific: strongest for AAA, mild for indie.
        volume_decay = self._volume_decay(total_reviews, quality_tier)
        if volume_decay != 1.0:
            pre_decay = effective_mult
            effective_mult *= volume_decay
            adjustments.append(
                f"[heuristic] Volume decay {volume_decay:.2f}x applied "
                f"({total_reviews:,} reviews, {quality_tier} tier): "
                f"multiplier {pre_decay:.1f}x => {effective_mult:.1f}x. "
                "High-review titles have larger casual buyer pools with lower review propensity."
            )

        # Unit estimates
        units_mid  = total_reviews * effective_mult
        adj_factor = effective_mult / base_mult

        # CALIBRATION FIX 3: Widen bands by band_multiplier (from active distortion flags)
        # band_multiplier > 1.0 = higher uncertainty. Applied symmetrically around mid.
        base_range_half = (range_high - range_low) / 2.0
        widened_low  = base_mult * adj_factor - base_range_half * band_multiplier
        widened_high = base_mult * adj_factor + base_range_half * band_multiplier
        # Ensure bounds don't invert and stay positive
        widened_low  = max(widened_low, base_mult * adj_factor * 0.30)
        widened_high = max(widened_high, effective_mult * 1.05)

        units_low  = total_reviews * widened_low
        units_high = total_reviews * widened_high

        adjustments.append(
            f"[derived] Effective Boxleiter multiplier: {effective_mult:.1f}x "
            f"=> estimated total units: {units_low:,.0f}-{units_high:,.0f} "
            f"(mid: {units_mid:,.0f}). Band multiplier: {band_multiplier:.2f}x."
        )

        confidence = "medium"
        if band_multiplier > 1.8:
            confidence = "low"
        elif band_multiplier < 1.1:
            confidence = "medium"

        return UnitEstimate(
            method="review_proxy",
            label="Review Proxy (Boxleiter method)",
            units_low=max(units_low, 0),
            units_mid=units_mid,
            units_high=units_high,
            confidence=confidence,
            source="heuristic",
            notes=[
                "Multiplier calibrated by genre, quality tier, sentiment, Metacritic, "
                "language support, and DLC presence.",
                f"Compound adjustments capped at +/-25% of base (calibrated 2026-03-31).",
                "Range reflects variance in the Boxleiter multiplier for this tier, "
                "widened by any active distortion flags.",
                "Treat as directional — actual sales may vary significantly.",
            ],
        ), effective_mult

    # -----------------------------------------------------------------------
    # Volume-scaled multiplier decay
    # -----------------------------------------------------------------------

    def _volume_decay(self, total_reviews: int, quality_tier: str) -> float:
        """
        Return a volume decay factor [0.20, 1.0] based on total review count and tier.

        CALIBRATION FIX 4 (2026-03-31): Flat-tier Boxleiter multipliers dramatically
        overestimate games with high review counts. At scale, mainstream/casual buyers
        make up a growing fraction of the owner base — and those buyers don't review.

        Benchmark calibration:
          - Black Myth (1.2M reviews, AAA): model 75x, actual 16.7x => needs 0.22x decay
          - Helldivers 2 (1.05M, AA):       model 33.8x, actual 11.4x => needs 0.34x decay
          - BG3 (825K, AAA):                model 75x, actual 22.8x  => needs 0.30x decay
          - Lethal Company (503K, indie):   model 22.5x, actual 35.8x => NO decay (viral indie)

        Decay is tier-specific: strongest for AAA, mild for indie (who spread via enthusiast channels).
        Source: [heuristic] — calibrated 2026-03-31.
        """
        vsd_cfg = self._a.get("volume_scaled_multiplier", {})
        if not vsd_cfg.get("enabled", True):
            return 1.0

        tier_cfgs = vsd_cfg.get("by_tier", {})
        tier_entry = tier_cfgs.get(quality_tier, tier_cfgs.get("indie", {}))
        thresholds = tier_entry.get("thresholds", [])

        if not thresholds:
            return 1.0

        for threshold in thresholds:
            if total_reviews <= threshold["max_reviews"]:
                return float(threshold["decay_factor"])

        # Fallback: use the last (most aggressive) decay factor
        return float(thresholds[-1]["decay_factor"])

    # -----------------------------------------------------------------------
    # CCU snapshot cross-check
    # -----------------------------------------------------------------------

    def _ccu_snapshot_estimate(
        self,
        current_ccu: int,
        is_multiplayer: bool,
    ) -> UnitEstimate:
        """
        Rough implied owner count from current CCU.
        Source: [heuristic] — low confidence, sanity check only.
        """
        a = self._a["ccu_crosscheck"]
        ratio = (
            a["multiplayer_ccu_to_owners"]
            if is_multiplayer
            else a["single_player_ccu_to_owners"]
        )
        implied_owners = current_ccu / ratio

        # Wide uncertainty band: ±60% around the implied value
        return UnitEstimate(
            method="ccu_crosscheck",
            label="CCU Snapshot Cross-check",
            units_low=implied_owners * 0.40,
            units_mid=implied_owners,
            units_high=implied_owners * 2.50,
            confidence="low",
            source="heuristic",
            notes=[
                f"Current CCU: {current_ccu:,}. Ratio used: 1 CCU per "
                f"{1/ratio:,.0f} owners ({'multiplayer' if is_multiplayer else 'single-player'}).",
                "CCU collapses rapidly for single-player games after launch. "
                "Snapshot is not representative of owner count.",
                "Wide range reflects genuine uncertainty. Use as sanity check only.",
            ],
        )

    # -----------------------------------------------------------------------
    # Revenue helpers
    # -----------------------------------------------------------------------

    def estimate_revenue(
        self,
        unit_estimate: UnitEstimate,
        price_usd: float,
        quality_tier: str = "indie",
        sentiment_ratio: float = 0.80,
        age_months: float | None = None,
        include_platform_cut: bool = True,
        include_vat_discount: bool = True,
        include_asp_blending: bool = True,
    ) -> dict:
        """
        Convert a UnitEstimate into gross and net revenue figures.

        CALIBRATION FIX (2026-03-31): Added ASP year-1 blended factor.
        Previous version used full MSRP × units, overstating revenue by 22-108%.
        Real year-1 ASP is typically 60-80% of MSRP due to:
          - Multiple discount events throughout the year
          - Regional pricing (non-US buyers pay less than US MSRP)
          - Refunds reducing net realized revenue

        Returns dict with: gross_low, gross_mid, gross_high,
                           net_low, net_mid, net_high (developer-received),
                           blended_asp, asp_factor (applied fraction of MSRP)
        Source: [derived] from unit estimate + [heuristic] ASP blending.
        """
        a_platform = self._a["platform"]
        platform_share = a_platform["steam_revenue_share"] if include_platform_cut else 1.0
        vat_factor = a_platform["vat_assumption_global"] if include_vat_discount else 1.0
        net_factor = platform_share * vat_factor

        # -- ASP blending (CALIBRATION FIX) ------------------------------------
        asp_factor = 1.0
        asp_notes: list[str] = []

        if include_asp_blending:
            asp_factor = self._compute_asp_factor(
                quality_tier=quality_tier,
                sentiment_ratio=sentiment_ratio,
                age_months=age_months,
                notes=asp_notes,
            )
        else:
            asp_notes.append(
                "ASP blending disabled — using full MSRP. "
                "WARNING: this overstates revenue vs. realistic year-1 performance."
            )

        blended_asp = price_usd * asp_factor

        def _rev(units: float) -> float:
            return units * blended_asp

        def _net(gross: float) -> float:
            return gross * net_factor

        gross_low  = _rev(unit_estimate.units_low)
        gross_mid  = _rev(unit_estimate.units_mid)
        gross_high = _rev(unit_estimate.units_high)

        return {
            "price_usd":       price_usd,
            "blended_asp":     round(blended_asp, 2),
            "asp_factor":      round(asp_factor, 3),
            "gross_low":       gross_low,
            "gross_mid":       gross_mid,
            "gross_high":      gross_high,
            "net_low":         _net(gross_low),
            "net_mid":         _net(gross_mid),
            "net_high":        _net(gross_high),
            "platform_share":  platform_share,
            "vat_factor":      vat_factor,
            "source":          "derived",
            "notes": [
                f"[heuristic] Blended ASP: ${blended_asp:.2f} "
                f"({asp_factor:.0%} of ${price_usd:.2f} MSRP). "
                "Accounts for discount events, regional pricing, and refunds.",
            ] + asp_notes + [
                f"[observed] Net = gross x {platform_share:.0%} (Steam rev share) "
                f"x {vat_factor:.0%} (blended VAT estimate).",
                "CALIBRATION NOTE: Prior to 2026-03-31, this model used full MSRP "
                "which overstated revenue by 22-108% on benchmark comp set. "
                "ASP blending was added to correct this systematic error.",
            ],
        }

    def _compute_asp_factor(
        self,
        quality_tier: str,
        sentiment_ratio: float,
        age_months: float | None,
        notes: list[str],
    ) -> float:
        """
        Compute the year-1 blended ASP factor for a given title context.
        Returns a float in [0.40, 1.0] representing fraction of MSRP realized.
        Source: [heuristic] — calibrated from benchmark comp set (2026-03-31).
        """
        asp_cfg = self._a.get("asp_year1_blended", {})
        tier_cfgs = asp_cfg.get("by_tier", {})

        # Sentiment quality: good >= 70% positive
        sentiment_quality = "sentiment_good" if sentiment_ratio >= 0.70 else "sentiment_mixed"

        tier_entry = tier_cfgs.get(quality_tier, tier_cfgs.get("indie", {}))
        base_asp = tier_entry.get(sentiment_quality, 0.68)

        notes.append(
            f"[heuristic] Base ASP factor: {base_asp:.0%} "
            f"({quality_tier} tier, {sentiment_quality.replace('_', ' ')})."
        )

        # Age adjustment
        age_cfg = asp_cfg.get("age_adjustment", {})
        age_adj = 0.0
        if age_months is not None:
            young_threshold = age_cfg.get("young_threshold_months", 6)
            old_threshold   = age_cfg.get("mature_threshold_months", 18)
            if age_months < young_threshold:
                age_adj = age_cfg.get("young_boost", 0.08)
                notes.append(
                    f"[heuristic] Young title ({age_months:.0f}mo): "
                    f"+{age_adj:.0%} ASP boost (less discounting to date)."
                )
            elif age_months > old_threshold:
                age_adj = age_cfg.get("old_penalty", -0.05)
                notes.append(
                    f"[heuristic] Mature title ({age_months:.0f}mo): "
                    f"{age_adj:.0%} ASP penalty (heavier discount history)."
                )

        # Sentiment floor for very low sentiment
        floor_cfg = asp_cfg.get("sentiment_asp_floor", {})
        floor_threshold = floor_cfg.get("mostly_negative_threshold", 0.55)
        floor_value = floor_cfg.get("floor_factor", 0.45)

        asp = base_asp + age_adj
        if sentiment_ratio < floor_threshold:
            asp = min(asp, floor_value)
            notes.append(
                f"[heuristic] Low sentiment ({sentiment_ratio:.0%}) — "
                f"ASP floored at {floor_value:.0%}."
            )

        # Final clamp
        asp = max(0.40, min(1.0, asp))
        return asp

    # -----------------------------------------------------------------------
    # Adjustment sub-calculators
    # -----------------------------------------------------------------------

    def _sentiment_adj(self, ratio: float, adjustments: list[str]) -> float:
        a = self._a["sentiment_adjustment"]
        for key in [
            "overwhelmingly_positive", "very_positive", "mostly_positive",
            "mixed", "mostly_negative"
        ]:
            tier = a[key]
            if ratio >= tier["threshold"]:
                adj = tier["multiplier_adj"]
                if adj != 1.0:
                    adjustments.append(
                        f"[heuristic] Sentiment '{tier['label']}' "
                        f"({ratio:.0%} positive): {adj:.2f}x adjustment."
                    )
                return adj
        return 1.0

    def _metacritic_adj(self, score: int | None, adjustments: list[str]) -> float:
        a = self._a["metacritic_adjustment"]
        if not a.get("enabled") or score is None:
            return 1.0
        for tier in a["tiers"]:
            if score >= tier["min_score"]:
                adj = tier["multiplier_adj"]
                if adj != 1.0:
                    adjustments.append(
                        f"[observed] Metacritic {score}: {adj:.2f}x adjustment."
                    )
                return adj
        return 1.0

    def _language_adj(self, count: int, adjustments: list[str]) -> float:
        a = self._a["language_adjustment"]
        if not a.get("enabled"):
            return 1.0
        for tier in a["tiers"]:
            if count >= tier["min_languages"]:
                adj = tier["multiplier_adj"]
                if adj != 1.0:
                    adjustments.append(
                        f"[derived] {count} supported languages: {adj:.2f}x reach adjustment."
                    )
                return adj
        return 1.0

    def _dlc_adj(self, count: int, adjustments: list[str]) -> float:
        a = self._a["dlc_adjustment"]
        if not a.get("enabled"):
            return 1.0
        for tier in a["tiers"]:
            if count >= tier["min_dlc_count"]:
                adj = tier["multiplier_adj"]
                if adj != 1.0:
                    adjustments.append(
                        f"[observed] {count} DLC items: {adj:.2f}x adjustment."
                    )
                return adj
        return 1.0


# ---------------------------------------------------------------------------
# Convenience: build a quick comp-set summary table
# ---------------------------------------------------------------------------

def build_comp_summary(
    estimates: list[TriangulatedEstimate],
    price_usd_override: float | None = None,
    estimator: "Estimator | None" = None,
) -> list[dict]:
    """
    Given a list of TriangulatedEstimate objects, return a list of flat dicts
    suitable for a Pandas DataFrame / display table.

    CALIBRATION FIX: Revenue now uses ASP blending, not full MSRP.
    Requires an Estimator instance to compute; falls back to MSRP if not provided.
    """
    rows = []
    for est in estimates:
        primary = est.primary
        price = price_usd_override or est.current_price_usd or 0.0

        # Corrected revenue (ASP-blended)
        if primary and price and estimator:
            rev_data = estimator.estimate_revenue(
                primary,
                price_usd=price,
                quality_tier=est.quality_tier,
                sentiment_ratio=est.sentiment_ratio,
                age_months=est.age_months,
            )
            gross_mid = rev_data["gross_mid"]
            asp = rev_data["blended_asp"]
            asp_pct = rev_data["asp_factor"]
        elif primary and price:
            gross_mid = primary.units_mid * price  # fallback: full MSRP
            asp = price
            asp_pct = 1.0
        else:
            gross_mid = 0
            asp = price
            asp_pct = 1.0

        flags = est.active_flags if hasattr(est, "active_flags") else []
        flag_str = ", ".join(flags).upper() if flags else "—"

        rows.append({
            "App ID":               est.appid,
            "Title":                est.name,
            "Genre":                est.genre,
            "Tier":                 est.quality_tier.title(),
            "Price (USD)":          f"${price:.2f}" if price else "N/A",
            "Age (months)":         round(est.age_months, 0) if est.age_months else "N/A",
            "Reviews":              f"{est.total_reviews:,}",
            "Sentiment":            f"{est.sentiment_ratio:.0%}",
            "Multiplier":           f"{est.effective_multiplier:.1f}x",
            "Est. Units (low)":     f"{primary.units_low:,.0f}" if primary else "N/A",
            "Est. Units (mid)":     f"{primary.units_mid:,.0f}" if primary else "N/A",
            "Est. Units (high)":    f"{primary.units_high:,.0f}" if primary else "N/A",
            "Blended ASP":          f"${asp:.2f} ({asp_pct:.0%})" if price else "N/A",
            "Est. Gross Rev (mid)": f"${gross_mid:,.0f}" if price else "N/A",
            "Confidence":           primary.confidence.title() if primary else "N/A",
            "Flags":                flag_str,
        })
    return rows
