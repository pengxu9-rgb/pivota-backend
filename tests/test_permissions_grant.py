"""permissions_grant: the explicit-list matcher, with no role implying anything. has_permission keeps
its role rules on top of it, unchanged."""
from utils.auth import BILLING_CREDITS_APPROVE, has_permission, permissions_grant


def test_exact_and_wildcard_grants():
    assert permissions_grant(["billing.credits.approve"], BILLING_CREDITS_APPROVE)
    assert permissions_grant(["billing.credits.*"], BILLING_CREDITS_APPROVE)
    assert permissions_grant(["billing.*"], BILLING_CREDITS_APPROVE)
    assert permissions_grant(" reviews.read , billing.* ", BILLING_CREDITS_APPROVE)  # comma string


def test_nothing_else_grants_it():
    assert not permissions_grant([], BILLING_CREDITS_APPROVE)
    assert not permissions_grant(None, BILLING_CREDITS_APPROVE)
    assert not permissions_grant(["billing.credits.read", "billing.credit.approve", "reviews.*", "*"],
                                 BILLING_CREDITS_APPROVE)
    assert not permissions_grant(["billing.credits.approve"], "")


def test_has_permission_keeps_its_role_rules():
    assert has_permission({"role": "admin", "permissions": []}, "reviews.read")
    assert has_permission({"role": "super_admin"}, "anything.at.all")
    assert has_permission({"role": "employee"}, "reviews.read")
    assert has_permission({"role": "outsourced", "permissions": ["reviews.*"]}, "reviews.group.manage")
    assert not has_permission({"role": "outsourced", "permissions": ["reviews.read"]}, "reviews.write")
    assert has_permission({"role": "outsourced"}, "")  # its own empty-permission rule, first
