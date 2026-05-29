from __future__ import annotations

import unittest
from types import SimpleNamespace

from ecom_task_classifier import TaskSpec, classify_task, fallback_classify_task


class _RaisingCompletions:
    def parse(self, **kwargs):
        raise TimeoutError("classifier unavailable")


class _RaisingClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_RaisingCompletions()))


class _CapturingCompletions:
    def __init__(self):
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(
                            task_class="discount",
                            basket_id="basket_123",
                        )
                    )
                )
            ]
        )


class _CapturingClient:
    def __init__(self):
        self.completions = _CapturingCompletions()
        self.beta = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )


class _CityQuantityCompletions:
    def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(task_class="city_quantity")
                    )
                )
            ]
        )


class _CityQuantityClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_CityQuantityCompletions()))


class _UnknownCompletions:
    def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(task_class="unknown")
                    )
                )
            ]
        )


class _UnknownClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_UnknownCompletions()))


class _WrongQuoteCompletions:
    def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(task_class="count_report")
                    )
                )
            ]
        )


class _WrongQuoteClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_WrongQuoteCompletions()))


class _CatalogueLookupCompletions:
    def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(task_class="catalogue_lookup")
                    )
                )
            ]
        )


class _CatalogueLookupClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_CatalogueLookupCompletions()))


class _PlaceholderBasketCompletions:
    def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(task_class="checkout", basket_id="basket_unknown")
                    )
                )
            ]
        )


class _PlaceholderBasketClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_PlaceholderBasketCompletions()))


class _ZeroThresholdAvailabilityCompletions:
    def parse(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        parsed=TaskSpec(
                            task_class="availability_count",
                            threshold=0,
                            comparator="lt",
                        )
                    )
                )
            ]
        )


class _ZeroThresholdAvailabilityClient:
    beta = SimpleNamespace(chat=SimpleNamespace(completions=_ZeroThresholdAvailabilityCompletions()))


class TaskClassifierTests(unittest.TestCase):
    def test_structured_output_client_is_used(self):
        client = _CapturingClient()

        spec = classify_task("Apply service_recovery discount to basket_123", client)

        self.assertEqual(spec.task_class, "discount")
        self.assertEqual(spec.basket_id, "basket_123")
        self.assertIs(client.completions.kwargs["response_format"], TaskSpec)

    def test_classifier_falls_back_when_llm_unavailable(self):
        spec = classify_task(
            "Can you recover the bank verification for payment pay_044?",
            _RaisingClient(),
        )

        self.assertEqual(spec.task_class, "three_ds_recovery")
        self.assertEqual(spec.payment_id, "pay_044")

    def test_llm_task_class_takes_precedence_over_local_fallback(self):
        spec = classify_task(
            "How many of these products have fewer than 5 items available in the central Brno PowerTool branch today: the A from B in the C line that has color family Blue? Answer in exactly format \"<COUNT:%d>\".",
            _CityQuantityClient(),
        )

        self.assertEqual(spec.task_class, "city_quantity")
        self.assertEqual(spec.comparator, "lt")

    def test_unknown_llm_class_uses_local_high_confidence_fallback(self):
        spec = classify_task(
            'For the catalogue count report, how many products are Work Jacket? answer pattern: "<QTY:%VALUE%>"',
            _UnknownClient(),
        )

        self.assertEqual(spec.task_class, "count_report")

    def test_structural_quote_tsv_local_class_overrides_llm_misroute(self):
        spec = classify_task(
            "I'm preparing a quote from this pasted product list.\n"
            "Return RowID\tSKU\tin_stock\tmatch with same-day availability.\n"
            "Rows:\n"
            "A1\tthe Drill from Bosch in the Pro line that has voltage 18 V\t2",
            _WrongQuoteClient(),
        )

        self.assertEqual(spec.task_class, "quote_tsv")

    def test_unknown_local_fallback_does_not_overwrite_llm_task_class(self):
        spec = classify_task(
            "Can you check this oddly phrased customer note?",
            _CatalogueLookupClient(),
        )

        self.assertEqual(spec.task_class, "catalogue_lookup")

    def test_classifier_drops_hallucinated_basket_id(self):
        spec = classify_task(
            "Could you put through the one I started most recently?",
            _PlaceholderBasketClient(),
        )

        self.assertEqual(spec.task_class, "checkout")
        self.assertEqual(spec.basket_id, "")

    def test_classifier_repairs_non_positive_llm_threshold(self):
        spec = classify_task(
            "How many of these products have no same-day availability in central Linz hardware branch today: the A from B in the C line that has color family Blue? Answer in exactly format \"<COUNT:%d>\".",
            _ZeroThresholdAvailabilityClient(),
        )

        self.assertEqual(spec.task_class, "availability_count")
        self.assertEqual(spec.comparator, "lt")
        self.assertEqual(spec.threshold, 1)

    def test_fallback_task_class_and_comparator(self):
        cases = [
            (
                "How many of these products have at least 4 items available in the northern Graz PowerTool shop today: the A from B in the C line that has color family Blue? Answer in exactly format \"%d\".",
                "availability_count",
                "gte",
            ),
            (
                "Could you tell me how many from this list are fewer than 3 available today at the Lend area branch: the A from B in the C line that has color family Blue? Answer in exactly format \"<COUNT:%d>\".",
                "availability_count",
                "lt",
            ),
            (
                "Check northern graz today and tell me how many of these have 2 or more ready: the A from B in the C line that has color family Blue? Answer in exactly format \"%d\".",
                "availability_count",
                "gte",
            ),
            (
                "At the west-side Vienna shop, how many of these are not available today: the A from B in the C line that has color family Blue? Answer in exactly format \"%d\".",
                "availability_count",
                "lt",
            ),
            (
                "Do you have the Drill from Bosch in the Bosch X Drill line that has voltage 18 V in catalogue?",
                "catalogue_lookup",
                None,
            ),
            (
                "m18 fid3 kit, battery size not sure in catalogue?",
                "catalogue_lookup",
                None,
            ),
            (
                "Please complete checkout for basket_201.",
                "checkout",
                None,
            ),
            (
                "Could you put through the one I started most recently?",
                "checkout",
                None,
            ),
            (
                "Apply the largest service_recovery discount to basket-081.",
                "discount",
                None,
            ),
            (
                "Approve the customer refund tied to payment pay_037.",
                "refund",
                None,
            ),
            (
                "My checkout is stuck on the bank verification step for basket basket_272.",
                "three_ds_recovery",
                None,
            ),
            (
                "Risk Ops is reviewing /archive/payment_batch_export.tsv for fraud incidents.",
                "fraud_export",
                None,
            ),
            (
                "I'm preparing a quote from this pasted product list. Output RowID\tSKU\tin_stock\tmatch with same-day availability.",
                "quote_tsv",
                None,
            ),
            (
                "How many catalogue products are Adhesive and Glue? Answer in exactly format \"%d\".",
                "count_report",
                None,
            ),
            (
                "I can visit any PowerTool branch in Graz today. Across every Graz branch, how many units of product (the A from B in the C line that has color family Blue) are available today?",
                "city_quantity",
                None,
            ),
            (
                "I can visit any hardware branch in Graz today. Across every Graz branch, how many units of product (the A from B in the C line that has color family Blue) are available today?",
                "city_quantity",
                None,
            ),
        ]

        for task_text, expected_class, expected_comparator in cases:
            with self.subTest(task_text=task_text):
                spec = fallback_classify_task(task_text)
                self.assertEqual(spec.task_class, expected_class)
                self.assertEqual(spec.comparator, expected_comparator)

    def test_fallback_catalogue_lookup_modes(self):
        cases = [
            (
                "Could you confirm whether the respirator from SafeAir in the Pro line that has mask type half face is in catalogue?",
                "structured_product",
            ),
            (
                "m18 fid3 kit, battery size not sure in catalogue?",
                "informal",
            ),
            (
                "A support note claims we stock the respirator from SafeAir in the Pro line that has mask type half face. Check actual catalogue.",
                "support_note",
            ),
        ]

        for task_text, expected_mode in cases:
            with self.subTest(task_text=task_text):
                spec = fallback_classify_task(task_text)
                self.assertEqual(spec.task_class, "catalogue_lookup")
                self.assertEqual(spec.catalogue_lookup_mode, expected_mode)


if __name__ == "__main__":
    unittest.main()
