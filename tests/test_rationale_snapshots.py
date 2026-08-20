import unittest

from app.modules.simulation.persistence.rationale_snapshots import (
    apply_rationale_type_snapshots,
    build_rationale_type_snapshots,
)


class RationaleSnapshotTests(unittest.TestCase):
    def test_snapshot_restores_label_after_database_id_and_datetime_format_change(self):
        original = [{
            "tradeId": 11,
            "variantId": 1,
            "securityId": 7,
            "tradeSide": "BUY",
            "tradedAt": "2026-07-01T09:00:00Z",
            "quantity": 3,
            "unitPrice": 12500,
            "decisionReason": "신규 계약 공시 확인",
            "rationaleLabelType": "EVENT_REACTION",
        }]
        reloaded = [{
            "simulatedTradeId": 9876,
            "variantId": 1,
            "securityId": 7,
            "tradeSide": "BUY",
            "tradedAt": "2026-07-01 09:00:00",
            "quantity": 3.0,
            "unitPrice": 12500.0,
            "decisionReason": "신규 계약 공시 확인",
        }]

        snapshots = build_rationale_type_snapshots(original)
        restored = apply_rationale_type_snapshots(reloaded, snapshots)

        self.assertEqual(restored, 1)
        self.assertEqual(reloaded[0]["rationaleLabelType"], "EVENT_REACTION")
        self.assertNotIn("tradeId", snapshots[0])

    def test_snapshot_skips_comparison_bots_and_unclassified_actual_trades(self):
        snapshots = build_rationale_type_snapshots([
            {
                "variantId": 1,
                "securityId": 1,
                "tradeSide": "BUY",
                "rationaleLabelType": "UNCLASSIFIED",
            },
            {
                "variantId": 2,
                "securityId": 1,
                "tradeSide": "BUY",
                "rationaleLabelType": "FUNDAMENTAL_ANALYSIS",
            },
        ])

        self.assertEqual(snapshots, [])


if __name__ == "__main__":
    unittest.main()
