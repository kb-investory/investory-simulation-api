"""Point-in-time stock factor evaluation used by simulation strategies."""

from typing import Dict, List, Optional, Sequence

from app.modules.simulation.rules.rule_schema import SelectionRule


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


class StockEvaluator:
    """실제 시세·재무 입력만 사용하며, 없는 팩터를 임의 상수로 채우지 않는다."""

    def __init__(self):
        self.last_screening_audit: dict = {}

    def calculate_factor_scores(
        self,
        sec_id: int,
        price_info: dict,
        sec_info: dict,
        disclosure_info: Optional[dict] = None,
    ) -> Dict[str, Optional[float]]:
        del sec_id

        per = sec_info.get("per", price_info.get("per"))
        pbr = sec_info.get("pbr", price_info.get("pbr"))
        value_parts: List[float] = []
        if per is not None and float(per) > 0:
            value_parts.append(_clamp(100.0 - (float(per) - 5.0) * 4.0))
        if pbr is not None and float(pbr) > 0:
            value_parts.append(_clamp(100.0 - (float(pbr) - 0.5) * 30.0))
        value_score = sum(value_parts) / len(value_parts) if value_parts else None

        revenue_growth = sec_info.get("revenueGrowth", price_info.get("revenueGrowth"))
        earnings_growth = sec_info.get("earningsGrowth", price_info.get("earningsGrowth"))
        growth_parts: List[float] = []
        for value in (revenue_growth, earnings_growth):
            if value is not None:
                growth_parts.append(_clamp(50.0 + float(value) * 125.0))
        growth_score = sum(growth_parts) / len(growth_parts) if growth_parts else None

        roe = sec_info.get("roe", price_info.get("roe"))
        debt_ratio = sec_info.get("debtRatio", price_info.get("debtRatio"))
        cfo_positive = sec_info.get("operatingCashFlowPositive", price_info.get("operatingCashFlowPositive"))
        quality_parts: List[float] = []
        if roe is not None:
            quality_parts.append(_clamp(50.0 + float(roe) * 200.0))
        if debt_ratio is not None:
            quality_parts.append(_clamp(100.0 - float(debt_ratio) * 50.0))
        if cfo_positive is not None:
            quality_parts.append(90.0 if bool(cfo_positive) else 20.0)
        quality_score = sum(quality_parts) / len(quality_parts) if quality_parts else None

        day5_return = price_info.get("day5Return")
        daily_return = price_info.get("changeRate", price_info.get("dailyReturnRate"))
        trend_parts: List[float] = []
        if day5_return is not None:
            trend_parts.append(_clamp(50.0 + float(day5_return) * 200.0))
        if daily_return is not None:
            trend_parts.append(_clamp(50.0 + float(daily_return) * 400.0))
        trend_score = sum(trend_parts) / len(trend_parts) if trend_parts else None

        disclosure_score = 50.0
        if disclosure_info and disclosure_info.get("impactScore") is not None:
            disclosure_score = _clamp(float(disclosure_info["impactScore"]))

        return {
            "value": round(value_score, 1) if value_score is not None else None,
            "growth": round(growth_score, 1) if growth_score is not None else None,
            "quality": round(quality_score, 1) if quality_score is not None else None,
            "trend": round(trend_score, 1) if trend_score is not None else None,
            "disclosure": round(disclosure_score, 1),
        }

    def calculate_personal_fit_score(
        self,
        factor_scores: Dict[str, Optional[float]],
        selection_rule: SelectionRule,
    ) -> float:
        selection_rule.validate()
        available = {
            name: float(score)
            for name, score in factor_scores.items()
            if score is not None and selection_rule.factor_weights.get(name, 0.0) > 0
        }
        weight_sum = sum(selection_rule.factor_weights[name] for name in available)
        if weight_sum <= 0:
            return 0.0
        total = sum(
            score * selection_rule.factor_weights[name] / weight_sum
            for name, score in available.items()
        )
        return round(total, 1)

    @staticmethod
    def _universe_rejection_reasons(price_info: dict, sec_info: dict, rule: dict) -> List[str]:
        reasons = []
        if sec_info.get("marketType") not in set(rule.get("allowed_markets", ["KOSPI", "KOSDAQ"])):
            reasons.append("MARKET_NOT_ALLOWED")
        if not sec_info.get("isActive", True):
            reasons.append("SECURITY_INACTIVE")
        if rule.get("exclude_halted", True) and (sec_info.get("isHalted") or price_info.get("isHalted")):
            reasons.append("SECURITY_HALTED")
        if rule.get("exclude_administrative", True) and sec_info.get("isAdministrative"):
            reasons.append("ADMINISTRATIVE_SECURITY")
        market_cap = sec_info.get("marketCap", price_info.get("marketCap"))
        if market_cap is None:
            reasons.append("MARKET_CAP_MISSING")
        elif float(market_cap) < float(rule.get("min_market_cap", 0.0)):
            reasons.append("MIN_MARKET_CAP")
        if float(price_info.get("tradingValue", 0.0)) < float(rule.get("min_daily_trading_value", 0.0)):
            reasons.append("MIN_TRADING_VALUE")
        return reasons

    @classmethod
    def _passes_universe(cls, price_info: dict, sec_info: dict, rule: dict) -> bool:
        return not cls._universe_rejection_reasons(price_info, sec_info, rule)

    @staticmethod
    def _entry_rejection_reasons(price_info: dict, disclosure_info: Optional[dict], rule: dict) -> List[str]:
        reasons = []
        day5_return = price_info.get("day5Return")
        if day5_return is None:
            reasons.append("DAY5_RETURN_MISSING")
        elif float(day5_return) > float(rule.get("max_5day_return", 1.0)):
            reasons.append("MAX_5DAY_RETURN")
        if rule.get("require_positive_disclosure"):
            if not disclosure_info or disclosure_info.get("direction") != "POSITIVE":
                reasons.append("POSITIVE_DISCLOSURE_REQUIRED")
        condition = str(rule.get("moving_average_condition", "NONE")).upper()
        close = float(price_info.get("closePrice", 0.0))
        ma5 = price_info.get("movingAverage5")
        ma20 = price_info.get("movingAverage20")
        if condition == "ABOVE_MA5" and (ma5 is None or close <= float(ma5)):
            reasons.append("ABOVE_MA5_REQUIRED")
        if condition == "ABOVE_MA20" and (ma20 is None or close <= float(ma20)):
            reasons.append("ABOVE_MA20_REQUIRED")
        if condition == "MA5_ABOVE_MA20" and (ma5 is None or ma20 is None or float(ma5) <= float(ma20)):
            reasons.append("MA5_ABOVE_MA20_REQUIRED")
        return reasons

    @classmethod
    def _passes_entry(cls, price_info: dict, disclosure_info: Optional[dict], rule: dict) -> bool:
        return not cls._entry_rejection_reasons(price_info, disclosure_info, rule)

    def screen_candidates(
        self,
        prices_map: Dict[int, dict],
        securities_map: Dict[int, dict],
        selection_rule: SelectionRule,
        disclosures_today: Optional[Dict[int, dict]] = None,
        universe_rule: Optional[dict] = None,
        entry_rule: Optional[dict] = None,
        required_factors: Sequence[str] = (),
    ) -> List[dict]:
        candidates = []
        rejected = []
        rejected_by_reason: Dict[str, int] = {}
        disclosures_today = disclosures_today or {}

        for sec_id, price_info in prices_map.items():
            sec_info = securities_map.get(sec_id, {})
            disclosure = disclosures_today.get(sec_id)
            scores = self.calculate_factor_scores(sec_id, price_info, sec_info, disclosure)
            fit_score = self.calculate_personal_fit_score(scores, selection_rule)
            reason_codes = []
            if universe_rule:
                reason_codes.extend(self._universe_rejection_reasons(price_info, sec_info, universe_rule))
            if entry_rule:
                reason_codes.extend(self._entry_rejection_reasons(price_info, disclosure, entry_rule))
            missing_required = [name for name in required_factors if scores.get(name) is None]
            if missing_required:
                reason_codes.append("REQUIRED_FACTOR_MISSING")
            if fit_score < selection_rule.min_passing_score:
                reason_codes.append("MIN_FACTOR_SCORE")
            reason_codes = list(dict.fromkeys(reason_codes))
            if reason_codes:
                for reason in reason_codes:
                    rejected_by_reason[reason] = rejected_by_reason.get(reason, 0) + 1
                rejected.append({
                    "securityId": sec_id,
                    "securityCode": sec_info.get("securityCode", ""),
                    "securityName": sec_info.get("securityName", ""),
                    "personalFitScore": fit_score,
                    "reasonCodes": reason_codes,
                    "missingRequiredFactors": missing_required,
                })
                continue
            available_count = sum(score is not None for score in scores.values())
            candidates.append({
                "securityId": sec_id,
                "securityCode": sec_info.get("securityCode", ""),
                "securityName": sec_info.get("securityName", ""),
                "personalFitScore": fit_score,
                "factorScores": scores,
                "dataCompletenessPercent": round(available_count / len(scores) * 100, 1),
                "missingFactors": [name for name, score in scores.items() if score is None],
                "priceInfo": price_info,
                "disclosureInfo": disclosure,
            })

        candidates.sort(key=lambda item: (item["personalFitScore"], item["dataCompletenessPercent"]), reverse=True)
        rejected.sort(key=lambda item: item["personalFitScore"], reverse=True)
        self.last_screening_audit = {
            "evaluatedCount": len(prices_map),
            "passedCount": len(candidates),
            "rejectedCount": len(rejected),
            "rejectedByReason": dict(sorted(rejected_by_reason.items())),
            "notableRejectedCandidates": rejected[:10],
            "notableCandidateLimit": 10,
            "reasonCountingPolicy": "MULTI_REASON_PER_SECURITY",
        }
        return candidates
