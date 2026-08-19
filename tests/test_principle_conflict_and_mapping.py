import unittest

from app.modules.simulation.compiler import AIRuleCompiler
from app.modules.simulation.report_analysis import DeterministicReportAnalyzer
from app.modules.simulation.rule_schema import (
    InvestmentBotStrategySchema,
    executable_rule_paths,
)


class MappedRuleNormalizationTests(unittest.TestCase):
    def _audit(self, items):
        return {"audit": {"interpreted_principles": items}}

    def test_a_valid_dotted_path_is_kept(self):
        data = self._audit([
            {"user_natural_text": "급등주 금지", "ai_mapped_rule": "entry.max_5day_return",
             "status": "CONFIRMED", "unmappable_reason": ""},
        ])

        AIRuleCompiler._normalize_mapped_rules(data)
        item = data["audit"]["interpreted_principles"][0]

        self.assertEqual(item["ai_mapped_rule"], "entry.max_5day_return")
        self.assertEqual(item["status"], "CONFIRMED")

    def test_a_prose_answer_is_demoted_instead_of_passing_as_executable(self):
        # Left alone this stays CONFIRMED and then fails every trade check
        # silently, while the screen claims the principle is being enforced.
        data = self._audit([
            {"user_natural_text": "급등주 금지",
             "ai_mapped_rule": "급등주에 대한 추격매수 금지 원칙 적용(트렌드 기반)",
             "status": "CONFIRMED", "unmappable_reason": ""},
        ])

        AIRuleCompiler._normalize_mapped_rules(data)
        item = data["audit"]["interpreted_principles"][0]

        self.assertEqual(item["ai_mapped_rule"], "")
        self.assertEqual(item["status"], "REVIEW_REQUIRED")
        self.assertIn("유효하지 않아", item["unmappable_reason"])

    def test_a_plausible_but_nonexistent_path_is_also_demoted(self):
        data = self._audit([
            {"user_natural_text": "실적 발표 전 매수 금지",
             "ai_mapped_rule": "entry.avoid_before_earnings",
             "status": "CONFIRMED", "unmappable_reason": ""},
        ])

        AIRuleCompiler._normalize_mapped_rules(data)

        self.assertEqual(data["audit"]["interpreted_principles"][0]["status"], "REVIEW_REQUIRED")

    def test_an_existing_unmappable_reason_is_not_overwritten(self):
        data = self._audit([
            {"user_natural_text": "실적 발표 전 매수 금지", "ai_mapped_rule": "",
             "status": "REVIEW_REQUIRED",
             "unmappable_reason": "실적 발표일 데이터가 시스템에 없습니다."},
        ])

        AIRuleCompiler._normalize_mapped_rules(data)

        self.assertEqual(
            data["audit"]["interpreted_principles"][0]["unmappable_reason"],
            "실적 발표일 데이터가 시스템에 없습니다.",
        )

    def test_the_prompt_lists_exactly_the_paths_the_schema_defines(self):
        from app.modules.simulation.prompts import SYSTEM_COMPILER_PROMPT

        missing = [path for path in executable_rule_paths() if path not in SYSTEM_COMPILER_PROMPT]

        self.assertEqual(missing, [], f"프롬프트에 없는 규칙 경로: {missing}")


class ConflictAnchoringTests(unittest.TestCase):
    def test_only_conflicts_between_real_user_principles_survive(self):
        principles = ["물타기로 평단을 낮춘다", "10% 빠지면 손절한다"]
        data = {"audit": {"principle_conflicts": [
            {"first_principle_text": "물타기로 평단을 낮춘다",
             "second_principle_text": "10% 빠지면 손절한다",
             "conflict_type": "CONTRADICTION", "reason": "같은 국면에서 반대 행동"},
            # Text the user never wrote: the model composing, not observing.
            {"first_principle_text": "분산투자를 한다",
             "second_principle_text": "10% 빠지면 손절한다",
             "conflict_type": "OVERLAP", "reason": "지어낸 충돌"},
            # A principle cannot conflict with itself.
            {"first_principle_text": "물타기로 평단을 낮춘다",
             "second_principle_text": "물타기로 평단을 낮춘다",
             "conflict_type": "OVERLAP", "reason": "자기 자신"},
        ]}}

        AIRuleCompiler._drop_unanchored_conflicts(data, principles)
        kept = data["audit"]["principle_conflicts"]

        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["conflict_type"], "CONTRADICTION")


class DiagnosticsSurfaceTests(unittest.TestCase):
    def _report(self, audit_extra):
        analytics = {
            "ruleSchema": {
                "additional_buy": {"trigger_drop_rate": -0.05},
                "exit": {"stop_loss_rate": -0.10},
                "audit": {
                    "interpreted_principles": [
                        {"user_natural_text": "물타기로 평단을 낮춘다",
                         "ai_mapped_rule": "additional_buy.trigger_drop_rate",
                         "status": "CONFIRMED", "unmappable_reason": ""},
                        {"user_natural_text": "10% 빠지면 손절한다",
                         "ai_mapped_rule": "exit.stop_loss_rate",
                         "status": "CONFIRMED", "unmappable_reason": ""},
                        {"user_natural_text": "실적 발표 전에는 사지 않는다",
                         "ai_mapped_rule": "", "status": "REVIEW_REQUIRED",
                         "unmappable_reason": "실적 발표일 데이터가 시스템에 없습니다."},
                    ],
                    **audit_extra,
                },
            },
            "principleItems": [
                {"principleSetItemId": 1, "principleText": "물타기로 평단을 낮춘다",
                 "ruleJson": {}, "sortOrder": 1},
                {"principleSetItemId": 2, "principleText": "10% 빠지면 손절한다",
                 "ruleJson": {}, "sortOrder": 2},
                {"principleSetItemId": 3, "principleText": "실적 발표 전에는 사지 않는다",
                 "ruleJson": {}, "sortOrder": 3},
            ],
            "dailyPrices": [],
        }
        participants = [
            {"variantId": 1, "variantType": "ACTUAL_USER", "cumulativeReturnPercent": 0.0},
            {"variantId": 2, "variantType": "PERSONAL_BOT", "cumulativeReturnPercent": 0.0},
        ]
        return DeterministicReportAnalyzer().build([], participants, analytics)

    def test_a_semantic_conflict_reaches_the_report(self):
        report = self._report({"principle_conflicts": [{
            "first_principle_text": "물타기로 평단을 낮춘다",
            "second_principle_text": "10% 빠지면 손절한다",
            "conflict_type": "CONTRADICTION",
            "reason": "손실 구간에서 한쪽은 더 사고 한쪽은 팔라고 지시합니다.",
        }]})
        conflicts = report["principleSetDiagnostics"]["conflicts"]

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["conflictType"], "CONTRADICTION")
        self.assertEqual(conflicts[0]["judgmentSource"], "LLM_PRINCIPLE_REVIEW")

    def test_a_conflict_naming_an_unknown_principle_is_dropped_at_the_report_too(self):
        report = self._report({"principle_conflicts": [{
            "first_principle_text": "이 사용자에게 없는 원칙",
            "second_principle_text": "10% 빠지면 손절한다",
            "conflict_type": "OVERLAP", "reason": "지어낸 충돌",
        }]})

        self.assertEqual(report["principleSetDiagnostics"]["conflicts"], [])

    def test_an_unmappable_principle_carries_its_reason_instead_of_sitting_silent(self):
        report = self._report({"principle_conflicts": []})
        unmapped = report["principleSetDiagnostics"]["unmappedPrinciples"]

        self.assertEqual(len(unmapped), 1)
        self.assertEqual(unmapped[0]["principleSetItemId"], 3)
        self.assertEqual(unmapped[0]["reason"], "실적 발표일 데이터가 시스템에 없습니다.")


class SchemaRoundTripTests(unittest.TestCase):
    def test_the_new_audit_fields_survive_a_round_trip(self):
        source = {
            **InvestmentBotStrategySchema().to_dict(),
            "audit": {
                "ai_confidence": 0.8,
                "interpreted_principles": [{
                    "user_natural_text": "실적 발표 전에는 사지 않는다",
                    "ai_mapped_rule": "",
                    "status": "REVIEW_REQUIRED",
                    "unmappable_reason": "실적 발표일 데이터가 없습니다.",
                }],
                "needs_user_confirmation": [],
                "principle_conflicts": [{
                    "first_principle_text": "A", "second_principle_text": "B",
                    "conflict_type": "CONTRADICTION", "reason": "설명",
                }],
            },
        }

        restored = InvestmentBotStrategySchema.from_dict(source).to_dict()["audit"]

        self.assertEqual(
            restored["interpreted_principles"][0]["unmappable_reason"],
            "실적 발표일 데이터가 없습니다.",
        )
        self.assertEqual(len(restored["principle_conflicts"]), 1)

    def test_an_older_stored_schema_without_the_new_fields_still_loads(self):
        source = {
            **InvestmentBotStrategySchema().to_dict(),
            "audit": {
                "ai_confidence": 0.9,
                "interpreted_principles": [{
                    "user_natural_text": "급등주 금지",
                    "ai_mapped_rule": "entry.max_5day_return",
                    "status": "CONFIRMED",
                }],
                "needs_user_confirmation": [],
            },
        }

        restored = InvestmentBotStrategySchema.from_dict(source).to_dict()["audit"]

        self.assertEqual(restored["interpreted_principles"][0]["unmappable_reason"], "")
        self.assertEqual(restored["principle_conflicts"], [])


if __name__ == "__main__":
    unittest.main()
