"""MySQL-backed input repository for the simulation engine.

The API layer consumes only the normalized dictionaries returned here.  No
runtime path in this module falls back to bundled JSON fixtures.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional

from app.modules.simulation.db_persistence import get_db_connection


class SimulationDataError(RuntimeError):
    """Raised when required database-backed simulation inputs are unavailable."""

    def __init__(self, code: str, message: str, details: Optional[dict] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _ReusableConnection:
    """Delegate a request-scoped connection while ignoring per-method close calls."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def close(self):
        return None

    def close_underlying(self):
        self.connection.close()


def _date_text(value) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _datetime_text(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value).replace(" ", "T").rstrip("Z") + "Z"


class SimulationRepository:
    """Loads simulation inputs from MySQL and normalizes their field names."""

    def __init__(self, connection_factory: Callable = get_db_connection, reuse_connection: bool = False):
        self._shared_connection = None
        if reuse_connection:
            self._shared_connection = _ReusableConnection(connection_factory())
            self.connection_factory = lambda: self._shared_connection
        else:
            self.connection_factory = connection_factory

    def close(self):
        if self._shared_connection is not None:
            self._shared_connection.close_underlying()
            self._shared_connection = None

    def resolve_account_id(self, user_id: int, requested_account_id: Optional[int] = None) -> int:
        """Resolve a connected account owned by the authenticated user."""
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ia.account_id
                    FROM investment_accounts ia
                    JOIN broker_connections bc ON bc.connection_id = ia.connection_id
                    WHERE bc.user_id = %s
                      AND bc.connection_status = 'CONNECTED'
                      AND (%s IS NULL OR ia.account_id = %s)
                    ORDER BY
                      (
                          SELECT COUNT(*)
                          FROM holding_snapshots hs
                          WHERE hs.account_id = ia.account_id
                      ) DESC,
                      (
                          SELECT MAX(hs.snapshot_date)
                          FROM holding_snapshots hs
                          WHERE hs.account_id = ia.account_id
                      ) DESC,
                      bc.connected_at DESC,
                      ia.account_id DESC
                    LIMIT 1
                    """,
                    (user_id, requested_account_id, requested_account_id),
                )
                row = cur.fetchone()
        finally:
            conn.close()

        if not row:
            raise SimulationDataError(
                "CONNECTED_ACCOUNT_NOT_FOUND",
                "요청한 사용자의 연결된 투자 계좌를 찾을 수 없습니다.",
                {"userId": user_id, "accountId": requested_account_id},
            )
        return int(row[0])

    def load_securities(self) -> List[dict]:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT security_id, security_code, security_name, market_type,
                           sector_name, industry_name, listed_date, delisted_date, is_active
                    FROM securities
                    ORDER BY security_id
                    """
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            raise SimulationDataError("SECURITIES_NOT_FOUND", "종목 마스터 데이터가 없습니다.")

        return [
            {
                "securityId": row[0],
                "securityCode": row[1],
                "securityName": row[2],
                "marketType": row[3],
                "sectorName": row[4],
                "industryName": row[5],
                "listedDate": _date_text(row[6]) if row[6] else None,
                "delistedDate": _date_text(row[7]) if row[7] else None,
                "isActive": bool(row[8]),
            }
            for row in rows
        ]

    def load_daily_prices(self, period_start: str, period_end: str) -> List[dict]:
        preload_start = (
            datetime.strptime(period_start, "%Y-%m-%d").date() - timedelta(days=40)
        ).isoformat()
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT security_id, price_date, open_price, high_price, low_price,
                           close_price, daily_return_rate, trading_volume, trading_value
                    FROM security_daily_prices
                    WHERE price_date BETWEEN %s AND %s
                    ORDER BY price_date, security_id
                    """,
                    (preload_start, period_end),
                )
                rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'security_fundamentals_daily'
                    """
                )
                has_fundamentals = bool(cur.fetchone()[0])
                fundamental_rows = []
                if has_fundamentals:
                    cur.execute(
                        """
                        SELECT security_id, effective_date, shares_outstanding,
                               per, pbr, roe, debt_ratio, revenue_growth,
                               earnings_growth, operating_cash_flow_positive,
                               annualized_net_income, total_equity
                        FROM security_fundamentals_daily
                        WHERE effective_date <= %s
                        ORDER BY security_id, effective_date
                        """,
                        (period_end,),
                    )
                    fundamental_rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            raise SimulationDataError(
                "PRICE_DATA_NOT_FOUND",
                "요청 기간에 가격 데이터가 없습니다.",
                {"periodStart": period_start, "periodEnd": period_end},
            )

        fundamentals_by_security: Dict[int, List[tuple]] = defaultdict(list)
        for item in fundamental_rows:
            fundamentals_by_security[int(item[0])].append(item)

        history: Dict[int, deque] = defaultdict(lambda: deque(maxlen=20))
        result = []
        for row in rows:
            security_id = int(row[0])
            close_price = float(row[5])
            closes = history[security_id]
            day5_return = None
            if len(closes) >= 5 and closes[0] > 0:
                reference = list(closes)[-5]
                day5_return = (close_price - reference) / reference if reference > 0 else None
            moving_average_5 = sum(list(closes)[-4:] + [close_price]) / 5 if len(closes) >= 4 else None
            moving_average_20 = sum(list(closes)[-19:] + [close_price]) / 20 if len(closes) >= 19 else None
            closes.append(close_price)
            price_date = _date_text(row[1])
            if price_date < period_start:
                continue

            effective_fundamental = None
            for candidate in fundamentals_by_security.get(security_id, []):
                if _date_text(candidate[1]) <= price_date:
                    effective_fundamental = candidate
                else:
                    break
            fundamentals = {}
            if effective_fundamental:
                shares = float(effective_fundamental[2] or 0.0)
                market_cap = close_price * shares if shares > 0 else None
                annualized_net_income = (
                    float(effective_fundamental[10]) if effective_fundamental[10] is not None else None
                )
                total_equity = (
                    float(effective_fundamental[11]) if effective_fundamental[11] is not None else None
                )
                point_in_time_per = (
                    market_cap / annualized_net_income
                    if market_cap is not None and annualized_net_income is not None and annualized_net_income > 0
                    else (float(effective_fundamental[3]) if effective_fundamental[3] is not None else None)
                )
                point_in_time_pbr = (
                    market_cap / total_equity
                    if market_cap is not None and total_equity is not None and total_equity > 0
                    else (float(effective_fundamental[4]) if effective_fundamental[4] is not None else None)
                )
                fundamentals = {
                    "fundamentalsEffectiveDate": _date_text(effective_fundamental[1]),
                    "sharesOutstanding": shares,
                    "marketCap": market_cap,
                    "per": point_in_time_per,
                    "pbr": point_in_time_pbr,
                    "roe": float(effective_fundamental[5]) if effective_fundamental[5] is not None else None,
                    "debtRatio": float(effective_fundamental[6]) if effective_fundamental[6] is not None else None,
                    "revenueGrowth": float(effective_fundamental[7]) if effective_fundamental[7] is not None else None,
                    "earningsGrowth": float(effective_fundamental[8]) if effective_fundamental[8] is not None else None,
                    "operatingCashFlowPositive": bool(effective_fundamental[9]) if effective_fundamental[9] is not None else None,
                }
            result.append(
                {
                    "securityId": security_id,
                    "priceDate": _date_text(row[1]),
                    "openPrice": float(row[2]),
                    "highPrice": float(row[3]),
                    "lowPrice": float(row[4]),
                    "closePrice": close_price,
                    "dailyReturnRate": float(row[6] or 0.0),
                    "changeRate": float(row[6] or 0.0),
                    "day5Return": day5_return,
                    "movingAverage5": moving_average_5,
                    "movingAverage20": moving_average_20,
                    "tradingVolume": int(row[7] or 0),
                    "tradingValue": float(row[8] or 0.0),
                    **fundamentals,
                }
            )
        return result

    def load_actual_trades(self, account_id: int, period_start: str, period_end: str) -> List[dict]:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT t.trade_id, t.security_id, s.security_code, s.security_name,
                           t.trade_side, t.quantity, t.unit_price,
                           t.transaction_cost_amount, t.traded_at,
                           note.rationale_text, note.rationale_label_type
                    FROM trades t
                    JOIN securities s ON s.security_id = t.security_id
                    LEFT JOIN journal_trade_notes note ON note.trade_id = t.trade_id
                    WHERE t.account_id = %s
                      AND DATE(t.traded_at) BETWEEN %s AND %s
                    ORDER BY t.traded_at, t.trade_id
                    """,
                    (account_id, period_start, period_end),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        return [
            {
                "tradeId": row[0],
                "securityId": row[1],
                "securityCode": row[2],
                "securityName": row[3],
                "tradeSide": row[4],
                "quantity": float(row[5]),
                "unitPrice": float(row[6]),
                "transactionCostAmount": float(row[7] or 0.0),
                "tradedAt": _datetime_text(row[8]),
                "rationaleText": row[9] or "",
                "rationaleLabelType": row[10] or "UNCLASSIFIED",
            }
            for row in rows
        ]

    def save_compiled_personal_bot(
        self,
        user_id: int,
        rule_schema: dict,
        profile: dict,
        compilation_metadata: dict,
        input_hash: str,
    ) -> dict:
        """Persist one immutable compiled strategy version for later pure backtests."""
        conn = self.connection_factory()
        personal_bot_id = f"BOT_{uuid.uuid4().hex[:12].upper()}"
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(bot_version), 0) + 1 FROM personal_bot_versions WHERE user_id = %s",
                    (user_id,),
                )
                bot_version = int(cur.fetchone()[0])
                cur.execute(
                    """
                    INSERT INTO personal_bot_versions
                        (personal_bot_id, user_id, bot_version, analysis_run_id,
                         analysis_version, rule_schema_json,
                         compilation_metadata_json, input_hash, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        personal_bot_id,
                        user_id,
                        bot_version,
                        profile.get("analysisRunId"),
                        profile.get("analysisVersion"),
                        json.dumps(rule_schema, ensure_ascii=False),
                        json.dumps(compilation_metadata or {}, ensure_ascii=False),
                        input_hash,
                    ),
                )
            conn.commit()
        except Exception as save_error:
            conn.rollback()
            # A concurrent request may have persisted the same fingerprint
            # after our cache lookup but before this insert. The unique index
            # remains the source of truth, so return that immutable version.
            try:
                existing_bot = self.find_compiled_personal_bot_by_input_hash(user_id, input_hash)
            except Exception:
                raise save_error
            if existing_bot:
                existing_bot["reusedExisting"] = True
                return existing_bot
            raise
        finally:
            conn.close()
        return {
            "personalBotId": personal_bot_id,
            "botVersion": bot_version,
            "ruleSchema": rule_schema,
            "analysisRunId": profile.get("analysisRunId"),
            "analysisVersion": profile.get("analysisVersion"),
            "ruleCompilation": compilation_metadata or {},
            "inputHash": input_hash,
        }

    def find_compiled_personal_bot_by_input_hash(self, user_id: int, input_hash: str) -> Optional[dict]:
        """Return the immutable bot compiled from exactly the same semantic input."""
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT personal_bot_id
                    FROM personal_bot_versions
                    WHERE user_id = %s AND input_hash = %s
                    LIMIT 1
                    """,
                    (user_id, input_hash),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            return None
        result = self.load_compiled_personal_bot(user_id, str(row[0]))
        result["inputHash"] = input_hash
        return result

    def load_compiled_personal_bot(self, user_id: int, personal_bot_id: Optional[str] = None) -> dict:
        """Load an exact bot version, or the user's latest compiled version when omitted."""
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                if personal_bot_id:
                    cur.execute(
                        """
                        SELECT personal_bot_id, bot_version, analysis_run_id,
                               analysis_version, rule_schema_json,
                               compilation_metadata_json, created_at
                        FROM personal_bot_versions
                        WHERE user_id = %s AND personal_bot_id = %s
                        """,
                        (user_id, personal_bot_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT personal_bot_id, bot_version, analysis_run_id,
                               analysis_version, rule_schema_json,
                               compilation_metadata_json, created_at
                        FROM personal_bot_versions
                        WHERE user_id = %s
                        ORDER BY bot_version DESC, created_at DESC
                        LIMIT 1
                        """,
                        (user_id,),
                    )
                row = cur.fetchone()
        finally:
            conn.close()
        if not row:
            raise SimulationDataError(
                "PERSONAL_BOT_NOT_COMPILED",
                "저장된 개인 투자봇이 없습니다. 먼저 투자봇 생성 API를 실행해 주세요.",
                {"personalBotId": personal_bot_id} if personal_bot_id else {},
            )
        rule_schema = row[4]
        if isinstance(rule_schema, str):
            rule_schema = json.loads(rule_schema)
        # 초기 레거시 데이터와 향후 확장된 variant config를 모두 읽습니다.
        if isinstance(rule_schema, dict) and "ruleSchema" in rule_schema:
            rule_schema = rule_schema["ruleSchema"]
        metadata = row[5] or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return {
            "personalBotId": row[0],
            "botVersion": int(row[1]),
            "analysisRunId": row[2],
            "analysisVersion": row[3],
            "ruleSchema": rule_schema,
            "ruleCompilation": metadata,
            "createdAt": _datetime_text(row[6]) if len(row) > 6 and row[6] else None,
        }

    def load_comparator_evidence(self, user_id: int, account_id: int) -> dict:
        """Return real aggregate counts used by comparator detail cards."""
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*), MAX(traded_at) FROM trades WHERE account_id = %s",
                    (account_id,),
                )
                trade_row = cur.fetchone() or (0, None)
                cur.execute(
                    "SELECT COUNT(*), MAX(updated_at) FROM investment_journals WHERE user_id = %s",
                    (user_id,),
                )
                journal_row = cur.fetchone() or (0, None)
                cur.execute(
                    """
                    SELECT COUNT(*), MAX(pset.updated_at)
                    FROM principle_set_items item
                    JOIN principle_sets pset ON pset.principle_set_id = item.principle_set_id
                    WHERE pset.user_id = %s AND pset.set_status = 'ACTIVE'
                    """,
                    (user_id,),
                )
                principle_row = cur.fetchone() or (0, None)
                cur.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE()
                      AND TABLE_NAME = 'security_fundamentals_daily'
                    """
                )
                if cur.fetchone()[0]:
                    cur.execute(
                        """
                        SELECT COUNT(DISTINCT fundamentals.security_id), MAX(fundamentals.created_at)
                        FROM security_fundamentals_daily fundamentals
                        JOIN securities s ON s.security_id = fundamentals.security_id
                        WHERE s.is_active = 1
                        """
                    )
                    security_row = cur.fetchone() or (0, None)
                else:
                    security_row = (0, None)
        finally:
            conn.close()

        timestamps = [value for value in (trade_row[1], journal_row[1], principle_row[1]) if value]
        return {
            "tradeCount": int(trade_row[0] or 0),
            "journalCount": int(journal_row[0] or 0),
            "confirmedPrincipleCount": int(principle_row[0] or 0),
            "actualUpdatedAt": _datetime_text(max(timestamps)) if timestamps else None,
            "updatedAt": _datetime_text(max(timestamps)) if timestamps else None,
            "analyzedSecurityCount": int(security_row[0] or 0),
            "systemUpdatedAt": _datetime_text(security_row[1]) if security_row[1] else None,
        }

    def load_market_index_prices(self, period_start: str, period_end: str) -> List[dict]:
        """KOSPI/KOSDAQ 지수 종가를 불러오며, 미적재 환경에서는 빈 목록을 반환한다."""
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'market_index_daily_prices'
                    """
                )
                if not cur.fetchone()[0]:
                    return []
                cur.execute(
                    """
                    SELECT index_code, price_date, close_price
                    FROM market_index_daily_prices
                    WHERE price_date BETWEEN %s AND %s
                      AND index_code IN ('KOSPI', 'KOSDAQ')
                    ORDER BY index_code, price_date
                    """,
                    (period_start, period_end),
                )
                rows = cur.fetchall()
        finally:
            conn.close()
        return [
            {"indexCode": row[0], "priceDate": _date_text(row[1]), "closePrice": float(row[2])}
            for row in rows
        ]

    def load_principles(self, user_id: int) -> List[dict]:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT item.principle_set_item_id, item.principle_text,
                           item.rule_json, item.sort_order
                    FROM principle_set_items item
                    JOIN principle_sets pset
                      ON pset.principle_set_id = item.principle_set_id
                    WHERE pset.user_id = %s AND pset.set_status = 'ACTIVE'
                    ORDER BY item.sort_order, item.principle_set_item_id
                    """,
                    (user_id,),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            raise SimulationDataError("PRINCIPLES_NOT_FOUND", "활성 투자 원칙이 없습니다.")

        result = []
        for row in rows:
            rule_json = row[2] or {}
            if isinstance(rule_json, str):
                try:
                    rule_json = json.loads(rule_json)
                except json.JSONDecodeError:
                    rule_json = {}
            result.append(
                {
                    "principleSetItemId": row[0],
                    "principleText": row[1],
                    "ruleJson": rule_json,
                    "sortOrder": row[3],
                }
            )
        return result

    def load_latest_investor_profile(self, user_id: int) -> dict:
        """Load the latest persisted six-axis investor profile for compilation."""
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT analysis_run_id, period_start, period_end, trade_count,
                           journal_count, analysis_version
                    FROM analysis_runs ar
                    WHERE ar.user_id = %s
                      AND 6 = (
                          SELECT COUNT(DISTINCT res.analysis_dimension_code)
                          FROM analysis_results res
                          WHERE res.analysis_run_id = ar.analysis_run_id
                      )
                    ORDER BY created_at DESC, analysis_run_id DESC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                run = cur.fetchone()
                if not run:
                    raise SimulationDataError(
                        "INVESTOR_PROFILE_NOT_FOUND",
                        "저장된 투자 성향 분석 결과가 없습니다.",
                        {"userId": user_id},
                    )
                cur.execute(
                    """
                    SELECT r.analysis_dimension_code, d.analysis_dimension_name,
                           r.primary_analysis_type_code, t.analysis_type_name,
                           r.evidence_json
                    FROM analysis_results r
                    JOIN analysis_dimensions d
                      ON d.analysis_dimension_code = r.analysis_dimension_code
                    JOIN analysis_types t
                      ON t.analysis_type_code = r.primary_analysis_type_code
                    WHERE r.analysis_run_id = %s
                    ORDER BY d.sort_order, r.analysis_result_id
                    """,
                    (run[0],),
                )
                rows = cur.fetchall()
                cur.execute(
                    """
                    SELECT combination_summary, strength_summary, caution_summary
                    FROM analysis_run_summaries
                    WHERE analysis_run_id = %s
                    """,
                    (run[0],),
                )
                summary = cur.fetchone()
        finally:
            conn.close()

        if len(rows) != 6:
            raise SimulationDataError(
                "INVESTOR_PROFILE_INCOMPLETE",
                "최신 투자 성향 결과가 6개 축을 모두 포함하지 않습니다.",
                {"analysisRunId": run[0], "axisCount": len(rows)},
            )

        axes = {}
        for row in rows:
            evidence = row[4] or {}
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except json.JSONDecodeError:
                    evidence = {}
            axes[row[0]] = {
                "dimensionName": row[1],
                "typeCode": row[2],
                "typeName": row[3],
                "score": int(evidence.get("score", 0)),
                "summary": evidence.get("summary", ""),
                "evidence": evidence,
            }

        return {
            "analysisRunId": int(run[0]),
            "periodStart": _date_text(run[1]),
            "periodEnd": _date_text(run[2]),
            "tradeCount": int(run[3]),
            "journalCount": int(run[4]),
            "analysisVersion": run[5],
            "combinationSummary": summary[0] if summary else "",
            "strengthSummary": summary[1] if summary else "",
            "cautionSummary": summary[2] if summary else "",
            "axes": axes,
        }

    def load_initial_snapshot(self, account_id: int, period_start: str) -> dict:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT MAX(snapshot_date)
                    FROM holding_snapshots
                    WHERE account_id = %s AND snapshot_date < %s
                    """,
                    (account_id, period_start),
                )
                row = cur.fetchone()
                snapshot_date = row[0] if row else None
                if not snapshot_date:
                    raise SimulationDataError(
                        "INITIAL_SNAPSHOT_NOT_FOUND",
                        "시뮬레이션 시작일 이전의 보유 스냅샷이 없습니다.",
                        {"accountId": account_id, "periodStart": period_start},
                    )
                cur.execute(
                    """
                    SELECT h.security_id, s.security_code, s.security_name,
                           h.quantity, h.average_cost, h.market_value, h.unrealized_pnl
                    FROM holding_snapshots h
                    JOIN securities s ON s.security_id = h.security_id
                    WHERE h.account_id = %s AND h.snapshot_date = %s
                    ORDER BY h.security_id
                    """,
                    (account_id, snapshot_date),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        holdings = [
            {
                "securityId": row[0],
                "securityCode": row[1],
                "securityName": row[2],
                "quantity": float(row[3]),
                "averageCost": float(row[4]),
                "marketValue": float(row[5]),
                "unrealizedPnl": float(row[6]),
            }
            for row in rows
            if float(row[3]) > 0
        ]
        initial_capital = sum(item["marketValue"] for item in holdings)
        if initial_capital <= 0:
            raise SimulationDataError(
                "INITIAL_CAPITAL_EMPTY",
                "시작 스냅샷의 보유 평가금액이 0원입니다.",
                {"snapshotDate": _date_text(snapshot_date)},
            )
        return {
            "snapshotDate": _date_text(snapshot_date),
            "accountId": account_id,
            "initialCapital": round(initial_capital, 2),
            "holdings": holdings,
            "holdingsCount": len(holdings),
            "calculationPolicy": "PREVIOUS_TRADING_DAY_HOLDING_SNAPSHOT",
        }

    def load_disclosures(self, period_start: str, period_end: str) -> List[dict]:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM INFORMATION_SCHEMA.TABLES
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'disclosure_events'
                    """
                )
                if not cur.fetchone()[0]:
                    return []
                cur.execute(
                    """
                    SELECT disclosure_event_id, security_id, receipt_no, report_name,
                           event_type, event_date, available_at, direction,
                           impact_score, confidence, analysis_reason, analysis_model
                    FROM disclosure_events
                    WHERE event_date BETWEEN %s AND %s
                    ORDER BY available_at, disclosure_event_id
                    """,
                    (period_start, period_end),
                )
                rows = cur.fetchall()
        finally:
            conn.close()

        return [
            {
                "eventId": row[0],
                "securityId": row[1],
                "receiptNo": row[2],
                "reportName": row[3],
                "eventType": row[4],
                "eventDate": _date_text(row[5]),
                "availableAt": _datetime_text(row[6]),
                "direction": row[7],
                "impactScore": float(row[8]),
                "confidence": float(row[9] or 0.0),
                "analysisReason": row[10] or "",
                "analysisModel": row[11] or "UNKNOWN",
            }
            for row in rows
        ]

    def load_overview(self, user_id: int, account_id: int) -> dict:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MIN(journal_date), MAX(journal_date), COUNT(DISTINCT journal_date) FROM investment_journals WHERE user_id = %s",
                    (user_id,),
                )
                journal = cur.fetchone()
                cur.execute(
                    "SELECT COUNT(*) FROM broker_connections WHERE user_id = %s AND connection_status = 'CONNECTED'",
                    (user_id,),
                )
                connections = int(cur.fetchone()[0])
                cur.execute("SELECT COUNT(*) FROM simulation_runs WHERE user_id = %s", (user_id,))
                simulations = int(cur.fetchone()[0])
                cur.execute(
                    "SELECT MIN(price_date), MAX(price_date), COUNT(DISTINCT price_date), COUNT(DISTINCT security_id) FROM security_daily_prices"
                )
                prices = cur.fetchone()
                cur.execute(
                    "SELECT MIN(snapshot_date) FROM holding_snapshots WHERE account_id = %s",
                    (account_id,),
                )
                first_snapshot = cur.fetchone()[0]
                first_runnable_date = None
                if first_snapshot:
                    cur.execute(
                        "SELECT MIN(price_date) FROM security_daily_prices WHERE price_date > %s",
                        (first_snapshot,),
                    )
                    first_runnable_date = cur.fetchone()[0]
        finally:
            conn.close()

        journal_start = journal[0] if journal and journal[0] else None
        journal_end = journal[1] if journal and journal[1] else None
        eligible_start = max(
            (value for value in (journal_start, first_runnable_date) if value is not None),
            default=None,
        )
        eligible_end = min(
            (value for value in (journal_end, prices[1] if prices else None) if value is not None),
            default=None,
        )
        return {
            "eligibleStartDate": _date_text(eligible_start) if eligible_start else None,
            "eligibleEndDate": _date_text(eligible_end) if eligible_end else None,
            "journalDays": int(journal[2] or 0) if journal else 0,
            "connectedAccountsCount": connections,
            "recentSimulationCount": simulations,
            "priceStartDate": _date_text(prices[0]) if prices and prices[0] else None,
            "priceEndDate": _date_text(prices[1]) if prices and prices[1] else None,
            "tradingDayCount": int(prices[2] or 0) if prices else 0,
            "securityCount": int(prices[3] or 0) if prices else 0,
            "accountId": account_id,
        }

    def assess_trade_price_quality(self, account_id: int, period_start: str, period_end: str) -> dict:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*),
                           SUM(CASE WHEN p.security_id IS NOT NULL THEN 1 ELSE 0 END),
                           SUM(CASE WHEN p.security_id IS NULL THEN 1 ELSE 0 END),
                           SUM(CASE WHEN p.security_id IS NOT NULL
                                     AND t.unit_price BETWEEN p.low_price AND p.high_price
                                    THEN 1 ELSE 0 END)
                    FROM trades t
                    LEFT JOIN security_daily_prices p
                      ON p.security_id = t.security_id
                     AND p.price_date = DATE(t.traded_at)
                    WHERE t.account_id = %s
                      AND DATE(t.traded_at) BETWEEN %s AND %s
                    """,
                    (account_id, period_start, period_end),
                )
                row = cur.fetchone()
        finally:
            conn.close()

        total = int(row[0] or 0)
        same_day = int(row[1] or 0)
        unmatched = int(row[2] or 0)
        in_range = int(row[3] or 0)
        limited = unmatched > 0 or in_range < same_day
        return {
            "level": "LIMITED" if limited else "GOOD",
            "isMarketConsistent": not limited,
            "totalTrades": total,
            "sameDayPriceMatched": same_day,
            "unmatchedTradeDates": unmatched,
            "dailyRangeMatched": in_range,
            "message": (
                "일부 실제 거래의 체결일·체결가가 일별 시세와 일치하지 않아 비교 결과에 오차가 있을 수 있습니다."
                if limited
                else "실제 거래의 체결일·체결가가 일별 시세 범위와 일치합니다."
            ),
        }
