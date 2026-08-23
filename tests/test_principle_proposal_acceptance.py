import json
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.api.endpoints.principles import (
    AcceptPrincipleProposalRequest,
    accept_principle_proposal,
)


class FakeCursor:
    def __init__(self, principle_items=None, idempotency_duplicate=False, existing_application=None):
        self.principle_items = principle_items or []
        self.idempotency_duplicate = idempotency_duplicate
        self.existing_application = existing_application
        self.executions = []
        self.lastrowid = 88
        self.last_query = ""
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.last_query = " ".join(str(query).split())
        self.executions.append((self.last_query, params))
        self.rowcount = (
            0
            if self.last_query.startswith("INSERT IGNORE") and self.idempotency_duplicate
            else 1
        )

    def fetchone(self):
        if "FROM principle_sets" in self.last_query:
            return (5,)
        if "MAX(sort_order)" in self.last_query:
            return (4,)
        if "FROM principle_proposal_applications app" in self.last_query:
            return self.existing_application
        return None

    def fetchall(self):
        return self.principle_items


class FakeConnection:
    def __init__(self, principle_items=None, idempotency_duplicate=False, existing_application=None):
        self.fake_cursor = FakeCursor(
            principle_items,
            idempotency_duplicate=idempotency_duplicate,
            existing_application=existing_application,
        )
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self.fake_cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class PrincipleProposalAcceptanceTests(unittest.TestCase):
    @staticmethod
    def _detail(proposal_type, **overrides):
        proposal = {
            "recommendationId": 2001,
            "proposalType": proposal_type,
            "title": "급등 후 진입 제한",
            "description": "최근 5일 급등 종목은 진입을 보류합니다.",
            "targetRule": "entry.max_5day_return",
            "ruleJson": {"entry": {"max_5day_return": 0.10}},
            **overrides,
        }
        return {
            "report_json": {
                "reportVersion": "DETERMINISTIC",
                "principleEvaluations": [{
                    "evaluationId": "PE_9_entry_max_5day_return",
                    "verdict": "STRENGTHEN",
                    "suggestion": proposal,
                }],
            }
        }

    def test_discovery_is_appended_from_server_stored_proposal(self):
        connection = FakeConnection()
        with patch(
            "app.modules.simulation.persistence.db_persistence.get_simulation_owner_id",
            return_value=1,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.load_simulation_from_db_by_id",
            return_value=self._detail("DISCOVERY"),
        ), patch(
            "app.modules.simulation.persistence.db_persistence.get_db_connection",
            return_value=connection,
        ):
            result = accept_principle_proposal(
                AcceptPrincipleProposalRequest(simulationId=10, recommendationId=2001),
                user_id=1,
            )

        self.assertTrue(connection.committed)
        self.assertEqual(result["applicationType"], "DISCOVERY_ADDED")
        insert = next(
            item for item in connection.fake_cursor.executions
            if item[0].startswith("INSERT INTO principle_set_items")
        )
        self.assertEqual(json.loads(insert[1][2]), {"entry": {"max_5day_return": 0.10}})

    def test_reinforcement_updates_the_matched_existing_principle(self):
        connection = FakeConnection([
            (9, "급등주를 추격매수하지 않는다", '{"entry":{"max_5day_return":0.15}}')
        ])
        detail = self._detail(
            "REINFORCEMENT",
            sourcePrincipleText="급등주를 추격매수하지 않는다",
            ruleJson={"entry": {"max_5day_return": 0.08}},
        )
        with patch(
            "app.modules.simulation.persistence.db_persistence.get_simulation_owner_id",
            return_value=1,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.load_simulation_from_db_by_id",
            return_value=detail,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.get_db_connection",
            return_value=connection,
        ):
            result = accept_principle_proposal(
                AcceptPrincipleProposalRequest(simulationId=10, recommendationId=2001),
                user_id=1,
            )

        update = next(item for item in connection.fake_cursor.executions if item[0].startswith("UPDATE"))
        self.assertEqual(json.loads(update[1][0]), {"entry": {"max_5day_return": 0.08}})
        self.assertEqual(update[1][1], 9)
        self.assertEqual(result["applicationType"], "REINFORCEMENT_UPDATED")
        # The execution threshold is this service's to change; the sentence the
        # user wrote belongs to the principle service and must survive intact.
        self.assertNotIn("principle_text", update[0])
        self.assertEqual(result["principleText"], "급등주를 추격매수하지 않는다")

    def test_v13_evaluation_suggestion_updates_the_principle_by_item_id(self):
        connection = FakeConnection([
            (8, "같은 규칙의 이전 원칙", '{"entry":{"max_5day_return":0.20}}'),
            (9, "급등주를 추격매수하지 않는다", '{"entry":{"max_5day_return":0.15}}'),
        ])
        detail = {
            "report_json": {
                "reportVersion": "DETERMINISTIC",
                "principleEvaluations": [{
                    "evaluationId": "PE_9_entry_max_5day_return",
                    "principleSetItemId": 9,
                    "verdict": "STRENGTHEN",
                    "suggestion": {
                        "recommendationId": 4001,
                        "proposalType": "REINFORCEMENT",
                        "principleSetItemId": 9,
                        "description": "최근 5일 8% 이상 급등한 종목은 진입을 보류합니다.",
                        "sourcePrincipleText": "급등주를 추격매수하지 않는다",
                        "targetRule": "entry.max_5day_return",
                        "ruleJson": {"entry": {"max_5day_return": 0.08}},
                    },
                }],
            }
        }
        with patch(
            "app.modules.simulation.persistence.db_persistence.get_simulation_owner_id",
            return_value=1,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.load_simulation_from_db_by_id",
            return_value=detail,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.get_db_connection",
            return_value=connection,
        ):
            result = accept_principle_proposal(
                AcceptPrincipleProposalRequest(simulationId=10, recommendationId=4001),
                user_id=1,
            )

        update = next(item for item in connection.fake_cursor.executions if item[0].startswith("UPDATE"))
        self.assertEqual(update[1][1], 9)
        self.assertEqual(result["principleSetItemId"], 9)
        self.assertEqual(result["principleText"], "급등주를 추격매수하지 않는다")

    def test_stable_evaluation_id_selects_the_proposal_without_a_recommendation_id(self):
        connection = FakeConnection([
            (9, "급등주를 추격매수하지 않는다", '{"entry":{"max_5day_return":0.15}}'),
        ])
        detail = {
            "report_json": {
                "reportVersion": "DETERMINISTIC",
                "principleEvaluations": [{
                    "evaluationId": "PE_9_entry_max_5day_return",
                    "principleSetItemId": 9,
                    "verdict": "STRENGTHEN",
                    "suggestion": {
                        "recommendationId": 4001,
                        "evaluationId": "PE_9_entry_max_5day_return",
                        "proposalType": "REINFORCEMENT",
                        "principleSetItemId": 9,
                        "sourcePrincipleText": "급등주를 추격매수하지 않는다",
                        "targetRule": "entry.max_5day_return",
                        "ruleJson": {"entry": {"max_5day_return": 0.08}},
                    },
                }],
            }
        }
        with patch(
            "app.modules.simulation.persistence.db_persistence.get_simulation_owner_id",
            return_value=1,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.load_simulation_from_db_by_id",
            return_value=detail,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.get_db_connection",
            return_value=connection,
        ):
            result = accept_principle_proposal(
                AcceptPrincipleProposalRequest(
                    simulationId=10,
                    evaluationId="PE_9_entry_max_5day_return",
                ),
                user_id=1,
            )

        self.assertEqual(result["principleSetItemId"], 9)
        # The stored proposal supplies the idempotency key, so a client that
        # never sends a positional id still records the application correctly.
        self.assertEqual(result["recommendationId"], 4001)

    def test_duplicate_request_returns_the_original_application_without_inserting(self):
        connection = FakeConnection(
            idempotency_duplicate=True,
            existing_application=(
                "APPLIED",
                "DISCOVERY",
                88,
                "최근 5일 급등 종목은 진입을 보류합니다.",
                '{"entry":{"max_5day_return":0.10}}',
            ),
        )
        with patch(
            "app.modules.simulation.persistence.db_persistence.get_simulation_owner_id",
            return_value=1,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.load_simulation_from_db_by_id",
            return_value=self._detail("DISCOVERY"),
        ), patch(
            "app.modules.simulation.persistence.db_persistence.get_db_connection",
            return_value=connection,
        ):
            result = accept_principle_proposal(
                AcceptPrincipleProposalRequest(simulationId=10, recommendationId=2001),
                user_id=1,
            )

        principle_inserts = [
            item for item in connection.fake_cursor.executions
            if item[0].startswith("INSERT INTO principle_set_items")
        ]
        self.assertEqual(principle_inserts, [])
        self.assertTrue(result["idempotentReplay"])
        self.assertEqual(result["principleSetItemId"], 88)
        self.assertEqual(result["ruleJson"], {"entry": {"max_5day_return": 0.10}})


    def test_a_simulation_belonging_to_someone_else_is_refused(self):
        connection = FakeConnection()
        with patch(
            "app.modules.simulation.persistence.db_persistence.get_simulation_owner_id",
            return_value=77,
        ), patch(
            "app.modules.simulation.persistence.db_persistence.load_simulation_from_db_by_id",
            return_value=self._detail("DISCOVERY"),
        ), patch(
            "app.modules.simulation.persistence.db_persistence.get_db_connection",
            return_value=connection,
        ):
            with self.assertRaises(HTTPException) as caught:
                accept_principle_proposal(
                    AcceptPrincipleProposalRequest(simulationId=10, recommendationId=2001),
                    user_id=1,
                )

        # 404 rather than 403: a stranger must not learn the run exists.
        self.assertEqual(caught.exception.status_code, 404)
        self.assertFalse(connection.committed)
        self.assertEqual(connection.fake_cursor.executions, [])


if __name__ == "__main__":
    unittest.main()
