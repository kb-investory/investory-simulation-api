"""Historical holding snapshot reconstruction and persistence."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Callable, Dict, Iterable, List

from app.modules.simulation.persistence.db_persistence import get_db_connection


class SnapshotReconstructionError(RuntimeError):
    pass


def _as_date(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()


def reconstruct_holding_snapshots(
    anchor_date,
    anchor_holdings: Iterable[dict],
    trades: Iterable[dict],
    daily_prices: Iterable[dict],
) -> List[dict]:
    """Reconstruct end-of-day quantities before an authoritative anchor snapshot."""

    anchor = _as_date(anchor_date)
    anchor_qty = defaultdict(float)
    anchor_avg = {}
    for holding in anchor_holdings:
        security_id = int(holding["securityId"])
        anchor_qty[security_id] = float(holding["quantity"])
        anchor_avg[security_id] = float(holding.get("averageCost", 0.0))

    normalized_trades = []
    buy_history = defaultdict(list)
    for trade in trades:
        trade_date = _as_date(trade["tradedAt"])
        if trade_date > anchor:
            continue
        item = {
            "securityId": int(trade["securityId"]),
            "tradeDate": trade_date,
            "tradeSide": str(trade["tradeSide"]).upper(),
            "quantity": float(trade["quantity"]),
            "unitPrice": float(trade["unitPrice"]),
        }
        normalized_trades.append(item)
        if item["tradeSide"] == "BUY":
            buy_history[item["securityId"]].append(item)

    price_map: Dict[date, Dict[int, float]] = defaultdict(dict)
    for price in daily_prices:
        price_date = _as_date(price["priceDate"])
        if price_date < anchor:
            price_map[price_date][int(price["securityId"])] = float(price["closePrice"])

    snapshots = []
    all_security_ids = set(anchor_qty)
    all_security_ids.update(item["securityId"] for item in normalized_trades)

    for snapshot_date in sorted(price_map):
        for security_id in sorted(all_security_ids):
            quantity = anchor_qty[security_id]
            for trade in normalized_trades:
                if trade["securityId"] != security_id or trade["tradeDate"] <= snapshot_date:
                    continue
                if trade["tradeSide"] == "BUY":
                    quantity -= trade["quantity"]
                elif trade["tradeSide"] == "SELL":
                    quantity += trade["quantity"]

            if quantity < -1e-6:
                raise SnapshotReconstructionError(
                    f"security_id={security_id}, snapshot_date={snapshot_date}: 역산 보유 수량이 음수입니다."
                )
            if quantity <= 1e-6 or security_id not in price_map[snapshot_date]:
                continue

            relevant_buys = [
                trade
                for trade in buy_history[security_id]
                if trade["tradeDate"] <= snapshot_date
            ]
            bought_qty = sum(trade["quantity"] for trade in relevant_buys)
            if bought_qty > 0:
                average_cost = sum(
                    trade["quantity"] * trade["unitPrice"] for trade in relevant_buys
                ) / bought_qty
                cost_quality = "ESTIMATED_FROM_TRADES"
            elif anchor_avg.get(security_id, 0.0) > 0:
                average_cost = anchor_avg[security_id]
                cost_quality = "ANCHOR_CARRIED"
            else:
                average_cost = price_map[snapshot_date][security_id]
                cost_quality = "DAILY_CLOSE_FALLBACK"

            close_price = price_map[snapshot_date][security_id]
            market_value = quantity * close_price
            snapshots.append(
                {
                    "securityId": security_id,
                    "snapshotDate": snapshot_date.strftime("%Y-%m-%d"),
                    "quantity": round(quantity, 4),
                    "averageCost": round(average_cost, 4),
                    "marketValue": round(market_value, 2),
                    "unrealizedPnl": round(market_value - quantity * average_cost, 2),
                    "quantityQuality": "RECONSTRUCTED",
                    "costBasisQuality": cost_quality,
                }
            )
    return snapshots


def reconstruct_holding_snapshots_forward(
    trades: Iterable[dict],
    daily_prices: Iterable[dict],
) -> dict:
    """Build end-of-day snapshots by replaying trades over DB trading days.

    Trade dates before the first available daily price are also included so a
    newly connected account starts on its actual first trade date. Until daily
    closes become available, the latest execution price is carried forward.
    A sell without enough reconstructed quantity is recorded as a data-quality
    adjustment rather than creating a negative position.
    """

    price_map: Dict[date, Dict[int, float]] = defaultdict(dict)
    for price in daily_prices:
        price_map[_as_date(price["priceDate"])][int(price["securityId"])] = float(
            price["closePrice"]
        )
    normalized_trades = sorted(
        [
            {
                "tradeId": trade.get("tradeId"),
                "securityId": int(trade["securityId"]),
                "tradeDate": _as_date(trade["tradedAt"]),
                "tradeSide": str(trade["tradeSide"]).upper(),
                "quantity": float(trade["quantity"]),
                "unitPrice": float(trade["unitPrice"]),
            }
            for trade in trades
        ],
        key=lambda item: (item["tradeDate"], item.get("tradeId") or 0),
    )
    trading_days = sorted(price_map)
    if not trading_days:
        return {"snapshots": [], "adjustments": []}

    first_price_date = trading_days[0]
    pre_price_trade_days = {
        trade["tradeDate"] for trade in normalized_trades if trade["tradeDate"] < first_price_date
    }
    snapshot_days = sorted(set(trading_days).union(pre_price_trade_days))

    positions: Dict[int, dict] = {}
    latest_prices: Dict[int, float] = {}
    snapshots = []
    adjustments = []
    trade_index = 0

    for trading_day in snapshot_days:
        while (
            trade_index < len(normalized_trades)
            and normalized_trades[trade_index]["tradeDate"] <= trading_day
        ):
            trade = normalized_trades[trade_index]
            security_id = trade["securityId"]
            position = positions.setdefault(security_id, {"quantity": 0.0, "averageCost": 0.0})
            quantity = trade["quantity"]
            latest_prices[security_id] = trade["unitPrice"]
            if trade["tradeSide"] == "BUY":
                new_quantity = position["quantity"] + quantity
                position["averageCost"] = (
                    position["quantity"] * position["averageCost"]
                    + quantity * trade["unitPrice"]
                ) / new_quantity
                position["quantity"] = new_quantity
            elif trade["tradeSide"] == "SELL":
                if quantity > position["quantity"] + 1e-6:
                    adjustments.append(
                        {
                            "tradeId": trade.get("tradeId"),
                            "securityId": security_id,
                            "originalTradeDate": trade["tradeDate"].strftime("%Y-%m-%d"),
                            "appliedTradingDate": trading_day.strftime("%Y-%m-%d"),
                            "type": "SELL_QUANTITY_EXCEEDS_RECONSTRUCTED_HOLDING",
                            "shortfallQuantity": round(quantity - position["quantity"], 4),
                        }
                    )
                    position["quantity"] = 0.0
                else:
                    position["quantity"] -= quantity
                if position["quantity"] <= 1e-6:
                    positions.pop(security_id, None)
            trade_index += 1

        latest_prices.update(price_map.get(trading_day, {}))

        for security_id, position in sorted(positions.items()):
            close_price = latest_prices.get(security_id)
            if close_price is None or position["quantity"] <= 1e-6:
                continue
            market_value = position["quantity"] * close_price
            snapshots.append(
                {
                    "securityId": security_id,
                    "snapshotDate": trading_day.strftime("%Y-%m-%d"),
                    "quantity": round(position["quantity"], 4),
                    "averageCost": round(position["averageCost"], 4),
                    "marketValue": round(market_value, 2),
                    "unrealizedPnl": round(
                        market_value - position["quantity"] * position["averageCost"], 2
                    ),
                    "quantityQuality": "RECONSTRUCTED_FROM_TRADES",
                    "costBasisQuality": "MOVING_AVERAGE_FROM_TRADES",
                }
            )
    return {"snapshots": snapshots, "adjustments": adjustments}


class HoldingSnapshotBackfillService:
    def __init__(self, connection_factory: Callable = get_db_connection):
        self.connection_factory = connection_factory

    def build(self, account_id: int) -> dict:
        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT MAX(snapshot_date) FROM holding_snapshots WHERE account_id = %s",
                    (account_id,),
                )
                row = cur.fetchone()
                anchor_date = row[0] if row else None
                if not anchor_date:
                    raise SnapshotReconstructionError("기준 보유 스냅샷이 없습니다.")

                cur.execute(
                    """
                    SELECT security_id, quantity, average_cost
                    FROM holding_snapshots
                    WHERE account_id = %s AND snapshot_date = %s
                    """,
                    (account_id, anchor_date),
                )
                anchor_holdings = [
                    {"securityId": row[0], "quantity": row[1], "averageCost": row[2]}
                    for row in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT trade_id, security_id, trade_side, traded_at, quantity, unit_price
                    FROM trades
                    WHERE account_id = %s
                    ORDER BY traded_at, trade_id
                    """,
                    (account_id,),
                )
                trades = [
                    {
                        "tradeId": row[0],
                        "securityId": row[1],
                        "tradeSide": row[2],
                        "tradedAt": row[3],
                        "quantity": row[4],
                        "unitPrice": row[5],
                    }
                    for row in cur.fetchall()
                ]
                cur.execute(
                    """
                    SELECT security_id, price_date, close_price
                    FROM security_daily_prices
                    WHERE price_date <= (SELECT MAX(price_date) FROM security_daily_prices)
                    ORDER BY price_date, security_id
                    """,
                )
                prices = [
                    {"securityId": row[0], "priceDate": row[1], "closePrice": row[2]}
                    for row in cur.fetchall()
                ]
        finally:
            conn.close()

        reconstructed = reconstruct_holding_snapshots_forward(trades, prices)
        snapshots = reconstructed["snapshots"]
        return {
            "accountId": account_id,
            "anchorDate": _as_date(anchor_date).strftime("%Y-%m-%d"),
            "snapshots": snapshots,
            "snapshotRows": len(snapshots),
            "snapshotDates": len({item["snapshotDate"] for item in snapshots}),
            "qualityAdjustments": reconstructed["adjustments"],
            "qualityAdjustmentCount": len(reconstructed["adjustments"]),
        }

    def persist(self, account_id: int, snapshots: Iterable[dict]) -> int:
        conn = self.connection_factory()
        affected = 0
        try:
            with conn.cursor() as cur:
                for item in snapshots:
                    cur.execute(
                        """
                        SELECT holding_snapshot_id
                        FROM holding_snapshots
                        WHERE account_id = %s AND security_id = %s AND snapshot_date = %s
                        LIMIT 1
                        """,
                        (account_id, item["securityId"], item["snapshotDate"]),
                    )
                    row = cur.fetchone()
                    values = (
                        item["quantity"],
                        item["averageCost"],
                        item["marketValue"],
                        item["unrealizedPnl"],
                    )
                    if row:
                        cur.execute(
                            """
                            UPDATE holding_snapshots
                            SET quantity = %s, average_cost = %s, market_value = %s,
                                unrealized_pnl = %s
                            WHERE holding_snapshot_id = %s
                            """,
                            values + (row[0],),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO holding_snapshots
                            (account_id, security_id, snapshot_date, quantity,
                             average_cost, market_value, unrealized_pnl, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                            """,
                            (
                                account_id,
                                item["securityId"],
                                item["snapshotDate"],
                            ) + values,
                        )
                    affected += 1
            conn.commit()
            return affected
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def replace_account_snapshots(self, account_id: int, snapshots: Iterable[dict]) -> dict:
        """Atomically replace one account's test snapshots with reconstructed rows."""

        rows = list(snapshots)
        if not rows:
            raise SnapshotReconstructionError("교체할 보유 스냅샷이 없습니다.")
        if any(float(item["quantity"]) <= 0 or float(item["marketValue"]) < 0 for item in rows):
            raise SnapshotReconstructionError("수량 또는 평가금액이 유효하지 않은 스냅샷이 있습니다.")

        conn = self.connection_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM holding_snapshots WHERE account_id = %s",
                    (account_id,),
                )
                previous_count = int(cur.fetchone()[0])
                cur.execute("DELETE FROM holding_snapshots WHERE account_id = %s", (account_id,))
                cur.executemany(
                    """
                    INSERT INTO holding_snapshots
                    (account_id, security_id, snapshot_date, quantity,
                     average_cost, market_value, unrealized_pnl, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    [
                        (
                            account_id,
                            item["securityId"],
                            item["snapshotDate"],
                            item["quantity"],
                            item["averageCost"],
                            item["marketValue"],
                            item["unrealizedPnl"],
                        )
                        for item in rows
                    ],
                )
                cur.execute(
                    """
                    SELECT COUNT(*), COUNT(DISTINCT snapshot_date),
                           MIN(snapshot_date), MAX(snapshot_date)
                    FROM holding_snapshots
                    WHERE account_id = %s
                    """,
                    (account_id,),
                )
                verification = cur.fetchone()
                if int(verification[0]) != len(rows):
                    raise SnapshotReconstructionError("스냅샷 교체 후 행 수 검증에 실패했습니다.")
            conn.commit()
            return {
                "accountId": account_id,
                "previousRows": previous_count,
                "insertedRows": int(verification[0]),
                "snapshotDates": int(verification[1]),
                "startDate": _as_date(verification[2]).strftime("%Y-%m-%d"),
                "endDate": _as_date(verification[3]).strftime("%Y-%m-%d"),
            }
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
