import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.endpoints.simulation import (
    COMPILE_JOB_CACHE,
    compile_simulation_bot,
    get_comparator_bots,
    get_compile_job_status,
)
from app.api.endpoints.simulation_helpers import RuleCompileRequest
from app.modules.simulation.analytics.comparator_details import build_comparators, personal_rules
from app.modules.simulation.persistence.repository import SimulationDataError


RULE_SCHEMA = {
    "selection": {
        "factor_weights": {"growth": 0.4, "disclosure": 0.3, "value": 0.2, "quality": 0.1},
        "min_passing_score": 70,
    },
    "additional_buy": {"allowed": False, "max_additional_count": 9},
    "portfolio": {"max_position_count": 5, "max_single_position_weight": 0.2},
    "exit": {"take_profit_rate": 0.2, "stop_loss_rate": -0.1},
    "audit": {"ai_confidence": 0.78},
}

BOT = {
    "personalBotId": "PBOT_1003",
    "botVersion": 3,
    "analysisRunId": 43,
    "analysisVersion": 3,
    "ruleSchema": RULE_SCHEMA,
    "ruleCompilation": {"source": "OPENAI", "compiledAt": "2026-08-12T10:30:00+09:00"},
    "createdAt": "2026-08-12T10:30:00+09:00",
}

PRINCIPLES = [
    {"principleSetItemId": 32, "principleText": "단일 종목 비중은 20%를 넘지 않는다.", "sortOrder": 2},
    {"principleSetItemId": 31, "principleText": "급등 종목은 신규 진입하지 않는다.", "sortOrder": 1},
]

EVIDENCE = {
    "tradeCount": 34,
    "journalCount": 18,
    "confirmedPrincipleCount": 2,
    "actualUpdatedAt": "2026-08-12T10:30:00+09:00",
    "updatedAt": "2026-08-12T10:30:00+09:00",
    "analyzedSecurityCount": 120,
    "systemUpdatedAt": "2026-08-11T16:30:00+09:00",
}


class FakeRepository:
    def resolve_account_id(self, user_id, requested_account_id=None):
        return requested_account_id or 27

    def load_principles(self, user_id):
        return PRINCIPLES

    def load_latest_investor_profile(self, user_id):
        return {"analysisRunId": 43, "analysisVersion": 3, "axes": {}}

    def find_compiled_personal_bot_by_input_hash(self, user_id, input_hash):
        return dict(BOT)

    def load_compiled_personal_bot(self, user_id, personal_bot_id=None):
        if personal_bot_id and personal_bot_id != BOT["personalBotId"]:
            raise SimulationDataError(
                "PERSONAL_BOT_NOT_COMPILED",
                "저장된 개인 투자봇이 없습니다.",
                {"personalBotId": personal_bot_id},
            )
        return dict(BOT)

    def load_comparator_evidence(self, user_id, account_id):
        return dict(EVIDENCE)


class NoPersonalBotRepository(FakeRepository):
    """No personal bot has ever been compiled for this user (personalBotId omitted)."""

    def load_compiled_personal_bot(self, user_id, personal_bot_id=None):
        raise SimulationDataError(
            "PERSONAL_BOT_NOT_COMPILED",
            "저장된 개인 투자봇이 없습니다. 먼저 투자봇 생성 API를 실행해 주세요.",
            {"personalBotId": personal_bot_id} if personal_bot_id else {},
        )


class SimulationComparatorDetailTests(unittest.TestCase):
    def setUp(self):
        COMPILE_JOB_CACHE.clear()

    def test_all_four_comparators_have_complete_detail_contract(self):
        result = build_comparators(BOT, PRINCIPLES, EVIDENCE)

        self.assertEqual([item["variantId"] for item in result], [1, 2, 3, 4])
        self.assertEqual(
            [item["variantType"] for item in result],
            ["ACTUAL_USER", "PERSONAL_BOT", "FAMOUS_STRATEGY", "RANDOM_BOT"],
        )
        for item in result:
            self.assertIsInstance(item["principles"], list)
            self.assertIsInstance(item["rules"], list)
            self.assertIsInstance(item["dataEvidence"], dict)
            for rule in item["rules"]:
                if "rawValue" in rule:
                    self.assertTrue(
                        rule["rawValue"] is None
                        or isinstance(rule["rawValue"], (str, int, float, bool))
                        or (
                            isinstance(rule["rawValue"], list)
                            and all(isinstance(value, str) for value in rule["rawValue"])
                        )
                    )

        personal = result[1]
        self.assertEqual(personal["personalBotId"], "PBOT_1003")
        self.assertEqual([item["principleId"] for item in personal["principles"]], [31, 32])
        self.assertEqual(personal["confidencePercent"], 78)
        ratio = next(item for item in personal["rules"] if item["key"] == "portfolio.maxPositionWeight")
        self.assertEqual(ratio["rawValue"], 0.2)
        self.assertEqual(ratio["value"], "최대 20%")
        self.assertFalse(any(item["key"].startswith("additionalBuy.") for item in personal["rules"]))

    def test_missing_data_uses_empty_arrays_instead_of_null(self):
        result = build_comparators({**BOT, "ruleSchema": {}}, [], {})
        self.assertEqual(result[0]["principles"], [])
        self.assertEqual(result[1]["principles"], [])
        self.assertEqual(result[1]["rules"], [])
        self.assertEqual(result[1]["traits"], [])

    def test_rule_order_is_deterministic(self):
        first = personal_rules(RULE_SCHEMA)
        second = personal_rules(dict(reversed(list(RULE_SCHEMA.items()))))
        self.assertEqual(first, second)

    @patch("app.api.endpoints.simulation.SimulationRepository", return_value=FakeRepository())
    def test_unknown_requested_personal_bot_returns_404(self, repository_class):
        with self.assertRaises(HTTPException) as context:
            get_comparator_bots(personalBotId="DOES_NOT_EXIST", user_id=1)
        self.assertEqual(context.exception.status_code, 404)
        self.assertEqual(context.exception.detail["code"], "PERSONAL_BOT_NOT_COMPILED")

    @patch("app.api.endpoints.simulation.SimulationRepository", return_value=NoPersonalBotRepository())
    def test_uncompiled_personal_bot_degrades_to_a_placeholder_instead_of_failing_the_list(
        self, repository_class,
    ):
        result = get_comparator_bots(user_id=1)

        self.assertEqual(len(result), 4)
        self.assertEqual([item["variantId"] for item in result], [1, 2, 3, 4])
        for item in result:
            if item["variantId"] != 2:
                self.assertEqual(item["availability"], "AVAILABLE")
        personal_slot = result[1]
        self.assertEqual(personal_slot["availability"], "NOT_COMPILED")
        self.assertIsNone(personal_slot["personalBotId"])
        self.assertEqual(personal_slot["unavailableReason"]["code"], "PERSONAL_BOT_NOT_COMPILED")

    @patch("app.api.endpoints.simulation.SimulationRepository", return_value=FakeRepository())
    def test_completed_compile_detail_matches_comparator_and_polling(self, repository_class):
        compiled = compile_simulation_bot(RuleCompileRequest(actualTrades=[]), user_id=1)
        comparators = get_comparator_bots(personalBotId="PBOT_1003", user_id=1)
        polled = get_compile_job_status(compiled["jobId"], user_id=1)

        self.assertEqual(compiled["botDetail"], comparators[1])
        self.assertEqual(polled["botDetail"], comparators[1])
        self.assertEqual(compiled["progressPercent"], 100)

    def test_korean_json_is_utf8_safe(self):
        payload = json.dumps(build_comparators(BOT, PRINCIPLES, EVIDENCE), ensure_ascii=False).encode("utf-8")
        decoded = payload.decode("utf-8")
        self.assertIn("나의 투자봇", decoded)
        self.assertNotIn("\\u", decoded)


if __name__ == "__main__":
    unittest.main()
