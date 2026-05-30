import unittest

from ecom_policy_index import (
    build_policy_index_from_documents,
    candidate_policy_paths_from_tree,
    classify_policy_doc,
)


class TreeNode:
    def __init__(self, name, children=None):
        self.name = name
        self.children = children or []


class PolicyIndexTest(unittest.TestCase):
    def test_classifies_policy_docs_by_path_and_content(self):
        self.assertEqual(classify_policy_doc("/AGENTS.md", "start here"), ("agents.root",))
        self.assertIn(
            "discount.service_recovery",
            classify_policy_doc("/docs/policies/discount-rules.md", "service recovery"),
        )
        self.assertIn(
            "payment.3ds",
            classify_policy_doc("/docs/payments/card-verification.md", "3DS retry rules"),
        )

    def test_refs_remap_legacy_paths_to_discovered_paths(self):
        index = build_policy_index_from_documents(
            [
                ("/docs/policies/security-v2.md", "identity roles and ownership", "sha-security"),
                ("/docs/policies/checkout-v2.md", "checkout basket inventory", "sha-checkout"),
            ]
        )

        self.assertEqual(
            index.security_refs("/docs/checkout.md", "/proc/baskets/basket_001.json"),
            [
                "/docs/policies/security-v2.md",
                "/docs/policies/checkout-v2.md",
                "/proc/baskets/basket_001.json",
            ],
        )
        self.assertTrue(index.known_sha("sha-security"))

    def test_path_classification_does_not_overclassify_policy_content(self):
        index = build_policy_index_from_documents(
            [
                (
                    "docs/discounts.md",
                    "Service recovery discount policy mentions employee roles and ownership checks.",
                    "sha-discounts",
                ),
                ("/docs/security.md", "Identity and ownership rules.", "sha-security"),
            ]
        )

        self.assertEqual(index.refs("security.identity"), ["/docs/security.md"])
        self.assertEqual(index.refs("/docs/discounts.md"), ["/docs/discounts.md"])

    def test_unknown_semantic_key_uses_default_path(self):
        index = build_policy_index_from_documents([])

        self.assertEqual(index.refs("returns.refund"), ["/docs/returns.md"])

    def test_tree_path_candidates_only_include_policy_like_files(self):
        root = TreeNode(
            "docs",
            [
                TreeNode("checkout.md"),
                TreeNode("random-note.md"),
                TreeNode("payments", [TreeNode("3ds.md"), TreeNode("receipt.txt")]),
            ],
        )

        self.assertEqual(
            candidate_policy_paths_from_tree(root),
            ["/docs/checkout.md", "/docs/payments/3ds.md"],
        )


if __name__ == "__main__":
    unittest.main()
