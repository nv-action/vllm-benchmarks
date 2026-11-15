"""Temporary artificial failure for Daily Issue chain verification.

This test deliberately fails so the publish_daily_issue workflow gets
exercised end-to-end on the nv-action test branch. Remove this file
after verification (revert the injecting commit).
"""


def test_force_fail_daily_issue_chain():
    assert False, "intentional failure: verify daily issue publishing chain"
