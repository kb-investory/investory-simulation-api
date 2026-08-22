"""
================================================================================
[Investory Engine Module] db_persistence.py
================================================================================
■ 역할:
  - 백테스트 시뮬레이션 연산 결과(simulation_runs, simulation_variants,
    simulated_trades, simulation_daily_performance)를 MySQL DB에 저장 및 과거 이력 조회
================================================================================
"""

import json
from datetime import datetime
from typing import Dict, List, Optional
import pymysql
from app.config import settings
from app.modules.simulation.persistence.rationale_snapshots import apply_rationale_type_snapshots

VARIANT_TYPE_TO_API_ID = {
    "ACTUAL_USER": 1,
    "PERSONAL_BOT": 2,
    "FAMOUS_STRATEGY": 3,
    "RANDOM_BOT": 4,
}
SIMULATION_ENGINE_VERSION = "v2.5"


class SimulationPersistenceError(RuntimeError):
    """저장 전제가 갖춰지지 않아 시뮬레이션 실행을 기록할 수 없을 때."""


def _active_principle_set_id(cur, user_id: int) -> int:
    """실행이 어떤 원칙 세트로 판정됐는지 기록하기 위한 조회.

    상수를 쓰면 실행에 남는 원칙 세트가 실제와 달라지고, 그 id가 없는
    데이터베이스에서는 외래키 제약에 걸립니다.
    """
    cur.execute(
        """
        SELECT principle_set_id FROM principle_sets
        WHERE user_id = %s AND set_status = 'ACTIVE'
        ORDER BY principle_set_id DESC LIMIT 1
        """,
        (user_id,),
    )
    row = cur.fetchone()
    if not row:
        raise SimulationPersistenceError(
            f"사용자 {user_id}의 활성 원칙 세트가 없어 시뮬레이션을 기록할 수 없습니다."
        )
    return int(row[0])


def reserve_simulation_run_to_db(
    user_id: int,
    period_start: str,
    period_end: str,
    initial_capital: float,
) -> int:
    """Reserve a run id quickly; detailed persistence completes after the HTTP response."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            principle_set_id = _active_principle_set_id(cur, user_id)
            cur.execute(
                """
                INSERT INTO simulation_runs
                (user_id, principle_set_id, period_start, period_end, initial_capital,
                 simulation_version, engine_version, run_status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', NOW())
                """,
                (user_id, principle_set_id, period_start, period_end, initial_capital,
                 "v1.0", SIMULATION_ENGINE_VERSION),
            )
            simulation_run_id = int(cur.lastrowid)
        conn.commit()
        return simulation_run_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_db_connection():
    """MySQL 데이터베이스 커넥션 생성"""
    return pymysql.connect(
        host=settings.MYSQL_HOST,
        port=settings.MYSQL_PORT,
        user=settings.MYSQL_USER,
        password=settings.MYSQL_PASSWORD,
        database=settings.MYSQL_DB,
        charset='utf8mb4',
        autocommit=False
    )

def save_simulation_run_to_db(
    user_id: int,
    period_start: str,
    period_end: str,
    initial_capital: float,
    participant_summary: List[dict],
    executed_trades: List[dict],
    daily_snapshots: List[dict],
    rule_schema: Optional[dict] = None,
    order_audits: Optional[List[dict]] = None,
    analytics: Optional[dict] = None,
    personal_bot_id: Optional[str] = None,
    simulation_run_id: Optional[int] = None,
) -> Optional[int]:
    """
    시뮬레이션 백테스트 실행 결과를 MySQL DB 4개 테이블에 보존(Persistence)합니다.
    
    저장 테이블:
      1. simulation_runs (세션 실행 이력)
      2. simulation_variants (비교 대조군 봇 목록)
      3. simulated_trades (가상 매매 체결 기록)
      4. simulation_daily_performance (일별 자산 성과 스냅샷)

    반환:
      - 성공 시 자동 생성된 PK (simulation_run_id: int)
      - 실패 시 None 반환
    """
    conn = None
    try:
        conn = get_db_connection()

        with conn.cursor() as cur:
            # 1. simulation_runs 저장 (시뮬레이션 실행 세션 생성)
            if simulation_run_id is None:
                cur.execute(
                    """
                    INSERT INTO simulation_runs
                    (user_id, principle_set_id, period_start, period_end, initial_capital,
                     simulation_version, engine_version, run_status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'RUNNING', NOW())
                    """,
                    (user_id, _active_principle_set_id(cur, user_id), period_start,
                     period_end, initial_capital, "v1.0", SIMULATION_ENGINE_VERSION),
                )
                sim_run_id = int(cur.lastrowid)
            else:
                sim_run_id = int(simulation_run_id)

            cur.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'simulation_runs'
                  AND COLUMN_NAME = 'analytics_json'
                """
            )
            if cur.fetchone()[0]:
                cur.execute(
                    """
                    UPDATE simulation_runs
                    SET analytics_json = %s,
                        market_data_version = %s,
                        engine_version = %s
                    WHERE simulation_run_id = %s
                    """,
                    (json.dumps(analytics or {}, ensure_ascii=False), f"PRICE_THROUGH_{period_end}", SIMULATION_ENGINE_VERSION, sim_run_id),
                )

            # 2. simulation_variants 저장 (4개 비교 참가자 등록)
            variant_type_map = {
                1: ("ACTUAL_USER", "실제 나", None),
                2: (
                    "PERSONAL_BOT",
                    "나의 투자봇 v1",
                    json.dumps({"personalBotId": personal_bot_id, "ruleSchema": rule_schema or {}}, ensure_ascii=False),
                ),
                3: ("FAMOUS_STRATEGY", "우량 가치·품질 퀀트 봇", json.dumps({"strategy": "VALUE_QUALITY"})),
                4: ("RANDOM_BOT", "원숭이 봇", json.dumps({"mcRunCount": 500, "seed": 42}))
            }

            selected_variant_ids = [
                int(item["variantId"])
                for item in participant_summary
                if int(item.get("variantId", 0)) in variant_type_map
            ]
            if not selected_variant_ids:
                raise ValueError("저장할 시뮬레이션 참가자가 없습니다.")

            variant_db_ids = {}
            for vid in selected_variant_ids:
                v_type, v_name, config_json = variant_type_map[vid]
                cur.execute(
                    """
                    INSERT INTO simulation_variants
                    (simulation_run_id, variant_type, variant_name, random_seed, variant_config_json, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    """,
                    (sim_run_id, v_type, v_name, 42 if vid == 4 else None, config_json)
                )
                variant_db_ids[vid] = cur.lastrowid

            cur.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES
                WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'simulation_orders'
                """
            )
            if cur.fetchone()[0]:
                order_rows = []
                for audit in order_audits or []:
                    db_vid = variant_db_ids.get(int(audit.get("simulation_variant_id", 0)))
                    if db_vid is None:
                        continue
                    order_rows.append((
                        db_vid,
                        audit.get("order_id"),
                        audit.get("security_id"),
                        audit.get("action"),
                        audit.get("signal_date"),
                        audit.get("execution_date"),
                        audit.get("requested_quantity", 0.0),
                        audit.get("approved_quantity", 0.0),
                        audit.get("status"),
                        json.dumps(audit.get("reason_codes", []), ensure_ascii=False),
                    ))
                if order_rows:
                    cur.executemany(
                        """
                        INSERT INTO simulation_orders
                        (simulation_variant_id, order_key, security_id, action,
                         signal_date, execution_date, requested_quantity,
                        approved_quantity, order_status, reason_codes_json, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        """,
                        order_rows,
                    )

            # 3. simulated_trades 저장 (참가자별 매매 체결 내역)
            cur.execute("SELECT principle_set_item_id FROM principle_set_items")
            valid_principle_item_ids = set(row[0] for row in cur.fetchall())

            trade_rows = []
            for t in executed_trades:
                orig_vid = t.get("simulationVariantId") or t.get("simulation_variant_id") or t.get("variantId") or 1
                db_vid = variant_db_ids.get(orig_vid)
                if db_vid is None:
                    continue
                sec_id = t.get("securityId") or t.get("security_id") or 101
                side = t.get("tradeSide") or t.get("trade_side") or "BUY"
                traded_at_raw = t.get("tradedAt") or t.get("traded_at") or f"{period_start} 09:00:00"
                traded_at = str(traded_at_raw).replace("T", " ").replace("Z", "").strip()
                qty = float(t.get("quantity", 0.0))
                unit_p = float(t.get("unitPrice") or t.get("unit_price") or 0.0)
                cost = float(t.get("transactionCostAmount") or t.get("transaction_cost_amount") or 0.0)
                reason = t.get("decisionReason") or t.get("decision_reason") or ""
                principle_item_id = t.get("triggeredPrincipleSetItemId") or t.get("triggered_principle_set_item_id")

                if principle_item_id not in valid_principle_item_ids:
                    principle_item_id = None

                trade_rows.append((db_vid, sec_id, principle_item_id, side, traded_at, qty, unit_p, cost, reason))

            if trade_rows:
                cur.executemany(
                    """
                    INSERT INTO simulated_trades
                    (simulation_variant_id, security_id, triggered_principle_set_item_id, trade_side, traded_at, quantity, unit_price, transaction_cost_amount, decision_reason, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    trade_rows,
                )

            # 4. simulation_daily_performance 저장 (일별 성과 스냅샷)
            cur.execute(
                """
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'simulation_daily_performance'
                  AND COLUMN_NAME = 'net_cash_flow'
                """
            )
            has_net_cash_flow = bool(cur.fetchone()[0])
            snapshot_rows = []
            for s in daily_snapshots:
                orig_vid = s.get("simulationVariantId") or s.get("simulation_variant_id") or s.get("variantId") or 1
                db_vid = variant_db_ids.get(orig_vid)
                if db_vid is None:
                    continue
                p_date = s.get("performanceDate") or s.get("performance_date") or s.get("snapshotDate") or period_start
                cash = float(s.get("cashBalance") or s.get("cash_balance") or s.get("cash") or 0.0)
                holdings = float(s.get("holdingsMarketValue") or s.get("holdings_market_value") or 0.0)
                port_val = float(s.get("portfolioValue") or s.get("portfolio_value") or s.get("totalEquity") or 0.0)
                daily_ret = float(s.get("dailyReturn") if "dailyReturn" in s else s.get("daily_return", 0.0))
                cum_ret = float(s.get("cumulativeReturn") if "cumulativeReturn" in s else s.get("cumulative_return", 0.0))
                mdd = float(s.get("drawdownRate") if "drawdownRate" in s else s.get("drawdown_rate", 0.0))
                net_cash_flow = float(s.get("netCashFlow") or s.get("net_cash_flow") or 0.0)

                row = (db_vid, p_date, cash, holdings, port_val, daily_ret, cum_ret, mdd)
                snapshot_rows.append(row + (net_cash_flow,) if has_net_cash_flow else row)

            if snapshot_rows and has_net_cash_flow:
                cur.executemany(
                        """
                        INSERT INTO simulation_daily_performance
                        (simulation_variant_id, performance_date, cash_balance, holdings_market_value,
                         portfolio_value, daily_return, cumulative_return, drawdown_rate, net_cash_flow, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON DUPLICATE KEY UPDATE
                        cash_balance = VALUES(cash_balance), holdings_market_value = VALUES(holdings_market_value),
                        portfolio_value = VALUES(portfolio_value), daily_return = VALUES(daily_return),
                        cumulative_return = VALUES(cumulative_return), drawdown_rate = VALUES(drawdown_rate),
                        net_cash_flow = VALUES(net_cash_flow)
                        """,
                        snapshot_rows,
                    )
            elif snapshot_rows:
                cur.executemany(
                        """
                        INSERT INTO simulation_daily_performance
                        (simulation_variant_id, performance_date, cash_balance, holdings_market_value, portfolio_value, daily_return, cumulative_return, drawdown_rate, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                        ON DUPLICATE KEY UPDATE
                        cash_balance = VALUES(cash_balance), holdings_market_value = VALUES(holdings_market_value),
                        portfolio_value = VALUES(portfolio_value), daily_return = VALUES(daily_return),
                        cumulative_return = VALUES(cumulative_return), drawdown_rate = VALUES(drawdown_rate)
                        """,
                        snapshot_rows,
                    )

            conn.commit()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE simulation_runs
                    SET run_status = 'COMPLETED', completed_at = NOW(), error_message = NULL
                    WHERE simulation_run_id = %s
                    """,
                    (sim_run_id,),
                )
            conn.commit()
            print(f"[DB Persistence Success] Saved simulation_run_id = {sim_run_id} to MySQL DB!")
            return sim_run_id
    except Exception as e:
        if conn:
            conn.rollback()
            if simulation_run_id is not None:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            UPDATE simulation_runs
                            SET run_status = 'FAILED', error_message = %s, completed_at = NOW()
                            WHERE simulation_run_id = %s
                            """,
                            (str(e)[:1000], int(simulation_run_id)),
                        )
                    conn.commit()
                except Exception:
                    conn.rollback()
        print(f"[DB Persistence Error] Failed to save simulation to DB: {e}")
        raise
    finally:
        if conn:
            conn.close()

def get_simulation_history_from_db(user_id: int = 1) -> Optional[List[dict]]:
    """DB에서 해당 사용자의 과거 시뮬레이션 회차 성과 이력을 최신순으로 조회합니다."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 
                    r.simulation_run_id,
                    r.period_start,
                    r.period_end,
                    r.created_at,
                    r.run_status,
                    MAX(CASE WHEN v.variant_type = 'ACTUAL_USER' THEN p.cumulative_return END) as user_return,
                    MAX(CASE WHEN v.variant_type = 'PERSONAL_BOT' THEN p.cumulative_return END) as bot_return
                FROM simulation_runs r
                LEFT JOIN simulation_variants v ON r.simulation_run_id = v.simulation_run_id
                LEFT JOIN simulation_daily_performance p ON v.simulation_variant_id = p.simulation_variant_id
                WHERE r.user_id = %s
                GROUP BY r.simulation_run_id, r.period_start, r.period_end, r.created_at, r.run_status
                ORDER BY r.simulation_run_id DESC
                """,
                (user_id,)
            )
            rows = cur.fetchall()
            if not rows:
                return None

            history = []
            for idx, row in enumerate(rows):
                run_id, p_start, p_end, created_at, run_status, user_ret, bot_ret = row
                date_str = created_at.strftime("%Y.%m.%d") if created_at else p_start.strftime("%Y.%m.%d")
                period_str = f"{p_start.strftime('%Y.%m.%d')} ~ {p_end.strftime('%Y.%m.%d')}"
                version_str = f"v{len(rows) - idx}"

                bot_ret_val = float(bot_ret) if bot_ret is not None else 0.0
                user_ret_val = float(user_ret) if user_ret is not None else 0.0

                history.append({
                    "simulationRunId": run_id,
                    "version": version_str,
                    "date": date_str,
                    "period": period_str,
                    "returnPercent": round(bot_ret_val * 100 if abs(bot_ret_val) <= 2 and bot_ret_val != 0 else bot_ret_val, 1),
                    "actualReturnPercent": round(user_ret_val * 100 if abs(user_ret_val) <= 2 and user_ret_val != 0 else user_ret_val, 1),
                    "status": run_status
                })

            return history
    except Exception as e:
        print(f"[DB Persistence Warning] Failed to fetch simulation history: {e}")
        return None
    finally:
        if conn:
            conn.close()


def get_latest_completed_simulation_id_from_db(user_id: int = 1) -> Optional[int]:
    """Return the latest completed run that has persisted result data."""
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.simulation_run_id
                FROM simulation_runs r
                WHERE r.user_id = %s
                  AND r.run_status = 'COMPLETED'
                  AND EXISTS (
                      SELECT 1
                      FROM simulation_variants v
                      WHERE v.simulation_run_id = r.simulation_run_id
                  )
                  AND EXISTS (
                      SELECT 1
                      FROM simulation_variants v
                      JOIN simulation_daily_performance p
                        ON p.simulation_variant_id = v.simulation_variant_id
                      WHERE v.simulation_run_id = r.simulation_run_id
                  )
                ORDER BY r.simulation_run_id DESC
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
    except Exception as e:
        print(f"[DB Persistence Warning] Failed to fetch latest completed simulation: {e}")
        return None
    finally:
        if conn:
            conn.close()


def find_existing_simulation_from_db(
    user_id: int,
    period_start: str,
    period_end: str,
    initial_capital: float,
    participant_types: Optional[List[str]] = None,
    personal_bot_id: Optional[str] = None,
) -> Optional[dict]:
    """
    동일 조건(user_id, period_start, period_end, initial_capital, participant_types)의 기존 완료 시뮬레이션이 DB에 있으면
    해당 결과를 그대로 반환합니다. 없으면 None 반환.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 1. 동일 조건 run 조회 (가장 최근 것)
            cur.execute(
                """
                SELECT *
                FROM simulation_runs
                WHERE user_id = %s
                  AND period_start = %s
                  AND period_end = %s
                  AND ABS(initial_capital - %s) < 1.0
                  AND run_status = 'COMPLETED'
                ORDER BY simulation_run_id DESC
                LIMIT 1
                """,
                (user_id, period_start, period_end, initial_capital)
            )
            existing_run = cur.fetchone()
            if not existing_run:
                return None
            if existing_run.get("engine_version") != SIMULATION_ENGINE_VERSION:
                print(
                    f"[DB Cache Miss] run_id={existing_run['simulation_run_id']}의 엔진 버전 "
                    f"({existing_run.get('engine_version')})이 현재 버전({SIMULATION_ENGINE_VERSION})과 다릅니다."
                )
                return None

            run_id = existing_run["simulation_run_id"]

            # 2. variants 조회
            cur.execute(
                "SELECT simulation_variant_id, variant_type, variant_name, variant_config_json FROM simulation_variants WHERE simulation_run_id = %s ORDER BY simulation_variant_id",
                (run_id,)
            )
            variants = cur.fetchall()
            if not variants:
                return None
            api_id_by_db_id = {
                v["simulation_variant_id"]: VARIANT_TYPE_TO_API_ID.get(v["variant_type"], v["simulation_variant_id"])
                for v in variants
            }

            if participant_types:
                db_variant_types = set(v["variant_type"] for v in variants)
                req_variant_types = set(participant_types)
                if not req_variant_types.issubset(db_variant_types):
                    print(f"[DB Cache Miss] run_id={run_id}의 봇 구성({db_variant_types})이 요청된 봇 구성({req_variant_types})을 포함하지 않아 캐시를 재사용하지 않습니다.")
                    return None

            if "PERSONAL_BOT" in set(participant_types or []):
                personal_variant = next(
                    (item for item in variants if item["variant_type"] == "PERSONAL_BOT"),
                    None,
                )
                config = personal_variant.get("variant_config_json") if personal_variant else {}
                if isinstance(config, str):
                    try:
                        config = json.loads(config)
                    except json.JSONDecodeError:
                        config = {}
                if not isinstance(config, dict) or config.get("personalBotId") != personal_bot_id:
                    print(
                        f"[DB Cache Miss] run_id={run_id}의 personalBotId가 요청값({personal_bot_id})과 다릅니다."
                    )
                    return None

            print(f"[DB Cache Hit] 동일 조건 기존 시뮬레이션 run_id={run_id} 재사용합니다. (LLM 재실행 없음)")

            # 3. daily performance 조회
            variant_ids = tuple(v["simulation_variant_id"] for v in variants)
            placeholders = ",".join(["%s"] * len(variant_ids))
            cur.execute(
                f"SELECT * FROM simulation_daily_performance WHERE simulation_variant_id IN ({placeholders}) ORDER BY simulation_variant_id, performance_date",
                variant_ids
            )
            all_perf = cur.fetchall()
            if not all_perf:
                print(f"[DB Cache Miss] run_id={run_id}에 일별 성과 데이터(daily_performance)가 없어 캐시를 재사용하지 않습니다.")
                return None

            # 4. simulated_trades 조회
            cur.execute(
                f"""SELECT t.*, s.security_code, s.security_name
                    FROM simulated_trades t
                    JOIN securities s ON s.security_id = t.security_id
                    WHERE t.simulation_variant_id IN ({placeholders})
                    ORDER BY t.traded_at""",
                variant_ids
            )
            all_trades = cur.fetchall()

        # 5. participant_summary 계산
        participant_summary = []
        for v in variants:
            vid = v["simulation_variant_id"]
            api_vid = api_id_by_db_id[vid]
            snaps = [p for p in all_perf if p["simulation_variant_id"] == vid]
            last_snap = snaps[-1] if snaps else {}

            tot_equity = float(last_snap.get("portfolio_value") or initial_capital)
            cum_ret = float(last_snap.get("cumulative_return") or 0.0)
            mdd = min((float(p.get("drawdown_rate") or 0.0) for p in snaps), default=0.0)

            daily_returns = [float(p.get("daily_return") or 0.0) for p in snaps]
            if len(daily_returns) > 1:
                mean_ret = sum(daily_returns) / len(daily_returns)
                var_val = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
                volatility = round((var_val ** 0.5) * (252 ** 0.5) * 100, 1)
            else:
                volatility = 0.0

            participant_summary.append({
                "variantId": api_vid,
                "variantType": v["variant_type"],
                "variantName": v["variant_name"],
                "totalEquity": tot_equity,
                "cumulativeReturnPercent": round(cum_ret * 100, 2),
                "volatilityPercent": volatility,
                "mddPercent": round(mdd * 100, 2)
            })

        # 6. daily snapshots 정규화
        normalized_snapshots = [
            {
                "variantId": api_id_by_db_id[p["simulation_variant_id"]],
                "simulationVariantId": api_id_by_db_id[p["simulation_variant_id"]],
                "simulation_variant_id": api_id_by_db_id[p["simulation_variant_id"]],
                "performanceDate": str(p["performance_date"]),
                "snapshotDate": str(p["performance_date"]),
                "performance_date": str(p["performance_date"]),
                "cashBalance": float(p.get("cash_balance") or 0.0),
                "cash": float(p.get("cash_balance") or 0.0),
                "netCashFlow": float(p.get("net_cash_flow") or 0.0),
                "holdingsMarketValue": float(p.get("holdings_market_value") or 0.0),
                "portfolioValue": float(p.get("portfolio_value") or initial_capital),
                "portfolio_value": float(p.get("portfolio_value") or initial_capital),
                "totalEquity": float(p.get("portfolio_value") or initial_capital),
                "dailyReturn": float(p.get("daily_return") or 0.0),
                "daily_return": float(p.get("daily_return") or 0.0),
                "cumulativeReturn": float(p.get("cumulative_return") or 0.0),
                "cumulative_return": float(p.get("cumulative_return") or 0.0),
                "cumulativeReturnPercent": round(float(p.get("cumulative_return") or 0.0) * 100, 2),
                "drawdownRate": float(p.get("drawdown_rate") or 0.0),
                "drawdown_rate": float(p.get("drawdown_rate") or 0.0),
                "mddPercent": round(float(p.get("drawdown_rate") or 0.0) * 100, 2)
            }
            for p in all_perf
        ]

        # 7. trades 정규화
        normalized_trades = [
            {
                "simulatedTradeId": t["simulated_trade_id"],
                "simulated_trade_id": t["simulated_trade_id"],
                "simulationVariantId": api_id_by_db_id[t["simulation_variant_id"]],
                "variantId": api_id_by_db_id[t["simulation_variant_id"]],
                "securityId": t["security_id"],
                "securityCode": t.get("security_code") or "",
                "securityName": t.get("security_name") or f"종목 {t['security_id']}",
                "tradeSide": t["trade_side"],
                "tradedAt": str(t["traded_at"]),
                "quantity": float(t.get("quantity") or 0.0),
                "unitPrice": float(t.get("unit_price") or 0.0),
                "transactionCostAmount": float(t.get("transaction_cost_amount") or 0.0),
                "decisionReason": t.get("decision_reason") or "",
                "triggeredPrincipleSetItemId": t.get("triggered_principle_set_item_id")
            }
            for t in all_trades
        ]

        report_obj = None
        if existing_run.get("report_json"):
            try:
                report_obj = json.loads(existing_run["report_json"]) if isinstance(existing_run["report_json"], str) else existing_run["report_json"]
            except Exception:
                report_obj = None

        analytics_obj = {}
        if existing_run.get("analytics_json"):
            try:
                analytics_obj = (
                    json.loads(existing_run["analytics_json"])
                    if isinstance(existing_run["analytics_json"], str)
                    else existing_run["analytics_json"]
                ) or {}
            except Exception:
                analytics_obj = {}
        apply_rationale_type_snapshots(
            normalized_trades,
            analytics_obj.get("rationaleTypeSnapshots") or [],
        )

        personal_variant = next(
            (item for item in variants if item.get("variant_type") == "PERSONAL_BOT"),
            {},
        )
        rule_schema = personal_variant.get("variant_config_json") or {}
        if isinstance(rule_schema, str):
            try:
                rule_schema = json.loads(rule_schema)
            except Exception:
                rule_schema = {}
        personal_bot_id = rule_schema.get("personalBotId") if isinstance(rule_schema, dict) else None
        if isinstance(rule_schema, dict) and "ruleSchema" in rule_schema:
            rule_schema = rule_schema["ruleSchema"]

        result = {
            "simulationRunId": run_id,
            "periodStart": str(existing_run["period_start"]),
            "periodEnd": str(existing_run["period_end"]),
            "initialCapital": float(existing_run["initial_capital"]),
            "participantSummary": participant_summary,
            "personalBotId": personal_bot_id,
            "ruleSchema": rule_schema,
            "totalTradesCount": len(normalized_trades),
            "simulatedTrades": normalized_trades,
            "dailySnapshots": normalized_snapshots,
            "dailyPerformance": normalized_snapshots,
            "report_json": report_obj,
            "reportJson": report_obj,
            "_fromCache": True
        }
        result.update(analytics_obj)
        return result

    except Exception as e:
        print(f"[DB Cache Warning] 기존 시뮬레이션 DB 조회 실패: {e}")
        return None
    finally:
        if conn:
            conn.close()


def load_simulation_from_db_by_id(simulation_run_id: int) -> Optional[dict]:
    """
    simulation_run_id로 DB에서 직접 시뮬레이션 전체 데이터를 조회합니다.
    서버 재시작 후 인메모리 캐시가 없을 때 리포트 엔드포인트에서 호출됩니다.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # 1. simulation_run 조회
            cur.execute(
                """
                SELECT *
                FROM simulation_runs
                WHERE simulation_run_id = %s
                """,
                (simulation_run_id,)
            )
            run = cur.fetchone()
            if not run:
                print(f"[DB] simulation_run_id={simulation_run_id} 조회 결과 없음")
                return None

            initial_capital = float(run["initial_capital"])

            # 2. variants 조회
            cur.execute(
                "SELECT simulation_variant_id, variant_type, variant_name, variant_config_json FROM simulation_variants WHERE simulation_run_id = %s ORDER BY simulation_variant_id",
                (simulation_run_id,)
            )
            variants = cur.fetchall()
            if not variants:
                return None
            api_id_by_db_id = {
                v["simulation_variant_id"]: VARIANT_TYPE_TO_API_ID.get(v["variant_type"], v["simulation_variant_id"])
                for v in variants
            }

            # 3. daily performance 조회
            variant_ids = tuple(v["simulation_variant_id"] for v in variants)
            placeholders = ",".join(["%s"] * len(variant_ids))
            cur.execute(
                f"SELECT * FROM simulation_daily_performance WHERE simulation_variant_id IN ({placeholders}) ORDER BY simulation_variant_id, performance_date",
                variant_ids
            )
            all_perf = cur.fetchall()

            # 4. simulated_trades 조회
            cur.execute(
                f"""SELECT t.*, s.security_code, s.security_name
                    FROM simulated_trades t
                    JOIN securities s ON s.security_id = t.security_id
                    WHERE t.simulation_variant_id IN ({placeholders})
                    ORDER BY t.traded_at""",
                variant_ids
            )
            all_trades = cur.fetchall()

        # 5. participant_summary 계산
        participant_summary = []
        for v in variants:
            vid = v["simulation_variant_id"]
            api_vid = api_id_by_db_id[vid]
            snaps = [p for p in all_perf if p["simulation_variant_id"] == vid]
            last_snap = snaps[-1] if snaps else {}

            tot_equity = float(last_snap.get("portfolio_value") or initial_capital)
            cum_ret = float(last_snap.get("cumulative_return") or 0.0)
            mdd = min((float(p.get("drawdown_rate") or 0.0) for p in snaps), default=0.0)

            daily_returns = [float(p.get("daily_return") or 0.0) for p in snaps]
            if len(daily_returns) > 1:
                mean_ret = sum(daily_returns) / len(daily_returns)
                var_val = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
                volatility = round((var_val ** 0.5) * (252 ** 0.5) * 100, 1)
            else:
                volatility = 0.0

            participant_summary.append({
                "variantId": api_vid,
                "variantType": v["variant_type"],
                "variantName": v["variant_name"],
                "totalEquity": tot_equity,
                "cumulativeReturnPercent": round(cum_ret * 100, 2),
                "volatilityPercent": volatility,
                "mddPercent": round(mdd * 100, 2)
            })

        # 6. daily snapshots 정규화
        normalized_snapshots = [
            {
                "variantId": api_id_by_db_id[p["simulation_variant_id"]],
                "simulationVariantId": api_id_by_db_id[p["simulation_variant_id"]],
                "simulation_variant_id": api_id_by_db_id[p["simulation_variant_id"]],
                "performanceDate": str(p["performance_date"]),
                "snapshotDate": str(p["performance_date"]),
                "performance_date": str(p["performance_date"]),
                "cashBalance": float(p.get("cash_balance") or 0.0),
                "cash": float(p.get("cash_balance") or 0.0),
                "netCashFlow": float(p.get("net_cash_flow") or 0.0),
                "holdingsMarketValue": float(p.get("holdings_market_value") or 0.0),
                "portfolioValue": float(p.get("portfolio_value") or initial_capital),
                "portfolio_value": float(p.get("portfolio_value") or initial_capital),
                "totalEquity": float(p.get("portfolio_value") or initial_capital),
                "dailyReturn": float(p.get("daily_return") or 0.0),
                "daily_return": float(p.get("daily_return") or 0.0),
                "cumulativeReturn": float(p.get("cumulative_return") or 0.0),
                "cumulative_return": float(p.get("cumulative_return") or 0.0),
                "cumulativeReturnPercent": round(float(p.get("cumulative_return") or 0.0) * 100, 2),
                "drawdownRate": float(p.get("drawdown_rate") or 0.0),
                "drawdown_rate": float(p.get("drawdown_rate") or 0.0),
                "mddPercent": round(float(p.get("drawdown_rate") or 0.0) * 100, 2)
            }
            for p in all_perf
        ]

        # 7. trades 정규화
        normalized_trades = [
            {
                "simulatedTradeId": t["simulated_trade_id"],
                "simulated_trade_id": t["simulated_trade_id"],
                "simulationVariantId": api_id_by_db_id[t["simulation_variant_id"]],
                "variantId": api_id_by_db_id[t["simulation_variant_id"]],
                "securityId": t["security_id"],
                "securityCode": t.get("security_code") or "",
                "securityName": t.get("security_name") or f"종목 {t['security_id']}",
                "tradeSide": t["trade_side"],
                "tradedAt": str(t["traded_at"]),
                "quantity": float(t.get("quantity") or 0.0),
                "unitPrice": float(t.get("unit_price") or 0.0),
                "transactionCostAmount": float(t.get("transaction_cost_amount") or 0.0),
                "decisionReason": t.get("decision_reason") or "",
                "triggeredPrincipleSetItemId": t.get("triggered_principle_set_item_id")
            }
            for t in all_trades
        ]

        report_obj = None
        if run.get("report_json"):
            try:
                report_obj = json.loads(run["report_json"]) if isinstance(run["report_json"], str) else run["report_json"]
            except Exception:
                report_obj = None

        analytics_obj = {}
        if run.get("analytics_json"):
            try:
                analytics_obj = (
                    json.loads(run["analytics_json"])
                    if isinstance(run["analytics_json"], str)
                    else run["analytics_json"]
                ) or {}
            except Exception:
                analytics_obj = {}
        apply_rationale_type_snapshots(
            normalized_trades,
            analytics_obj.get("rationaleTypeSnapshots") or [],
        )

        personal_variant = next(
            (item for item in variants if item.get("variant_type") == "PERSONAL_BOT"),
            {},
        )
        rule_schema = personal_variant.get("variant_config_json") or {}
        if isinstance(rule_schema, str):
            try:
                rule_schema = json.loads(rule_schema)
            except Exception:
                rule_schema = {}
        personal_bot_id = rule_schema.get("personalBotId") if isinstance(rule_schema, dict) else None
        if isinstance(rule_schema, dict) and "ruleSchema" in rule_schema:
            rule_schema = rule_schema["ruleSchema"]

        print(f"[DB] simulation_run_id={simulation_run_id} 로드 완료 (trades={len(normalized_trades)}, perf={len(normalized_snapshots)})")
        result = {
            "simulationRunId": simulation_run_id,
            "periodStart": str(run["period_start"]),
            "periodEnd": str(run["period_end"]),
            "initialCapital": initial_capital,
            "participantSummary": participant_summary,
            "personalBotId": personal_bot_id,
            "ruleSchema": rule_schema,
            "totalTradesCount": len(normalized_trades),
            "simulatedTrades": normalized_trades,
            "dailySnapshots": normalized_snapshots,
            "dailyPerformance": normalized_snapshots,
            "report_json": report_obj,
            "reportJson": report_obj,
            "_fromDb": True
        }
        result.update(analytics_obj)
        return result

    except Exception as e:
        print(f"[DB] load_simulation_from_db_by_id({simulation_run_id}) 실패: {e}")
        return None
    finally:
        if conn:
            conn.close()


def save_simulation_report_to_db(simulation_run_id: int, report_data: dict) -> bool:
    """
    생성된 AI 성과 복기 리포트(report_data dict)를 simulation_runs 테이블의 report_json 컬럼에 저장/업데이트합니다.
    """
    conn = None
    try:
        conn = get_db_connection()
        report_json_str = json.dumps(report_data, ensure_ascii=False)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE simulation_runs SET report_json = %s WHERE simulation_run_id = %s",
                (report_json_str, simulation_run_id)
            )
            conn.commit()
            print(f"[DB Persistence Success] Saved report_json to simulation_run_id = {simulation_run_id}")
            return True
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[DB Persistence Warning] Failed to save report_json for run_id={simulation_run_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()
