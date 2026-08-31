def test_policy_report_has_no_mutating_false_positive_helper():
    import scripts.sync_channel_policy as policy

    assert not hasattr(policy, "clear_false_positive_block")
