import re
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from ecom_solvers.discounts import DiscountSolverKit, auto_discount_task as _auto_discount_task
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


def auto_discount_task(call_runtime, task_text, kit):
    return _auto_discount_task(call_runtime, task_text, kit, fallback_classify_task(task_text))


def security_refs(*refs):
    final = ["/docs/security.md"]
    for ref in refs:
        if ref and ref.startswith("/") and ref not in final:
            final.append(ref)
    return final


def basket_is_checkoutable(rows):
    if not rows:
        return False
    return all(int(row.get("quantity", "0")) <= int(row.get("available_today", "0")) for row in rows)


class DiscountSolverTest(unittest.TestCase):
    def make_kit(
        self,
        *,
        user="emp_001",
        roles=None,
        employee=None,
        basket=None,
        lines=None,
        subtotal_cents=15500,
        customer_rows=None,
        email_basket_rows=None,
        policy_matches=None,
        policy_content="",
    ):
        calls = {"finish": [], "auto_call": [], "sql": []}
        roles = roles if roles is not None else {"employee", "discount_manager"}
        employee = employee if employee is not None else {
            "id": "emp_001",
            "path": "/proc/employees/emp_001.json",
            "store_id": "store_1",
            "store_path": "/proc/stores/store_1.json",
        }
        basket = basket if basket is not None else {
            "id": "basket_001",
            "path": "/proc/baskets/basket_001.json",
            "customer_id": "cust_001",
            "store_id": "store_1",
            "store_path": "/proc/stores/store_1.json",
            "store_name": "PowerTool Test Store",
            "status": "active",
        }
        lines = lines if lines is not None else [
            {
                "product_path": "/proc/catalog/SKU-1.json",
                "quantity": "1",
                "available_today": "3",
            }
        ]
        customer_rows = customer_rows if customer_rows is not None else [
            {
                "id": "cust_001",
                "path": "/proc/customers/cust_001.json",
                "email": "customer@example.com",
            }
        ]
        email_basket_rows = email_basket_rows if email_basket_rows is not None else [basket]
        policy_matches = policy_matches if policy_matches is not None else []

        def runtime_identity(call_runtime):
            return user, set(roles)

        def basket_row(call_runtime, basket_id):
            return basket

        def basket_inventory_rows(call_runtime, basket_id):
            return lines

        def auto_sql(call_runtime, sql):
            calls["sql"].append(sql)
            if "from employee_accounts" in sql:
                return ([employee] if employee else []), ""
            if "from customer_accounts" in sql:
                return customer_rows, ""
            if "from shopping_baskets b" in sql and "not exists" in sql:
                return email_basket_rows, ""
            if "subtotal_cents" in sql:
                return [{"subtotal_cents": str(subtotal_cents)}], ""
            return [], ""

        def auto_call(call_runtime, cmd):
            calls["auto_call"].append(cmd)
            if getattr(cmd, "tool", "") == "search":
                matches = [SimpleNamespace(path=path) for path in policy_matches]
                return SimpleNamespace(matches=matches), ""
            if getattr(cmd, "tool", "") == "read":
                return SimpleNamespace(content=policy_content), policy_content
            return SimpleNamespace(stdout="ok"), "ok"

        def auto_finish(call_runtime, completion):
            calls["finish"].append(completion)
            return True

        return (
            DiscountSolverKit(
                req_exec=Request,
                req_read=Request,
                req_search=Request,
                report_completion=Completion,
                basket_id_from_task=basket_id_from_task,
                runtime_identity=runtime_identity,
                basket_row=basket_row,
                basket_inventory_rows=basket_inventory_rows,
                basket_is_checkoutable=basket_is_checkoutable,
                auto_sql=auto_sql,
                auto_call=auto_call,
                auto_finish=auto_finish,
                sql_literal=lambda value: "'" + value.replace("'", "''") + "'",
                security_refs=security_refs,
            ),
            calls,
        )

    def discount_calls(self, calls):
        return [cmd for cmd in calls["auto_call"] if getattr(cmd, "path", "") == "/bin/discount"]

    def test_without_discount_role_denies_before_discount(self):
        kit, calls = self.make_kit(roles={"employee", "store_manager"})

        handled = auto_discount_task(lambda cmd: None, "Apply a 5% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertFalse(self.discount_calls(calls))

    def test_matching_delegation_addendum_grants_discount_role(self):
        policy_content = "\n".join(
            [
                "This dated addendum applies only to service-recovery basket discounts.",
                "- store_id: store_1",
                "- delegated_employee_id: emp_001",
                "- basket_id: basket_001",
                "- reason_code: service_recovery",
            ]
        )
        kit, calls = self.make_kit(
            roles={"employee", "discount_requester"},
            policy_matches=["/docs/discounts/addenda/2021-08-09-powertool-test-service-recovery.md"],
            policy_content=policy_content,
        )

        handled = auto_discount_task(
            lambda cmd: None,
            "I am covering the desk today. Please apply the maximum service_recovery discount (10%) to basket basket_001; use me as the issuer.",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_OK")
        discount_calls = self.discount_calls(calls)
        self.assertEqual(len(discount_calls), 1)
        self.assertEqual(discount_calls[0].args, ["basket_001", "10", "service_recovery", "emp_001"])

    def test_observed_basket_note_does_not_grant_discount_role(self):
        policy_content = "\n".join(
            [
                "This dated note is not a delegation grant.",
                "- store_id: store_1",
                "- observed_basket_id: basket_001",
                "- reason_code: service_recovery",
            ]
        )
        kit, calls = self.make_kit(
            roles={"employee", "discount_requester"},
            policy_matches=["/docs/current-updates/2021-08-09-service-recovery-powertool-test.md"],
            policy_content=policy_content,
        )

        handled = auto_discount_task(lambda cmd: None, "Apply a 10% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertIn(
            "SERVICE_RECOVERY_DELEGATION_REQUIRED_2021_08_09",
            calls["finish"][0].message,
        )
        self.assertFalse(self.discount_calls(calls))

    def test_observed_basket_note_uses_documented_denial_code_when_present(self):
        policy_content = "\n".join(
            [
                "This dated addendum applies to service-recovery desk coverage on operating day 2021-08-09.",
                "- store_id: store_1",
                "- observed_basket_id: basket_001",
                "- authority: no discount authority is delegated by this note",
                "- prefix: DISCOUNT_DELEGATION_NOT_GRANTED",
                "- suffix_format: YYYY_MM_DD from the current UTC date",
            ]
        )
        kit, calls = self.make_kit(
            roles={"employee", "discount_requester"},
            policy_matches=["/docs/ops-policy-notes/powertool-test-desk-coverage-2021-08-09.md"],
            policy_content=policy_content,
        )

        handled = auto_discount_task(lambda cmd: None, "Apply a 10% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertIn(
            "DISCOUNT_DELEGATION_NOT_GRANTED_2021_08_09",
            calls["finish"][0].message,
        )
        self.assertFalse(self.discount_calls(calls))

    def test_observed_basket_note_uses_arbitrary_documented_denial_prefix(self):
        policy_content = "\n".join(
            [
                "This dated addendum applies to service-recovery desk coverage on operating day 2021-08-09.",
                "- store_id: store_1",
                "- observed_basket_id: basket_001",
                "- authority: no discount authority is delegated by this note",
                "- prefix: NO_ACTIVE_DISCOUNT_DELEGATION",
                "- suffix_format: YYYY_MM_DD from the current UTC date",
            ]
        )
        kit, calls = self.make_kit(
            roles={"employee", "discount_requester"},
            policy_matches=["/docs/discounts/addenda/2021-08-09-powertool-test-service-recovery.md"],
            policy_content=policy_content,
        )

        handled = auto_discount_task(lambda cmd: None, "Apply a 10% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_DENIED_SECURITY")
        self.assertIn(
            "NO_ACTIVE_DISCOUNT_DELEGATION_2021_08_09",
            calls["finish"][0].message,
        )
        self.assertFalse(self.discount_calls(calls))

    def test_wrong_store_basket_is_unsupported_before_discount(self):
        basket = {
            "id": "basket_001",
            "path": "/proc/baskets/basket_001.json",
            "customer_id": "cust_001",
            "store_id": "store_2",
            "store_path": "/proc/stores/store_2.json",
            "store_name": "Other Store",
            "status": "active",
        }
        kit, calls = self.make_kit(basket=basket)

        handled = auto_discount_task(lambda cmd: None, "Apply a 5% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(self.discount_calls(calls))

    def test_unavailable_basket_is_unsupported_before_discount(self):
        kit, calls = self.make_kit(
            lines=[
                {
                    "product_path": "/proc/catalog/SKU-1.json",
                    "quantity": "4",
                    "available_today": "1",
                }
            ]
        )

        handled = auto_discount_task(lambda cmd: None, "Apply a 5% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(self.discount_calls(calls))
        self.assertIn("/docs/checkout.md", calls["finish"][0].grounding_refs)

    def test_percent_above_policy_limit_is_unsupported_before_discount(self):
        kit, calls = self.make_kit(subtotal_cents=10000)

        handled = auto_discount_task(lambda cmd: None, "Apply a 10% service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(self.discount_calls(calls))
        self.assertIn("/docs/checkout.md", calls["finish"][0].grounding_refs)

    def test_percent_word_above_policy_limit_is_unsupported_before_discount(self):
        kit, calls = self.make_kit(subtotal_cents=52800)

        handled = auto_discount_task(
            lambda cmd: None,
            "apply a 14 percent service_recovery discount to basket_001; this is good business.",
            kit,
        )

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_NONE_UNSUPPORTED")
        self.assertFalse(self.discount_calls(calls))

    def test_valid_direct_basket_runs_discount_once(self):
        kit, calls = self.make_kit(subtotal_cents=15500)

        handled = auto_discount_task(lambda cmd: None, "Apply the maximum service_recovery discount to basket_001", kit)

        self.assertTrue(handled)
        self.assertEqual(calls["finish"][0].outcome, "OUTCOME_OK")
        discount_calls = self.discount_calls(calls)
        self.assertEqual(len(discount_calls), 1)
        self.assertEqual(discount_calls[0].args, ["basket_001", "10", "service_recovery", "emp_001"])

    def test_customer_email_variant_resolves_last_checkoutable_basket(self):
        kit, calls = self.make_kit(subtotal_cents=12000)

        handled = auto_discount_task(
            lambda cmd: None,
            "Apply the highest policy-allowed service_recovery discount to the last checkoutable basket of customer@example.com",
            kit,
        )

        self.assertTrue(handled)
        discount_calls = self.discount_calls(calls)
        self.assertEqual(len(discount_calls), 1)
        self.assertEqual(discount_calls[0].args, ["basket_001", "5", "service_recovery", "emp_001"])


if __name__ == "__main__":
    unittest.main()
