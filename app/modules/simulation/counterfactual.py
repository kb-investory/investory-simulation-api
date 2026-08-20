"""Per-principle counterfactual backtests.

The report can already say a principle was violated three times and that those
trades did badly. It could not say what that cost the account, because the only
counterfactual available was the whole personal bot, which differs from the user
in every rule at once.

This module isolates one principle at a time: replay the user's own trades with
only the orders that violated that principle removed, and diff the result
against the real run. Everything else -- prices, costs, slippage, starting
holdings -- is identical, so the difference is attributable to the one principle.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.modules.simulation.backtest import BacktestEngine
from app.modules.simulation.models import Position
from app.modules.simulation.strategies import ActualUserStrategy

logger = logging.getLogger(__name__)

COUNTERFACTUAL_VARIANT_ID = 1
# Removing a buy the user should not have made is a faithful counterfactual.
# "Should have sold but did not" would require inventing an order the user never
# placed, at a size and price the engine would have to guess, so it is refused.
REMOVABLE_ACTIONS = {"BUY", "ADD"}
# Each principle costs one extra day-loop. Cap the work so a user with many
# principles cannot stretch the request unboundedly.
MAX_COUNTERFACTUAL_PRINCIPLES = 8

DISCLAIMER = (
    "과거 가격에 사용자의 실제 매매만 다시 적용한 결과이며, 해당 원칙을 지켰을 때의 "
    "미래 수익을 보장하지 않습니다."
)


def unsupported(reason_code: str, message: str) -> dict:
    return {
        "supported": False,
        "reasonCode": reason_code,
        "reason": message,
        "method": None,
    }


def _raw_trade_key(trade: dict) -> tuple:
    """Key a source trade the way the executed trade remembers its origin."""
    return (
        int(trade.get("securityId") or trade.get("security_id") or 0),
        str(trade.get("tradeSide") or trade.get("trade_side") or ""),
        str(trade.get("tradedAt") or trade.get("traded_at") or ""),
    )


def _executed_trade_key(trade: dict) -> tuple:
    return (
        int(trade.get("securityId") or trade.get("security_id") or 0),
        str(trade.get("tradeSide") or trade.get("trade_side") or ""),
        str(trade.get("originalTradedAt") or trade.get("original_traded_at") or ""),
    )


def _origin_keys_by_report_trade_id(simulated_trades: List[dict]) -> Dict[object, tuple]:
    """Map a report tradeId back to the source trade it was replayed from.

    The engine renumbers executed trades with its own counter, so the report's
    tradeId is not the database trade id. The executed trade does keep the
    original timestamp, which together with the security and side identifies the
    source row.
    """
    mapping = {}
    for trade in simulated_trades:
        variant_id = int(
            trade.get("variantId")
            or trade.get("simulationVariantId")
            or trade.get("simulation_variant_id")
            or 0
        )
        if variant_id != COUNTERFACTUAL_VARIANT_ID:
            continue
        trade_id = trade.get("tradeId") or trade.get("simulatedTradeId")
        key = _executed_trade_key(trade)
        if trade_id is not None and key[2]:
            mapping[trade_id] = key
    return mapping


def _performance(snapshots: List) -> tuple[Optional[float], Optional[float]]:
    if not snapshots:
        return None, None
    cumulative_return = float(getattr(snapshots[-1], "cumulative_return", 0.0)) * 100
    drawdown = min(float(getattr(item, "drawdown_rate", 0.0)) for item in snapshots) * 100
    return round(cumulative_return, 2), round(drawdown, 2)


def _violated_trade_ids(evaluation: dict, decision_reviews: List[dict]) -> List[dict]:
    """Collect the reviews whose match for this principle came out VIOLATED."""
    principle_id = evaluation.get("principleSetItemId")
    target_rule = evaluation.get("targetRule")
    principle_text = evaluation.get("principleText")
    violated = []
    for review in decision_reviews:
        for match in review.get("principleMatches", []):
            if match.get("judgment") != "VIOLATED":
                continue
            same_principle = (
                principle_id is not None
                and match.get("principleSetItemId") == principle_id
            ) or (
                principle_id is None
                and target_rule
                and match.get("targetRule") == target_rule
                and match.get("principleText") == principle_text
            )
            if same_principle:
                violated.append(review)
                break
    return violated


def _summary_text(difference: float, removed_count: int) -> str:
    if difference > 0:
        return (
            f"이 원칙을 지켜 위반 매수 {removed_count}건을 하지 않았다면 "
            f"수익률이 {difference:.2f}%p 높았습니다."
        )
    if difference < 0:
        return (
            f"이 원칙을 지켜 위반 매수 {removed_count}건을 하지 않았다면 "
            f"수익률이 {abs(difference):.2f}%p 낮았습니다."
        )
    return f"위반 매수 {removed_count}건을 제외해도 수익률 차이는 없었습니다."


def build_principle_counterfactuals(
    report: dict,
    *,
    period_start: str,
    period_end: str,
    initial_capital: float,
    securities_map: Dict[int, dict],
    daily_prices: List[dict],
    trading_days: List[str],
    actual_trades: List[dict],
    simulated_trades: List[dict],
    initial_positions: Dict[int, Position],
    disclosures_by_date: Optional[dict] = None,
    baseline_return_percent: Optional[float] = None,
    baseline_mdd_percent: Optional[float] = None,
) -> int:
    """Attach a counterfactual to each evaluation, in place. Returns runs made."""
    evaluations = report.get("principleEvaluations") or []
    decision_reviews = report.get("decisionReviews") or []
    if not evaluations or not actual_trades:
        return 0

    origin_by_trade_id = _origin_keys_by_report_trade_id(simulated_trades)
    completed = 0

    for evaluation in evaluations:
        violated_reviews = _violated_trade_ids(evaluation, decision_reviews)
        if not violated_reviews:
            evaluation["counterfactual"] = unsupported(
                "NO_VIOLATION",
                "이 원칙을 위반한 거래가 없어 비교할 대안 시나리오가 없습니다.",
            )
            continue

        blocked_actions = sorted({
            str(review.get("action") or "")
            for review in violated_reviews
            if str(review.get("action") or "") not in REMOVABLE_ACTIONS
        })
        if blocked_actions:
            evaluation["counterfactual"] = unsupported(
                "SELL_SIDE_NOT_SUPPORTED",
                "매도를 하지 않은 위반은 없던 주문을 만들어야 하므로 대안 시나리오를 "
                "계산하지 않습니다.",
            )
            continue

        if completed >= MAX_COUNTERFACTUAL_PRINCIPLES:
            evaluation["counterfactual"] = unsupported(
                "LIMIT_REACHED",
                f"한 번의 실행에서 최대 {MAX_COUNTERFACTUAL_PRINCIPLES}개 원칙까지만 "
                "대안 시나리오를 계산합니다.",
            )
            continue

        removal_keys = []
        for review in violated_reviews:
            key = origin_by_trade_id.get(review.get("tradeId"))
            if key:
                removal_keys.append(key)
        if not removal_keys:
            evaluation["counterfactual"] = unsupported(
                "SOURCE_TRADE_NOT_MATCHED",
                "위반 거래를 원본 거래 내역과 연결하지 못해 대안 시나리오를 "
                "계산하지 않았습니다.",
            )
            continue

        # Consume one source row per violation so a repeated security/side/time
        # never removes more trades than were actually flagged.
        pending = dict.fromkeys(removal_keys, 0)
        for key in removal_keys:
            pending[key] += 1
        filtered_trades = []
        removed_count = 0
        for trade in actual_trades:
            key = _raw_trade_key(trade)
            if pending.get(key):
                pending[key] -= 1
                removed_count += 1
                continue
            filtered_trades.append(trade)

        if not removed_count:
            evaluation["counterfactual"] = unsupported(
                "SOURCE_TRADE_NOT_MATCHED",
                "위반 거래를 원본 거래 내역과 연결하지 못해 대안 시나리오를 "
                "계산하지 않았습니다.",
            )
            continue

        try:
            engine = BacktestEngine(
                simulation_run_id=0,
                period_start=period_start,
                period_end=period_end,
                initial_capital=initial_capital,
                securities_map=securities_map,
                daily_prices=daily_prices,
            )
            engine.register_variant(
                COUNTERFACTUAL_VARIANT_ID,
                ActualUserStrategy(
                    COUNTERFACTUAL_VARIANT_ID,
                    filtered_trades,
                    trading_days=trading_days,
                ),
                initial_positions=initial_positions,
                initial_cash=0.0,
            )
            _, snapshots = engine.run(disclosures_by_date)
        except Exception as error:
            logger.warning(
                "Counterfactual backtest failed for %s (%s)",
                evaluation.get("evaluationId"),
                type(error).__name__,
            )
            evaluation["counterfactual"] = unsupported(
                "CALCULATION_FAILED",
                "대안 시나리오를 계산하는 중 오류가 발생해 결과를 제공하지 않습니다.",
            )
            continue

        counterfactual_return, counterfactual_mdd = _performance(snapshots)
        if counterfactual_return is None:
            evaluation["counterfactual"] = unsupported(
                "NO_SNAPSHOT",
                "대안 시나리오에서 성과 스냅샷이 생성되지 않았습니다.",
            )
            continue

        baseline_return = round(float(baseline_return_percent or 0.0), 2)
        difference = round(counterfactual_return - baseline_return, 2)
        mdd_difference = (
            round(counterfactual_mdd - float(baseline_mdd_percent), 2)
            if baseline_mdd_percent is not None and counterfactual_mdd is not None
            else None
        )
        evaluation["counterfactual"] = {
            "supported": True,
            "method": "VIOLATING_BUY_ORDERS_REMOVED",
            "removedTradeCount": removed_count,
            "removedTradeIds": [review.get("tradeId") for review in violated_reviews][:20],
            "baselineReturnPercent": baseline_return,
            "counterfactualReturnPercent": counterfactual_return,
            "differencePercentPoint": difference,
            "baselineMddPercent": baseline_mdd_percent,
            "counterfactualMddPercent": counterfactual_mdd,
            "mddDifferencePercentPoint": mdd_difference,
            "summary": _summary_text(difference, removed_count),
            "disclaimer": DISCLAIMER,
            "calculationSource": "DETERMINISTIC_BACKTEST_REPLAY",
        }
        completed += 1

    report.setdefault("generationMetadata", {})["counterfactualSource"] = (
        "DETERMINISTIC_BACKTEST_REPLAY" if completed else "NOT_AVAILABLE"
    )
    report["generationMetadata"]["counterfactualRunCount"] = completed
    return completed
