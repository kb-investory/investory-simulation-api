"""Deterministic post-simulation analytics and Monte Carlo comparison."""

from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from functools import partial
from math import floor
from typing import Dict, Iterable, List, Optional

from app.modules.simulation.engine.backtest import BacktestEngine
from app.modules.simulation.models import OrderAudit
from app.modules.simulation.engine.strategies import RandomBotStrategy
from app.modules.simulation.engine.evaluator import StockEvaluator

# #32: 500회 몬테카를로 루프를 단일 스레드에서 순차 실행하면, 이 서비스가 동기 def 핸들러라
# CPU 바운드 파이썬 코드는 GIL 때문에 요청이 몰려도 스레드를 늘리는 것만으론 병렬화가 안 된다
# (로컬 실측: 5명 동시 avg 1.98s -> 50명 동시 avg 16.71s, 8~10배 느려짐). 각 시드의 계산은
# seed 말고는 서로 공유하는 상태가 전혀 없는 embarrassingly parallel 구조라, 프로세스 풀로
# 코어 수만큼 실제 병렬 실행한다 — 지연 생성 싱글턴으로 요청마다 프로세스를 새로 띄우는
# 오버헤드를 피한다.
_executor: Optional[ProcessPoolExecutor] = None


def _get_monte_carlo_executor() -> ProcessPoolExecutor:
    global _executor
    if _executor is None:
        # os.cpu_count()가 기본값 — uvicorn --workers N으로 프로세스를 여러 개 띄우면 이 풀도
        # 프로세스 수만큼 늘어나서, 전체 코어 수를 몇 배로 오버섭스크라이브하게 된다(실측:
        # 4-worker에서 단일 프로세스보다 오히려 더 느려짐). 멀티프로세스 모드에서 실측할 땐
        # MONTE_CARLO_WORKERS를 코어수/워커수로 낮춰서 줄 것.
        worker_count = int(os.getenv("MONTE_CARLO_WORKERS", str(os.cpu_count())))
        _executor = ProcessPoolExecutor(max_workers=worker_count)
    return _executor


def shutdown_monte_carlo_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None


def _simulate_random_trace(
    seed: int,
    period_start: str,
    period_end: str,
    initial_capital: float,
    securities_map: Dict[int, dict],
    daily_prices: List[dict],
) -> float:
    """단일 시드의 원숭이 봇 트레이스 하나를 실행해 최종 누적수익률(%)만 반환한다.

    ProcessPoolExecutor로 넘겨지므로 인자/반환값이 전부 pickle 가능한 순수 데이터여야 한다
    (BacktestEngine/RandomBotStrategy는 DB나 스레드 상태를 안 갖는 순수 계산 객체라 안전하다).
    """
    engine = BacktestEngine(
        simulation_run_id=seed + 1,
        period_start=period_start,
        period_end=period_end,
        initial_capital=initial_capital,
        securities_map=securities_map,
        daily_prices=daily_prices,
    )
    engine.register_variant(4, RandomBotStrategy(4, seed=seed))
    _, snapshots = engine.run()
    final_return = snapshots[-1].cumulative_return * 100 if snapshots else 0.0
    return round(final_return, 4)


def _quantile(sorted_values: List[float], probability: float) -> float:
    if not sorted_values:
        return 0.0
    position = (len(sorted_values) - 1) * probability
    lower = floor(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def run_random_monte_carlo(
    period_start: str,
    period_end: str,
    initial_capital: float,
    securities_map: Dict[int, dict],
    daily_prices: List[dict],
    run_count: int = 500,
    seed_start: int = 0,
) -> dict:
    worker = partial(
        _simulate_random_trace,
        period_start=period_start,
        period_end=period_end,
        initial_capital=initial_capital,
        securities_map=securities_map,
        daily_prices=daily_prices,
    )
    seeds = range(seed_start, seed_start + run_count)
    # chunksize 기본값(1)은 시드 500개를 각각 별도 작업으로 보내 매번 worker(=securities_map/
    # daily_prices를 통째로 물고 있는 partial)를 새로 pickle한다 — 이 데이터가 작지 않아서(30종목
    # x 수십~수백일), 500번 반복되는 직렬화 비용이 병렬화로 아끼는 시간보다 커진다(로컬 50명 동시
    # 실측: chunksize=1일 때 순차 실행보다 오히려 느려짐). 워커 하나당 한 번만 보내지도록 청크를
    # 크게 잡아 pickle 횟수를 코어 수 근처로 줄인다.
    worker_count = os.cpu_count() or 1
    chunksize = max(1, -(-run_count // worker_count))  # ceil division
    returns = list(_get_monte_carlo_executor().map(worker, seeds, chunksize=chunksize))

    ordered = sorted(returns)
    return {
        "runCount": run_count,
        "seedStart": seed_start,
        "seedEnd": seed_start + run_count - 1,
        "minimumReturnPercent": round(ordered[0], 2) if ordered else 0.0,
        "lowerQuartileReturnPercent": round(_quantile(ordered, 0.25), 2),
        "medianReturnPercent": round(_quantile(ordered, 0.50), 2),
        "upperQuartileReturnPercent": round(_quantile(ordered, 0.75), 2),
        "maximumReturnPercent": round(ordered[-1], 2) if ordered else 0.0,
        "distributionPercent": ordered,
    }


def add_personal_bot_percentile(distribution: dict, personal_return_percent: float) -> dict:
    values = distribution.get("distributionPercent", [])
    if not values:
        distribution["personalBotPercentile"] = None
        return distribution
    not_greater = sum(value <= personal_return_percent for value in values)
    distribution["personalBotPercentile"] = round(not_greater / len(values) * 100, 1)
    return distribution


def calculate_benchmarks(
    daily_prices: List[dict],
    securities_map: Dict[int, dict],
    index_prices: Optional[List[dict]] = None,
) -> List[dict]:
    result = []
    actual_index_codes = set()
    if index_prices:
        grouped: Dict[str, List[float]] = defaultdict(list)
        for item in index_prices:
            grouped[item["indexCode"]].append(float(item["closePrice"]))
        for index_code, values in grouped.items():
            if len(values) < 2 or values[0] <= 0:
                continue
            actual_index_codes.add(index_code)
            result.append({
                "benchmark": index_code,
                "returnPercent": round((values[-1] - values[0]) / values[0] * 100, 2),
                "securityCount": None,
                "method": "시장 지수 종가 기준",
            })
    closes: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(list))
    for item in daily_prices:
        market = securities_map.get(item["securityId"], {}).get("marketType")
        if market in {"KOSPI", "KOSDAQ"}:
            closes[market][item["securityId"]].append(float(item["closePrice"]))

    for market, by_security in closes.items():
        if market in actual_index_codes:
            continue
        security_returns = [
            (values[-1] - values[0]) / values[0] * 100
            for values in by_security.values()
            if len(values) >= 2 and values[0] > 0
        ]
        result.append({
            "benchmark": f"{market}_EQUAL_WEIGHT_UNIVERSE",
            "returnPercent": round(sum(security_returns) / len(security_returns), 2) if security_returns else 0.0,
            "securityCount": len(security_returns),
            "method": "시뮬레이션 투자 가능 종목의 동일가중 수익률",
        })
    return result


def _trade_variant_id(trade: dict) -> int:
    return int(trade.get("variantId") or trade.get("simulationVariantId") or trade.get("simulation_variant_id") or 0)


def calculate_variant_metrics(
    participant_summary: List[dict],
    trades: List[dict],
    snapshots: List[dict],
    audits: Iterable[OrderAudit],
    benchmarks: List[dict],
    actual_compliance: Optional[dict] = None,
) -> List[dict]:
    audits_by_variant: Dict[int, List[OrderAudit]] = defaultdict(list)
    for audit in audits:
        audits_by_variant[audit.simulation_variant_id].append(audit)

    benchmark_item = next((item for item in benchmarks if item["benchmark"].startswith("KOSPI")), None)
    benchmark_return = benchmark_item["returnPercent"] if benchmark_item else 0.0
    benchmark_name = benchmark_item["benchmark"] if benchmark_item else None
    results = []
    for summary in participant_summary:
        variant_id = int(summary["variantId"])
        variant_trades = [item for item in trades if _trade_variant_id(item) == variant_id]
        variant_snapshots = [
            item for item in snapshots
            if int(item.get("variantId") or item.get("simulationVariantId") or 0) == variant_id
        ]
        average_equity = (
            sum(float(item.get("portfolioValue", 0.0)) for item in variant_snapshots) / len(variant_snapshots)
            if variant_snapshots else float(summary.get("totalEquity", 0.0))
        )
        gross_turnover = sum(float(item.get("quantity", 0.0)) * float(item.get("unitPrice", 0.0)) for item in variant_trades)
        costs = sum(float(item.get("transactionCostAmount", 0.0)) for item in variant_trades)
        variant_audits = audits_by_variant.get(variant_id, [])
        compliant = sum(item.status in {"EXECUTED", "ADJUSTED"} for item in variant_audits)
        violations = [asdict(item) for item in variant_audits if item.status == "REJECTED"]
        compliance_percent = round(compliant / len(variant_audits) * 100, 1) if variant_audits else 100.0
        assessed_count = len(variant_audits)
        if variant_id == 1 and actual_compliance:
            compliance_percent = actual_compliance["compliancePercent"]
            violations = actual_compliance["violations"]
            assessed_count = actual_compliance["assessedTradeCount"]
        return_percent = float(summary.get("cumulativeReturnPercent", 0.0))
        results.append({
            "variantId": variant_id,
            "variantType": summary.get("variantType"),
            "turnoverPercent": round(gross_turnover / average_equity * 100, 2) if average_equity > 0 else 0.0,
            "transactionCostAmount": round(costs, 2),
            "transactionCostDragPercent": round(costs / average_equity * 100, 4) if average_equity > 0 else 0.0,
            "principleCompliancePercent": compliance_percent,
            "principleAssessedTradeCount": assessed_count,
            "principleViolationCount": len(violations),
            "principleViolations": violations[:20],
            "benchmark": benchmark_name,
            "excessReturnPercentPoint": round(return_percent - benchmark_return, 2),
        })
    return results


def calculate_security_contributions(engine: BacktestEngine, trades: List[dict]) -> List[dict]:
    cash_flows: Dict[tuple, float] = defaultdict(float)
    costs: Dict[tuple, float] = defaultdict(float)
    for trade in trades:
        variant_id = _trade_variant_id(trade)
        security_id = int(trade.get("securityId") or trade.get("security_id") or 0)
        gross = float(trade.get("quantity", 0.0)) * float(trade.get("unitPrice", 0.0))
        cost = float(trade.get("transactionCostAmount", 0.0))
        sign = -1.0 if trade.get("tradeSide") in {"BUY", "ADD"} else 1.0
        cash_flows[(variant_id, security_id)] += sign * gross
        costs[(variant_id, security_id)] += cost

    result = []
    keys = set(cash_flows)
    for variant_id, portfolio in engine.portfolios.items():
        keys.update((variant_id, security_id) for security_id in portfolio.positions)
    for variant_id, security_id in sorted(keys):
        ending_value = 0.0
        if variant_id in engine.portfolios and security_id in engine.portfolios[variant_id].positions:
            ending_value = engine.portfolios[variant_id].positions[security_id].market_value
        initial_value = 0.0
        initial_position = engine.initial_positions_by_variant.get(variant_id, {}).get(security_id)
        if initial_position:
            initial_value = initial_position.market_value
        contribution = cash_flows[(variant_id, security_id)] + ending_value - initial_value - costs[(variant_id, security_id)]
        result.append({
            "variantId": variant_id,
            "securityId": security_id,
            "securityName": engine.securities_map.get(security_id, {}).get("securityName", ""),
            "contributionAmount": round(contribution, 2),
            "endingMarketValue": round(ending_value, 2),
            "initialMarketValue": round(initial_value, 2),
            "transactionCostAmount": round(costs[(variant_id, security_id)], 2),
        })
    return sorted(result, key=lambda item: abs(item["contributionAmount"]), reverse=True)


def calculate_action_contributions(trades: List[dict], daily_prices: List[dict]) -> List[dict]:
    """Estimate each action type's five-trading-day directional contribution."""
    prices_by_security: Dict[int, List[tuple]] = defaultdict(list)
    for price in daily_prices:
        prices_by_security[int(price["securityId"])].append(
            (str(price["priceDate"]), float(price["closePrice"]))
        )
    for series in prices_by_security.values():
        series.sort(key=lambda item: item[0])

    grouped = defaultdict(lambda: {"count": 0, "notional": 0.0, "estimated": 0.0, "observed": 0})
    for trade in trades:
        action = str(trade.get("tradeSide", ""))
        if action not in {"BUY", "ADD", "SELL", "REDUCE"}:
            continue
        variant_id = _trade_variant_id(trade)
        security_id = int(trade.get("securityId", 0))
        trade_date = str(trade.get("appliedTradingDate") or trade.get("tradedAt", ""))[:10]
        notional = float(trade.get("quantity", 0.0)) * float(trade.get("unitPrice", 0.0))
        bucket = grouped[(variant_id, action)]
        bucket["count"] += 1
        bucket["notional"] += notional
        series = prices_by_security.get(security_id, [])
        start_index = next((idx for idx, item in enumerate(series) if item[0] >= trade_date), None)
        if start_index is None or start_index + 5 >= len(series) or series[start_index][1] <= 0:
            continue
        future_return = (series[start_index + 5][1] - series[start_index][1]) / series[start_index][1]
        direction = 1.0 if action in {"BUY", "ADD"} else -1.0
        bucket["estimated"] += notional * future_return * direction
        bucket["observed"] += 1

    return [
        {
            "variantId": variant_id,
            "action": action,
            "tradeCount": values["count"],
            "observedOutcomeCount": values["observed"],
            "tradedNotionalAmount": round(values["notional"], 2),
            "estimated5DayDirectionalContributionAmount": round(values["estimated"], 2),
            "method": "체결 후 5거래일 수익률에 매수 +1, 매도 -1 방향을 적용한 추정치",
        }
        for (variant_id, action), values in sorted(grouped.items())
    ]


def find_divergence_moments(trades: List[dict], daily_prices: Optional[List[dict]] = None, limit: int = 20) -> List[dict]:
    actual = defaultdict(list)
    personal = defaultdict(list)
    for trade in trades:
        key = (str(trade.get("appliedTradingDate") or trade.get("tradedAt", ""))[:10], int(trade.get("securityId", 0)))
        if _trade_variant_id(trade) == 1:
            actual[key].append(trade.get("tradeSide"))
        elif _trade_variant_id(trade) == 2:
            personal[key].append(trade.get("tradeSide"))
    future_prices: Dict[int, List[tuple]] = defaultdict(list)
    for price in daily_prices or []:
        future_prices[int(price["securityId"])].append((price["priceDate"], float(price["closePrice"])))
    moments = []
    for key in sorted(set(actual) | set(personal)):
        if actual.get(key) == personal.get(key):
            continue
        subsequent_return = None
        series = future_prices.get(key[1], [])
        start_index = next((idx for idx, item in enumerate(series) if item[0] >= key[0]), None)
        if start_index is not None and start_index + 5 < len(series) and series[start_index][1] > 0:
            subsequent_return = round((series[start_index + 5][1] - series[start_index][1]) / series[start_index][1] * 100, 2)
        moments.append({
            "date": key[0],
            "securityId": key[1],
            "actualUserActions": actual.get(key, ["HOLD"]),
            "personalBotActions": personal.get(key, ["HOLD"]),
            "summary": "같은 날 실제 사용자와 개인봇의 행동이 달랐습니다.",
            "subsequent5TradingDayReturnPercent": subsequent_return,
        })
    moments.sort(
        key=lambda item: (
            any(
                action in {"BUY", "ADD", "SELL", "REDUCE"}
                for action in item["actualUserActions"]
            ),
            abs(item["subsequent5TradingDayReturnPercent"] or 0.0),
        ),
        reverse=True,
    )
    return moments[:limit]


def evaluate_actual_principle_compliance(
    trades: List[dict],
    daily_prices: List[dict],
    securities_map: Dict[int, dict],
    rule_schema: dict,
) -> dict:
    price_map = {(item["priceDate"], int(item["securityId"])): item for item in daily_prices}
    evaluator = StockEvaluator()
    assessed = 0
    compliant = 0
    violations = []
    for trade in trades:
        if _trade_variant_id(trade) != 1 or trade.get("tradeSide") != "BUY":
            continue
        assessed += 1
        trade_date = str(trade.get("appliedTradingDate") or trade.get("tradedAt", ""))[:10]
        security_id = int(trade.get("securityId", 0))
        price = price_map.get((trade_date, security_id))
        codes = []
        if not price:
            codes.append("PRICE_NOT_AVAILABLE")
        else:
            if not evaluator._passes_universe(price, securities_map.get(security_id, {}), rule_schema.get("universe", {})):
                codes.append("UNIVERSE_RULE_VIOLATED")
            if not evaluator._passes_entry(price, None, rule_schema.get("entry", {})):
                codes.append("ENTRY_RULE_VIOLATED")
        if codes:
            violations.append({
                "tradeId": trade.get("tradeId"),
                "securityId": security_id,
                "tradedAt": trade.get("tradedAt"),
                "reasonCodes": codes,
            })
        else:
            compliant += 1
    return {
        "assessedTradeCount": assessed,
        "compliantTradeCount": compliant,
        "compliancePercent": round(compliant / assessed * 100, 1) if assessed else 100.0,
        "violations": violations,
        "scope": "BUY_UNIVERSE_AND_ENTRY_RULES",
    }


def detect_behavior_patterns(
    trades: List[dict],
    daily_prices: List[dict],
    snapshots: Optional[List[dict]] = None,
) -> List[dict]:
    price_map = {(item["priceDate"], item["securityId"]): item for item in daily_prices}
    actual_trades = [item for item in trades if _trade_variant_id(item) == 1]
    fomo = []
    for trade in actual_trades:
        if trade.get("tradeSide") != "BUY":
            continue
        trade_date = str(trade.get("appliedTradingDate") or trade.get("tradedAt", ""))[:10]
        price = price_map.get((trade_date, int(trade.get("securityId", 0))), {})
        day5 = price.get("day5Return")
        if day5 is not None and float(day5) >= 0.10:
            fomo.append(trade)

    patterns = []
    if fomo:
        patterns.append({
            "patternCode": "FOMO_BUY",
            "label": "추격매수",
            "count": len(fomo),
            "evidenceTradeIds": [item.get("tradeId") for item in fomo[:10]],
            "description": "최근 5거래일 10% 이상 상승한 뒤 매수한 거래입니다.",
        })

    actual_equity_by_date = {
        str(item.get("performanceDate") or item.get("snapshotDate"))[:10]: float(item.get("portfolioValue", 0.0))
        for item in snapshots or []
        if int(item.get("variantId") or item.get("simulationVariantId") or 0) == 1
    }
    concentrated = []
    for trade in actual_trades:
        if trade.get("tradeSide") != "BUY":
            continue
        trade_date = str(trade.get("appliedTradingDate") or trade.get("tradedAt", ""))[:10]
        equity = actual_equity_by_date.get(trade_date, 0.0)
        notional = float(trade.get("quantity", 0.0)) * float(trade.get("unitPrice", 0.0))
        if equity > 0 and notional / equity >= 0.20:
            concentrated.append(trade)
    if concentrated:
        patterns.append({
            "patternCode": "CONCENTRATED_BUY",
            "label": "과도한 집중 가능성",
            "count": len(concentrated),
            "evidenceTradeIds": [item.get("tradeId") for item in concentrated[:10]],
            "description": "한 번의 매수 금액이 당일 포트폴리오 자산의 20% 이상인 거래입니다.",
        })

    actual_by_security: Dict[int, List[dict]] = defaultdict(list)
    for trade in actual_trades:
        actual_by_security[int(trade.get("securityId", 0))].append(trade)
    delayed = []
    for security_id, security_trades in actual_by_security.items():
        buys = [item for item in security_trades if item.get("tradeSide") == "BUY"]
        sells = [item for item in security_trades if item.get("tradeSide") == "SELL"]
        if not buys or not sells:
            continue
        average_buy = sum(float(item.get("quantity", 0.0)) * float(item.get("unitPrice", 0.0)) for item in buys) / max(
            sum(float(item.get("quantity", 0.0)) for item in buys), 1.0
        )
        first_buy_date = min(str(item.get("appliedTradingDate") or item.get("tradedAt", ""))[:10] for item in buys)
        first_breach = next((
            price["priceDate"]
            for price in daily_prices
            if int(price["securityId"]) == security_id
            and price["priceDate"] >= first_buy_date
            and float(price["closePrice"]) <= average_buy * 0.90
        ), None)
        if not first_breach:
            continue
        first_sell = min(str(item.get("appliedTradingDate") or item.get("tradedAt", ""))[:10] for item in sells)
        intervening_days = sorted({
            price["priceDate"] for price in daily_prices
            if int(price["securityId"]) == security_id and first_breach <= price["priceDate"] < first_sell
        })
        if len(intervening_days) >= 3:
            delayed.append({"securityId": security_id, "breachDate": first_breach, "sellDate": first_sell, "delayTradingDays": len(intervening_days)})
    if delayed:
        patterns.append({
            "patternCode": "DELAYED_STOP_LOSS",
            "label": "손절 지연",
            "count": len(delayed),
            "evidence": delayed[:10],
            "description": "평균 매수가 대비 -10% 하락 후 3거래일 이상 지나 매도한 사례입니다.",
        })
    return patterns
