"""Persist rationale labels inside analytics JSON without a simulation-table migration."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from typing import Iterable, List


ACTUAL_VARIANT_IDS = {1, 1001}


def _variant_id(trade: dict) -> int:
    return int(
        trade.get("variantId")
        or trade.get("simulationVariantId")
        or trade.get("simulation_variant_id")
        or 0
    )


def _datetime_key(value) -> str:
    return str(value or "").replace("T", " ").replace("Z", "").strip()[:19]


def _number_key(value) -> str:
    return f"{float(value or 0.0):.4f}"


def rationale_snapshot_key(trade: dict) -> str:
    """Create a stable key that survives simulated_trade_id reassignment by MySQL."""
    identity = {
        "variantId": _variant_id(trade),
        "securityId": int(trade.get("securityId") or trade.get("security_id") or 0),
        "tradeSide": str(trade.get("tradeSide") or trade.get("trade_side") or ""),
        "tradedAt": _datetime_key(trade.get("tradedAt") or trade.get("traded_at")),
        "quantity": _number_key(trade.get("quantity")),
        "unitPrice": _number_key(trade.get("unitPrice") or trade.get("unit_price")),
        "decisionReason": str(
            trade.get("decisionReason")
            or trade.get("decision_reason")
            or trade.get("rationaleText")
            or ""
        ),
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_rationale_type_snapshots(trades: Iterable[dict]) -> List[dict]:
    snapshots = []
    for trade in trades:
        if _variant_id(trade) not in ACTUAL_VARIANT_IDS:
            continue
        label_type = str(
            trade.get("rationaleLabelType")
            or trade.get("rationale_label_type")
            or "UNCLASSIFIED"
        )
        if label_type == "UNCLASSIFIED":
            continue
        snapshots.append({
            "matchKey": rationale_snapshot_key(trade),
            "rationaleLabelType": label_type,
        })
    return snapshots


def apply_rationale_type_snapshots(trades: Iterable[dict], snapshots: Iterable[dict]) -> int:
    labels_by_key = defaultdict(deque)
    for snapshot in snapshots or []:
        if not isinstance(snapshot, dict):
            continue
        match_key = str(snapshot.get("matchKey") or "")
        label_type = str(snapshot.get("rationaleLabelType") or "UNCLASSIFIED")
        if match_key and label_type != "UNCLASSIFIED":
            labels_by_key[match_key].append(label_type)

    restored = 0
    for trade in trades:
        labels = labels_by_key.get(rationale_snapshot_key(trade))
        if not labels:
            trade.setdefault("rationaleLabelType", "UNCLASSIFIED")
            continue
        label_type = labels.popleft()
        trade["rationaleLabelType"] = label_type
        trade["rationale_label_type"] = label_type
        restored += 1
    return restored
