import re
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from ecom_solvers.payments_3ds import ThreeDsSolverKit, auto_3ds_recovery_task as _auto_3ds_recovery_task
from ecom_task_classifier import fallback_classify_task


@dataclass
class Completion:
    tool: str
    completed_steps_laconic: list[str]
    message: str
    grounding_refs: list[str]
    outcome: str


class Request:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def basket_id_from_task(task_text):
    match = re.search(r"basket[_ -](\d+)", task_text, re.I)
    return f"basket_{match.group(1)}" if match else ""


def payment_id_from_task(task_text):
    match = re.search(r"\bpay[_ -](\d+)\b", task_text, re.I)
    return f"pay_{match.group(1)}" if match else ""


def auto_3ds_recovery_task(call_runtime, task_text, kit):
    return _auto_3ds_recovery_task(call_runtime, task_text, kit, fallback_classify_task(task_text))


def security_refs(*refs):
    final = ["/docs/security.md"]
    for ref in refs:
        if ref and ref.startswith("/") and ref not in final:
            final.append(ref)
    return final


class ThreeDsSolverTest(unittest.TestCase):
    def make_kit(
        self,
        *,
        user="cust_001",
        roles=None,
        payment_rows=None,
        policy_matches=None,
        policy_content="",
        date_now="2024-07-17T10:00:00Z",
    ):
        calls = {"finish": [], "auto_call": [], "sql": []}
        roles = roles if roles is not None else {"customer"}
        payment_rows = payment_rows if payment_rows is not None else [
            {
                "id": "pay_001",
                "path": "/proc/payments/pay_001.json",
                "basket_id": "basket_001",
                "customer_id": "cust_001",
                "status": "requires_3ds_action",
                "three_ds_status": "3ds-status2",
                "three_ds_failure_reason": "challenge_timeout",
                "three_ds_attempts": "0",
                "three_ds_max_attempts": "3",
                "basket_path": "/proc/baskets/basket_001.json",
                "basket_status": "checked_out",
                "basket_customer_id": "cust_001",
            }
        ]
        policy_matches = policy_matches if policy_matches is not None else []

        def runtime_identity(call_runtime):
            return user, set(roles)

        def auto_sql(call_runtime, sql):
            calls["sql"].append(sql)
            return payment_rows, ""

        def auto_call(call_runtime, cmd):
            calls["auto_call"].append(cmd)
            if getattr(cmd, "tool", "") == "search":
                matches = [SimpleNamespace(path=path) for path in policy_matches]
                return SimpleNamespace(matches=matches), ""
            if getattr(cmd, "tool", "") == "read":
                return SimpleNamespace(content=policy_content), policy_content
            if getattr(cmd, "path", "") == "/bin/date":
                return SimpleNamespace(stdout=date_now), date_now
            return SimpleNamespace(stdout="ok"), "ok"

        def auto_finish(call_runtime, completion):
            calls["finish"].append(completion)
            return True

        return (
            ThreeDsSolverKit(
                req_exec=Request,
                req_read=Request,
                req_search=Request,
                report_completion=Completion,
                basket_id_from_task=basket_id_from_task,
                payment_id_from_task=payment_id_from_task,
                runtime_identity=runtime_identity,
                auto_sql=auto_sql,
                auto_call=auto_call,
                auto_finish=auto_finish,
                sql_literal=lambda value: "'" + value.replace("'", "''") + "'",
                security_refs=security_refs,
            ),
            calls,
        )

    def recover_calls(self, calls):
        return [
            cmd
            for cmd in calls["auto_call"]
            if getattr(cmd, "path", "") == "/bin/payments"
            and getattr(cmd, "args", [])[:1] == ["recover-3ds"]
        ]

    def test_missing_payment_is_unsupported_without_recovery(self):
        kit, calls = self.make_kit(payment_rows=[])

        handled = auto_3ds_recovery_task(lambda cmd: None, "Recover 3DS for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(self.recover_calls(calls))

    def test_cross_customer_denies_without_private_refs_or_recovery(self):
        kit, calls = self.make_kit(user="cust_999")

        handled = auto_3ds_recovery_task(lambda cmd: None, "Recover 3DS for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertFalse(self.recover_calls(calls))
        refs = calls["finish"][0].grounding_refs
        self.assertNotIn("/proc/payments/pay_001.json", refs)
        self.assertNotIn("/proc/baskets/basket_001.json", refs)

    def test_paid_payment_is_unsupported_without_recovery(self):
        payment_rows = [
            {
                "id": "pay_001",
                "path": "/proc/payments/pay_001.json",
                "basket_id": "basket_001",
                "customer_id": "cust_001",
                "status": "paid",
                "three_ds_status": "3ds-status2",
                "three_ds_attempts": "0",
                "three_ds_max_attempts": "3",
                "basket_path": "/proc/baskets/basket_001.json",
                "basket_status": "checked_out",
                "basket_customer_id": "cust_001",
            }
        ]
        kit, calls = self.make_kit(payment_rows=payment_rows)

        handled = auto_3ds_recovery_task(lambda cmd: None, "Recover 3DS for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertIn("already paid", calls["finish"][0].message)
        self.assertFalse(self.recover_calls(calls))

    def test_retry_lockout_is_unsupported_without_recovery(self):
        kit, calls = self.make_kit(
            policy_matches=["/docs/payments/3ds-retry.md"],
            policy_content="retry_available_at: 2024-07-18T10:00:00Z",
            date_now="2024-07-17T10:00:00Z",
        )

        handled = auto_3ds_recovery_task(lambda cmd: None, "Recover 3DS for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertIn("2024-07-18T10:00:00Z", calls["finish"][0].message)
        self.assertFalse(self.recover_calls(calls))

    def test_max_attempts_is_unsupported_without_recovery(self):
        payment_rows = [
            {
                "id": "pay_001",
                "path": "/proc/payments/pay_001.json",
                "basket_id": "basket_001",
                "customer_id": "cust_001",
                "status": "requires_3ds_action",
                "three_ds_status": "3ds-status2",
                "three_ds_attempts": "3",
                "three_ds_max_attempts": "3",
                "basket_path": "/proc/baskets/basket_001.json",
                "basket_status": "checked_out",
                "basket_customer_id": "cust_001",
            }
        ]
        kit, calls = self.make_kit(payment_rows=payment_rows)

        handled = auto_3ds_recovery_task(lambda cmd: None, "Recover 3DS for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(self.recover_calls(calls))

    def test_valid_payment_runs_recovery_once(self):
        kit, calls = self.make_kit()

        handled = auto_3ds_recovery_task(lambda cmd: None, "Recover 3DS for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_OK")
        recover_calls = self.recover_calls(calls)
        self.assertEqual(len(recover_calls), 1)
        self.assertEqual(recover_calls[0].args, ["recover-3ds", "pay_001"])


if __name__ == "__main__":
    unittest.main()
