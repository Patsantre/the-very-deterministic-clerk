import re
import unittest
from dataclasses import dataclass

from ecom_solvers.refunds import RefundSolverKit, auto_refund_task as _auto_refund_task
from ecom_task_classifier import fallback_classify_task


@dataclass
class Completion:
    tool: str
    completed_steps_laconic: list[str]
    message: str
    grounding_refs: list[str]
    outcome: str


class ReqExec:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def payment_id_from_task(task_text):
    match = re.search(r"\bpay[_ -](\d+)\b", task_text, re.I)
    return f"pay_{match.group(1)}" if match else ""


def auto_refund_task(call_runtime, task_text, kit):
    return _auto_refund_task(call_runtime, task_text, kit, fallback_classify_task(task_text))


def security_refs(*refs):
    final = ["/docs/security.md"]
    for ref in refs:
        if ref and ref.startswith("/") and ref not in final:
            final.append(ref)
    return final


class RefundSolverTest(unittest.TestCase):
    def make_kit(
        self,
        *,
        user="emp_001",
        roles=None,
        payment_rows=None,
        return_rows=None,
        amount_rows=None,
        fallback_amount_rows=None,
    ):
        calls = {"finish": [], "auto_call": [], "sql": []}
        roles = roles if roles is not None else {"employee", "refund_manager"}
        payment_rows = payment_rows if payment_rows is not None else [
            {
                "id": "pay_001",
                "path": "/proc/payments/pay_001.json",
                "customer_id": "cust_001",
                "status": "paid",
            }
        ]
        return_rows = return_rows if return_rows is not None else [
            {
                "id": "ret_001",
                "path": "/proc/returns/ret_001.json",
                "status": "refund_pending",
                "payment_path": "/proc/payments/pay_001.json",
            }
        ]
        amount_rows = amount_rows if amount_rows is not None else []
        fallback_amount_rows = fallback_amount_rows if fallback_amount_rows is not None else []

        def runtime_identity(call_runtime):
            return user, set(roles)

        def auto_sql(call_runtime, sql):
            calls["sql"].append(sql)
            if "from payment_transactions" in sql and "where payment_id =" in sql:
                return payment_rows, ""
            if "from return_requests" in sql and "where payment_id =" in sql:
                return return_rows, ""
            if "from return_requests r" in sql and "where r.return_id =" in sql:
                return return_rows, ""
            if "p.payment_amount_cents" in sql:
                return amount_rows, ""
            if "where r.customer_id" in sql:
                return fallback_amount_rows, ""
            return [], ""

        def auto_call(call_runtime, cmd):
            calls["auto_call"].append(cmd)
            return None, ""

        def auto_finish(call_runtime, completion):
            calls["finish"].append(completion)
            return True

        return (
            RefundSolverKit(
                req_exec=ReqExec,
                report_completion=Completion,
                runtime_identity=runtime_identity,
                payment_id_from_task=payment_id_from_task,
                auto_sql=auto_sql,
                auto_call=auto_call,
                auto_finish=auto_finish,
                sql_literal=lambda value: "'" + value.replace("'", "''") + "'",
                security_refs=security_refs,
            ),
            calls,
        )

    def test_refund_pending_return_is_unsupported_without_side_effect(self):
        kit, calls = self.make_kit(
            return_rows=[
                {
                    "id": "ret_004",
                    "path": "/proc/returns/ret_004.json",
                    "status": "refund_pending",
                    "payment_path": "/proc/payments/pay_007.json",
                }
            ]
        )

        handled = auto_refund_task(lambda cmd: None, "Please approve refund for return ret_004", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(calls["auto_call"])
        self.assertIn("/proc/returns/ret_004.json", calls["finish"][0].grounding_refs)

    def test_missing_return_is_unsupported_without_side_effect(self):
        kit, calls = self.make_kit(return_rows=[])

        handled = auto_refund_task(lambda cmd: None, "Move refund approval forward for return ret_999", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(calls["auto_call"])
        self.assertEqual(calls["finish"][0].grounding_refs, ["/docs/returns.md"])

    def test_payment_refund_approval_does_not_call_payments_tool(self):
        kit, calls = self.make_kit()

        handled = auto_refund_task(lambda cmd: None, "Approve refund for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(calls["auto_call"])

    def test_customer_payment_refund_finalizes_pending_return(self):
        kit, calls = self.make_kit(
            user="cust_001",
            roles={"customer"},
            payment_rows=[
                {
                    "id": "pay_001",
                    "path": "/proc/payments/pay_001.json",
                    "customer_id": "cust_001",
                    "status": "paid",
                }
            ],
            return_rows=[
                {
                    "id": "ret_001",
                    "path": "/proc/returns/ret_001.json",
                    "customer_id": "cust_001",
                    "status": "refund_pending",
                    "payment_path": "/proc/payments/pay_001.json",
                }
            ],
        )

        handled = auto_refund_task(lambda cmd: None, "refund my payment pay_001 for €699", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_OK")
        self.assertEqual(len(calls["auto_call"]), 1)
        self.assertEqual(calls["auto_call"][0].path, "/bin/payments")
        self.assertEqual(calls["auto_call"][0].args, ["refund", "ret_001"])
        self.assertIn("/docs/security.md", calls["finish"][0].grounding_refs)

    def test_payment_refund_approval_calls_tool_for_approved_return(self):
        kit, calls = self.make_kit(
            return_rows=[
                {
                    "id": "ret_014",
                    "path": "/proc/returns/ret_014.json",
                    "status": "approved",
                    "payment_path": "/proc/payments/pay_001.json",
                }
            ]
        )

        handled = auto_refund_task(lambda cmd: None, "Approve refund for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_OK")
        self.assertEqual(len(calls["auto_call"]), 1)
        self.assertEqual(calls["auto_call"][0].path, "/bin/payments")
        self.assertEqual(calls["auto_call"][0].args, ["approve-refund", "ret_014"])

    def test_employee_without_refund_role_is_denied_without_private_refs(self):
        kit, calls = self.make_kit(roles={"employee", "customer_service"})

        handled = auto_refund_task(lambda cmd: None, "Approve refund for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertFalse(calls["auto_call"])
        self.assertEqual(calls["finish"][0].grounding_refs, ["/docs/security.md", "/docs/returns.md"])

    def test_fulfillment_coordinator_without_refund_manager_is_denied(self):
        kit, calls = self.make_kit(roles={"employee", "fulfillment_coordinator"})

        handled = auto_refund_task(lambda cmd: None, "Approve refund for payment pay_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertFalse(calls["auto_call"])

    def test_amount_refund_request_is_unsupported_without_side_effect(self):
        kit, calls = self.make_kit(
            user="cust_001",
            roles={"customer"},
            amount_rows=[
                {
                    "return_id": "ret_009",
                    "return_path": "/proc/returns/ret_009.json",
                    "return_status": "rejected",
                    "payment_id": "pay_017",
                    "payment_path": "/proc/payments/pay_017.json",
                }
            ],
        )

        handled = auto_refund_task(lambda cmd: None, "i need the EUR 125.00 purchase refunded", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(calls["auto_call"])
        self.assertIn("/proc/returns/ret_009.json", calls["finish"][0].grounding_refs)

    def test_customer_amount_refund_finalizes_pending_return(self):
        kit, calls = self.make_kit(
            user="cust_001",
            roles={"customer"},
            amount_rows=[
                {
                    "return_id": "ret_016",
                    "return_path": "/proc/returns/ret_016.json",
                    "return_status": "refund_pending",
                    "return_customer_id": "cust_001",
                    "payment_id": "pay_031",
                    "payment_path": "/proc/payments/pay_031.json",
                    "payment_status": "paid",
                    "basket_id": "basket_031",
                    "basket_path": "/proc/baskets/basket_031.json",
                    "basket_customer_id": "cust_001",
                }
            ],
        )

        handled = auto_refund_task(lambda cmd: None, "please refund my purchase for 22 euros", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_OK")
        self.assertEqual(len(calls["auto_call"]), 1)
        self.assertEqual(calls["auto_call"][0].path, "/bin/payments")
        self.assertEqual(calls["auto_call"][0].args, ["refund", "ret_016"])
        self.assertIn("/proc/baskets/basket_031.json", calls["finish"][0].grounding_refs)


if __name__ == "__main__":
    unittest.main()
