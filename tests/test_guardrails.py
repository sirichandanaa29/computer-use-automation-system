import unittest

from cua_system.guardrails import AllowlistPolicy, RiskClassifier, RiskDecision, PolicyViolation
from cua_system.schema import ActionType, RiskLevel, Step


class AllowlistPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = AllowlistPolicy(allowed_domains=["localhost"], blocked_path_substrings=["/admin"])

    def test_allows_in_domain_url(self):
        self.policy.check_url("http://localhost:5000/search?member_id=1")  # should not raise

    def test_blocks_out_of_domain_url(self):
        with self.assertRaises(PolicyViolation):
            self.policy.check_url("http://evil.example.com/")

    def test_blocks_admin_path_even_in_domain(self):
        with self.assertRaises(PolicyViolation):
            self.policy.check_url("http://localhost:5000/admin/users")

    def test_blocks_disallowed_action_type(self):
        policy = AllowlistPolicy(allowed_domains=["localhost"], allowed_action_types={ActionType.NAVIGATE})
        with self.assertRaises(PolicyViolation):
            policy.check_action(ActionType.CLICK)


class RiskClassifierTests(unittest.TestCase):
    def test_safe_and_reversible_proceed_automatically(self):
        clf = RiskClassifier(irreversible_preapproved=False)
        safe_step = Step(step_id="s1", action=ActionType.CLICK, risk=RiskLevel.SAFE)
        rev_step = Step(step_id="s2", action=ActionType.FILL, risk=RiskLevel.REVERSIBLE)
        self.assertEqual(clf.decide(safe_step), RiskDecision.PROCEED)
        self.assertEqual(clf.decide(rev_step), RiskDecision.PROCEED)

    def test_irreversible_requires_confirmation_by_default(self):
        clf = RiskClassifier(irreversible_preapproved=False)
        step = Step(step_id="s3", action=ActionType.CLICK, risk=RiskLevel.IRREVERSIBLE)
        self.assertEqual(clf.decide(step), RiskDecision.REQUIRE_CONFIRMATION)

    def test_irreversible_proceeds_when_preapproved(self):
        clf = RiskClassifier(irreversible_preapproved=True)
        step = Step(step_id="s3", action=ActionType.CLICK, risk=RiskLevel.IRREVERSIBLE)
        self.assertEqual(clf.decide(step), RiskDecision.PROCEED)


if __name__ == "__main__":
    unittest.main()
