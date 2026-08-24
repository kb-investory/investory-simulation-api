"""
================================================================================
[API Endpoint Service] simulation_run_service.py
================================================================================
■ 역할:
  - POST /simulations/run 요청 하나의 전체 백테스트 실행(입력 검증 → 데이터 로딩 →
    BacktestEngine 실행 → 분석(analytics) → 원칙 반증 시뮬레이션 → 영속화 예약 →
    응답 조립)을 오케스트레이션합니다.
  - HTTP 예외 변환과 report 강화(enrichment) 스케줄링은 simulation.py 라우터가 담당합니다
    (get_simulation_report 엔드포인트와 공유하는 헬퍼이므로 생성자로 주입받습니다).
================================================================================
"""

import logging
from dataclasses import asdict
from time import perf_counter
from typing import Callable, Dict, List, Optional

from fastapi import BackgroundTasks

from app.modules.simulation.engine.backtest import BacktestEngine
from app.modules.simulation.engine.strategies import (
    ActualUserStrategy, PersonalBotStrategy, FamousStrategyBot, RandomBotStrategy
)
from app.modules.simulation.models import Position
from app.modules.simulation.persistence.repository import SimulationDataError, SimulationRepository
from app.modules.simulation.persistence.rationale_snapshots import build_rationale_type_snapshots
from app.modules.simulation.analytics.report_generator import SimulationReportGenerator
from app.modules.simulation.analytics.counterfactual import build_principle_counterfactuals
from app.modules.simulation.analytics.analytics import (
    add_personal_bot_percentile,
    calculate_action_contributions,
    calculate_benchmarks,
    calculate_security_contributions,
    calculate_variant_metrics,
    detect_behavior_patterns,
    evaluate_actual_principle_compliance,
    find_divergence_moments,
    run_random_monte_carlo,
)
from app.modules.simulation.analytics.comparator_details import (
    RANDOM_MONTE_CARLO_RUN_COUNT,
    RANDOM_TRACE_SEED,
)
from app.modules.simulation.collectors.market_index_collector import MarketIndexCollector
from app.modules.simulation.persistence.db_persistence import (
    save_simulation_run_to_db,
    find_existing_simulation_from_db,
    reserve_simulation_run_to_db,
    save_simulation_report_to_db,
)

from app.api.endpoints.simulation_helpers import (
    SimulationRunRequest, normalize_daily_snapshot, normalize_trade,
    SIMULATION_RUN_CACHE,
)
from app.core.metrics import SIMULATION_STAGE_LATENCY_SECONDS

logger = logging.getLogger(__name__)

SUPPORTED_PARTICIPANTS = {
    "ACTUAL_USER": 1,
    "PERSONAL_BOT": 2,
    "FAMOUS_STRATEGY": 3,
    "RANDOM_BOT": 4,
}


class SimulationRunService:
    """POST /simulations/run 요청 하나의 전체 실행을 오케스트레이션한다."""

    VARIANT_NAMES = {
        1: ("ACTUAL_USER", "실제 나"),
        2: ("PERSONAL_BOT", "나의 투자봇 v1"),
        3: ("FAMOUS_STRATEGY", "우량 가치·품질 퀀트 봇"),
        4: ("RANDOM_BOT", "원숭이 봇"),
    }

    def __init__(
        self,
        req: SimulationRunRequest,
        background_tasks: BackgroundTasks,
        user_id: int,
        schedule_report_enrichment: Callable,
    ):
        self.req = req
        self.background_tasks = background_tasks
        self.user_id = user_id
        self.schedule_report_enrichment = schedule_report_enrichment
        self.request_started = perf_counter()
        self.repository: Optional[SimulationRepository] = None

    def run(self) -> dict:
        try:
            return self._run()
        finally:
            if self.repository is not None:
                self.repository.close()

    def _run(self) -> dict:
        self._validate_and_resolve()
        cached = self._check_cache()
        if cached is not None:
            # self.repository의 DB 작업은 여기서 끝 — 캐시 히트 경로도 아래 실경로와 동일하게
            # 커넥션을 곧장 반납한다(run()의 finally가 또 닫아도 close()는 idempotent).
            self.repository.close()
            return cached
        self._load_data()
        # #31: self.repository(reuse_connection=True)가 DB에 실제로 접근하는 건 여기까지다.
        # 이 아래(_run_backtest/_summarize_and_simulate의 몬테카를로 500회/_compute_analytics)는
        # 전부 CPU 바운드 연산이라 DB를 안 쓰는데, 커넥션을 요청 끝까지(run()의 finally) 붙들고
        # 있으면 그 시간만큼 풀 슬롯을 그냥 놀리는 셈이라 동시 요청이 몰리면 풀이 고갈된다
        # (로컬 50명 동시 실측: QueuePool limit ... timeout). 여기서 바로 반납해 그 구간 동안
        # 다른 요청이 이 슬롯을 쓸 수 있게 한다.
        self.repository.close()
        self._run_backtest()
        self._summarize_and_simulate()
        self._compute_analytics()
        self._persist_and_report()
        self._apply_counterfactuals_and_schedule()
        return self._build_response()

    # ---- 1. 입력 검증 + 계좌/초기자금/컴파일봇 조회 ----
    def _validate_and_resolve(self) -> None:
        req = self.req
        if not req.period_start or not req.period_end or req.period_start >= req.period_end:
            raise SimulationDataError("INVALID_PERIOD", "시작일은 종료일보다 빨라야 합니다.")

        self.participant_types = req.participant_types or list(SUPPORTED_PARTICIPANTS)
        invalid_types = sorted(set(self.participant_types) - set(SUPPORTED_PARTICIPANTS))
        if invalid_types:
            raise SimulationDataError(
                "INVALID_PARTICIPANT_TYPE",
                "지원하지 않는 참가자 유형이 포함되어 있습니다.",
                {"invalidTypes": invalid_types},
            )

        self.repository = SimulationRepository(reuse_connection=True)
        self.account_id = self.repository.resolve_account_id(self.user_id, req.account_id)
        self.initial_state = self.repository.load_initial_snapshot(self.account_id, req.period_start)
        self.initial_capital = float(self.initial_state["initialCapital"])

        self.compiled_bot = None
        self.excluded_participants: List[dict] = []
        if "PERSONAL_BOT" in self.participant_types:
            try:
                self.compiled_bot = self.repository.load_compiled_personal_bot(
                    self.user_id, req.personal_bot_id
                )
            except SimulationDataError as error:
                # A specific bot id that doesn't exist is a real failure below.
                # Only "nobody has compiled one yet" degrades — the other
                # participants never depended on a personal bot existing.
                if req.personal_bot_id or error.code != "PERSONAL_BOT_NOT_COMPILED":
                    raise
                self.participant_types = [
                    participant_type
                    for participant_type in self.participant_types
                    if participant_type != "PERSONAL_BOT"
                ]
                self.excluded_participants.append(
                    {"variantType": "PERSONAL_BOT", "reason": error.code}
                )

        if not self.participant_types:
            raise SimulationDataError(
                "NO_RUNNABLE_PARTICIPANTS",
                "실행 가능한 참가자가 없습니다.",
                {"excludedParticipants": self.excluded_participants},
            )

    # ---- 2. 캐시 확인 ----
    def _check_cache(self) -> Optional[dict]:
        req = self.req
        cached_result = find_existing_simulation_from_db(
            user_id=self.user_id,
            period_start=req.period_start,
            period_end=req.period_end,
            initial_capital=self.initial_capital,
            participant_types=self.participant_types,
            personal_bot_id=self.compiled_bot["personalBotId"] if self.compiled_bot else None,
        )
        if cached_result and isinstance(cached_result.get("positionSnapshots"), list):
            cached_result["accountId"] = self.account_id
            cached_result["dataSource"] = "MYSQL"
            cached_result["usesMockData"] = False
            cache_lookup_ms = (perf_counter() - self.request_started) * 1000
            SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="cacheLookup").observe(cache_lookup_ms / 1000)
            cached_result["executionTimingMs"] = {
                "cacheLookup": round(cache_lookup_ms, 1),
                "cacheHit": True,
            }
            SIMULATION_RUN_CACHE[cached_result["simulationRunId"]] = cached_result
            return cached_result
        if cached_result:
            logger.info(
                "Cached simulation %s has no position snapshots; running the current engine.",
                cached_result.get("simulationRunId"),
            )
        return None

    # ---- 3. 데이터 로딩 ----
    def _load_data(self) -> None:
        req = self.req
        self.securities = self.repository.load_securities()
        self.daily_prices = self.repository.load_daily_prices(req.period_start, req.period_end)
        self.trading_days = sorted({item["priceDate"] for item in self.daily_prices})
        try:
            # #31: 기본 connection_factory(get_db_connection)를 쓰면 이미 self.repository가
            # 붙들고 있는 커넥션과 별개로 풀에서 하나를 더 체크아웃한다 — 요청 하나가 슬롯을
            # 2개 동시에 쓰는 셈이라, self.repository의 재사용 커넥션을 그대로 넘겨서 하나만
            # 쓰게 한다(_load_data() 안이라 아직 반납 전).
            self.benchmark_data = MarketIndexCollector(
                connection_factory=self.repository.connection_factory
            ).ensure_period(
                req.period_start,
                req.period_end,
                self.trading_days,
            )
        except Exception as error:
            logger.warning(
                "KRX benchmark collection failed; using the equal-weight fallback",
                exc_info=error,
            )
            self.benchmark_data = {
                "status": "FETCH_FAILED",
                "fetchedCount": 0,
                "missingCount": len(self.trading_days) * 2,
            }
        self.market_index_prices = self.repository.load_market_index_prices(req.period_start, req.period_end)
        self.user_trades = self.repository.load_actual_trades(self.account_id, req.period_start, req.period_end)
        self.principles_data = self.repository.load_principles(self.user_id) if self.compiled_bot else []
        self.rule_confirmations = self.repository.load_rule_confirmations(self.user_id) if self.compiled_bot else []
        self.disclosure_events = self.repository.load_disclosures(req.period_start, req.period_end)
        self.data_quality = self.repository.assess_trade_price_quality(self.account_id, req.period_start, req.period_end)
        self.data_load_ms = (perf_counter() - self.request_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="dataLoad").observe(self.data_load_ms / 1000)

        self.securities_map = {item["securityId"]: item for item in self.securities}
        self.rule_schema_dict = self.compiled_bot["ruleSchema"] if self.compiled_bot else {}
        self.rule_compilation = dict(self.compiled_bot.get("ruleCompilation") or {}) if self.compiled_bot else {}
        if self.compiled_bot:
            self.rule_compilation["reusedCompiledBot"] = True

        self.disclosures_by_date: Dict[str, Dict[int, dict]] = {}
        self.disclosure_model_counts: Dict[str, int] = {}
        for event in self.disclosure_events:
            model_name = event.get("analysisModel", "UNKNOWN")
            self.disclosure_model_counts[model_name] = self.disclosure_model_counts.get(model_name, 0) + 1
            event_date = event["eventDate"]
            if event.get("availableAt", "")[:10] > event_date:
                continue
            self.disclosures_by_date.setdefault(event_date, {})[event["securityId"]] = event

    # ---- 4. 엔진 구성 및 실행 ----
    def _run_backtest(self) -> None:
        req = self.req
        self.engine = BacktestEngine(
            simulation_run_id=req.simulation_run_id or 1,
            period_start=req.period_start,
            period_end=req.period_end,
            initial_capital=self.initial_capital,
            securities_map=self.securities_map,
            daily_prices=self.daily_prices,
        )
        self.initial_positions = {
            item["securityId"]: Position(
                security_id=item["securityId"],
                security_code=item["securityCode"],
                security_name=item["securityName"],
                quantity=item["quantity"],
                average_buy_price=item["averageCost"],
                current_price=item["marketValue"] / item["quantity"],
                acquired_date=self.initial_state["snapshotDate"],
            )
            for item in self.initial_state["holdings"]
            if item["quantity"] > 0
        }

        if "ACTUAL_USER" in self.participant_types:
            self.engine.register_variant(
                1,
                ActualUserStrategy(1, self.user_trades, trading_days=self.trading_days),
                initial_positions=self.initial_positions,
                initial_cash=0.0,
            )
        if "PERSONAL_BOT" in self.participant_types:
            self.engine.register_variant(2, PersonalBotStrategy(2, self.principles_data, self.rule_schema_dict))
        if "FAMOUS_STRATEGY" in self.participant_types:
            self.engine.register_variant(3, FamousStrategyBot(3))
        if "RANDOM_BOT" in self.participant_types:
            self.engine.register_variant(4, RandomBotStrategy(4, seed=RANDOM_TRACE_SEED))

        backtest_started = perf_counter()
        executed_trades, daily_snapshots = self.engine.run(self.disclosures_by_date)
        self.backtest_ms = (perf_counter() - backtest_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="backtest").observe(self.backtest_ms / 1000)
        self.normalized_snapshots = [normalize_daily_snapshot(item) for item in daily_snapshots]
        self.normalized_trades = [normalize_trade(item) for item in executed_trades]
        self.position_snapshots = self.engine.position_snapshots
        self.executed_trades_count = len(executed_trades)

    # ---- 5. 참가자 요약 통계 + 몬테카를로 ----
    def _summarize_and_simulate(self) -> None:
        participant_summary = []
        for participant_type in self.participant_types:
            variant_id = SUPPORTED_PARTICIPANTS[participant_type]
            snaps = [item for item in self.normalized_snapshots if item["variantId"] == variant_id]
            last_snap = snaps[-1] if snaps else {}
            daily_returns = [float(item["dailyReturn"]) for item in snaps]
            volatility = 0.0
            if len(daily_returns) > 1:
                mean_return = sum(daily_returns) / len(daily_returns)
                variance = sum((value - mean_return) ** 2 for value in daily_returns) / (len(daily_returns) - 1)
                volatility = round((variance ** 0.5) * (252 ** 0.5) * 100, 2)
            cumulative_return = float(last_snap.get("cumulativeReturn", 0.0))
            minimum_drawdown = min((float(item["drawdownRate"]) for item in snaps), default=0.0)
            participant_summary.append(
                {
                    "variantId": variant_id,
                    "variantType": participant_type,
                    "variantName": self.VARIANT_NAMES[variant_id][1],
                    "totalEquity": float(last_snap.get("portfolioValue", self.initial_capital)),
                    "cumulativeReturnPercent": round(cumulative_return * 100, 2),
                    "volatilityPercent": volatility,
                    "mddPercent": round(minimum_drawdown * 100, 2),
                }
            )
        self.participant_summary = participant_summary

        self.random_distribution = None
        self.monte_carlo_ms = 0.0
        if "RANDOM_BOT" in self.participant_types:
            monte_carlo_started = perf_counter()
            self.random_distribution = run_random_monte_carlo(
                period_start=self.req.period_start,
                period_end=self.req.period_end,
                initial_capital=self.initial_capital,
                securities_map=self.securities_map,
                daily_prices=self.daily_prices,
                run_count=RANDOM_MONTE_CARLO_RUN_COUNT,
                seed_start=0,
            )
            self.monte_carlo_ms = (perf_counter() - monte_carlo_started) * 1000
            SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="monteCarlo").observe(self.monte_carlo_ms / 1000)
            personal_summary = next(
                (item for item in self.participant_summary if item["variantType"] == "PERSONAL_BOT"),
                None,
            )
            if personal_summary:
                add_personal_bot_percentile(
                    self.random_distribution,
                    float(personal_summary["cumulativeReturnPercent"]),
                )
            random_summary = next(
                (item for item in self.participant_summary if item["variantType"] == "RANDOM_BOT"),
                None,
            )
            if random_summary:
                random_summary.update({
                    "traceSeed": RANDOM_TRACE_SEED,
                    "traceReturnPercent": random_summary["cumulativeReturnPercent"],
                    "monteCarloMedianReturnPercent": self.random_distribution["medianReturnPercent"],
                    "comparisonMode": f"{RANDOM_MONTE_CARLO_RUN_COUNT}_RUN_DISTRIBUTION_WITH_SEED_{RANDOM_TRACE_SEED}_TRACE",
                })

    # ---- 6. 분석(analytics) ----
    def _compute_analytics(self) -> None:
        analytics_started = perf_counter()
        self.benchmarks = calculate_benchmarks(self.daily_prices, self.securities_map, self.market_index_prices)
        self.order_audits = [asdict(item) for item in self.engine.order_audits]
        self.screening_audits = self.engine.screening_audits
        self.actual_compliance = evaluate_actual_principle_compliance(
            self.normalized_trades,
            self.daily_prices,
            self.securities_map,
            self.rule_schema_dict,
        )
        self.variant_metrics = calculate_variant_metrics(
            self.participant_summary,
            self.normalized_trades,
            self.normalized_snapshots,
            self.engine.order_audits,
            self.benchmarks,
            actual_compliance=self.actual_compliance,
        )
        self.security_contributions = calculate_security_contributions(self.engine, self.normalized_trades)
        self.action_contributions = calculate_action_contributions(self.normalized_trades, self.daily_prices)
        self.divergence_moments = find_divergence_moments(self.normalized_trades, self.daily_prices)
        self.behavior_patterns = detect_behavior_patterns(
            self.normalized_trades, self.daily_prices, self.normalized_snapshots
        )
        self.analytics_ms = (perf_counter() - analytics_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="analytics").observe(self.analytics_ms / 1000)
        self.analytics_payload = {
            "orderAudits": self.order_audits,
            "screeningAudits": self.screening_audits,
            "variantMetrics": self.variant_metrics,
            "benchmarks": self.benchmarks,
            "benchmarkData": self.benchmark_data,
            "randomDistribution": self.random_distribution,
            "securityContributions": self.security_contributions,
            "actionContributions": self.action_contributions,
            "divergenceMoments": self.divergence_moments,
            "behaviorPatterns": self.behavior_patterns,
            "actualPrincipleCompliance": self.actual_compliance,
            "positionSnapshots": self.position_snapshots,
            "rationaleTypeSnapshots": build_rationale_type_snapshots(self.normalized_trades),
            # Keep the evaluated principle identities in analytics_json so a
            # report can be rebuilt without reading today's mutable principle set.
            "principleItems": self.principles_data,
            "ruleConfirmations": self.rule_confirmations,
            "securitySnapshots": self.securities,
        }

    # ---- 7. 영속화 예약 + 리포트 생성 ----
    def _persist_and_report(self) -> None:
        req = self.req
        persistence_started = perf_counter()
        self.db_run_id = reserve_simulation_run_to_db(
            user_id=self.user_id,
            period_start=req.period_start,
            period_end=req.period_end,
            initial_capital=self.initial_capital,
        )
        self.persistence_reservation_ms = (perf_counter() - persistence_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="persistenceReservation").observe(
            self.persistence_reservation_ms / 1000
        )
        self.background_tasks.add_task(
            save_simulation_run_to_db,
            user_id=self.user_id,
            period_start=req.period_start,
            period_end=req.period_end,
            initial_capital=self.initial_capital,
            participant_summary=self.participant_summary,
            executed_trades=self.normalized_trades,
            daily_snapshots=self.normalized_snapshots,
            rule_schema=self.rule_schema_dict,
            order_audits=self.order_audits,
            analytics=self.analytics_payload,
            personal_bot_id=self.compiled_bot["personalBotId"] if self.compiled_bot else None,
            simulation_run_id=self.db_run_id,
        )
        report_analytics = dict(self.analytics_payload)
        report_analytics["dailyPrices"] = self.daily_prices
        report_analytics["dailyPerformance"] = self.normalized_snapshots
        report_analytics["ruleSchema"] = self.rule_schema_dict
        report_generation_started = perf_counter()
        self.deterministic_report = SimulationReportGenerator().build_deterministic_report(
            simulation_run_id=self.db_run_id,
            simulated_trades=self.normalized_trades,
            participant_summary=self.participant_summary,
            daily_performance=self.normalized_snapshots,
            analytics=report_analytics,
        )
        self.report_generation_ms = (perf_counter() - report_generation_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="reportGeneration").observe(
            self.report_generation_ms / 1000
        )

    # ---- 8. 원칙 반증 시뮬레이션 + 백그라운드 저장/강화 예약 ----
    def _apply_counterfactuals_and_schedule(self) -> None:
        req = self.req
        # Replay the user's own trades once per violated principle so the report
        # can attribute the gap to a single principle instead of the whole bot.
        counterfactual_started = perf_counter()
        actual_summary = next(
            (item for item in self.participant_summary if item["variantType"] == "ACTUAL_USER"),
            {},
        )
        try:
            build_principle_counterfactuals(
                self.deterministic_report,
                period_start=req.period_start,
                period_end=req.period_end,
                initial_capital=self.initial_capital,
                securities_map=self.securities_map,
                daily_prices=self.daily_prices,
                trading_days=self.trading_days,
                actual_trades=self.user_trades,
                simulated_trades=self.normalized_trades,
                initial_positions=self.initial_positions,
                disclosures_by_date=self.disclosures_by_date,
                baseline_return_percent=actual_summary.get("cumulativeReturnPercent"),
                baseline_mdd_percent=actual_summary.get("mddPercent"),
            )
        except Exception as error:
            logger.warning(
                "Principle counterfactuals skipped for simulation %s",
                self.db_run_id,
                exc_info=error,
            )
        self.counterfactual_ms = (perf_counter() - counterfactual_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="principleCounterfactuals").observe(
            self.counterfactual_ms / 1000
        )

        self.background_tasks.add_task(
            save_simulation_report_to_db,
            self.db_run_id,
            self.deterministic_report,
        )
        self.schedule_report_enrichment(
            self.background_tasks,
            self.db_run_id,
            self.deterministic_report,
        )

    # ---- 9. 응답 조립 ----
    def _build_response(self) -> dict:
        req = self.req
        response_ready_ms = (perf_counter() - self.request_started) * 1000
        SIMULATION_STAGE_LATENCY_SECONDS.labels(stage="responseReady").observe(response_ready_ms / 1000)
        response = {
            "simulationRunId": self.db_run_id,
            "persistenceStatus": "RUNNING",
            "excludedParticipants": self.excluded_participants,
            "accountId": self.account_id,
            "periodStart": req.period_start,
            "periodEnd": req.period_end,
            "initialCapital": self.initial_capital,
            "initialState": self.initial_state,
            "participantSummary": self.participant_summary,
            "personalBotId": self.compiled_bot["personalBotId"] if self.compiled_bot else None,
            "ruleSchema": self.rule_schema_dict,
            "profileSource": {
                "source": "MYSQL",
                "analysisRunId": self.compiled_bot.get("analysisRunId") if self.compiled_bot else None,
                "analysisVersion": self.compiled_bot.get("analysisVersion") if self.compiled_bot else None,
            },
            "ruleCompilation": self.rule_compilation,
            "totalTradesCount": self.executed_trades_count,
            "simulatedTrades": self.normalized_trades,
            "dailySnapshots": self.normalized_snapshots,
            "dailyPerformance": self.normalized_snapshots,
            "orderAudits": self.order_audits,
            "screeningAudits": self.screening_audits,
            "variantMetrics": self.variant_metrics,
            "benchmarks": self.benchmarks,
            "benchmarkData": self.benchmark_data,
            "randomDistribution": self.random_distribution,
            "securityContributions": self.security_contributions,
            "actionContributions": self.action_contributions,
            "divergenceMoments": self.divergence_moments,
            "behaviorPatterns": self.behavior_patterns,
            "actualPrincipleCompliance": self.actual_compliance,
            "positionSnapshots": self.position_snapshots,
            "report_json": self.deterministic_report,
            "reportJson": self.deterministic_report,
            "dataSource": "MYSQL",
            "usesMockData": False,
            "dataQuality": self.data_quality,
            "disclosureDataEnabled": bool(self.disclosure_events),
            "disclosureAnalysis": {
                "totalEvents": len(self.disclosure_events),
                "byModel": self.disclosure_model_counts,
                "historicalBackfillPolicy": "RULE_ONLY",
                "dailyCollectionPolicy": "OPENAI_REQUIRED_NO_FALLBACK",
            },
            "executionPolicy": {
                "actualUser": "DATABASE_ACTUAL_FILL",
                "bots": "NEXT_TRADING_DAY_OPEN",
                "slippageRate": self.engine.SLIPPAGE_RATE,
            },
            "executionTimingMs": {
                "dataLoad": round(self.data_load_ms, 1),
                "backtest": round(self.backtest_ms, 1),
                "monteCarlo500Runs": round(self.monte_carlo_ms, 1),
                "analytics": round(self.analytics_ms, 1),
                "reportGeneration": round(self.report_generation_ms, 1),
                "principleCounterfactuals": round(self.counterfactual_ms, 1),
                "persistenceReservation": round(self.persistence_reservation_ms, 1),
                "responseReady": round(response_ready_ms, 1),
                "cacheHit": False,
            },
        }
        SIMULATION_RUN_CACHE[self.db_run_id] = response
        return response
