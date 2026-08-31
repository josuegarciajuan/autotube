import json


def test_clear_false_positive_block_preserves_history():
    from scripts.sync_channel_policy import clear_false_positive_block

    class DB:
        def __init__(self):
            self.values = {}

        def set_system_state(self, key, value):
            self.values[key] = value

    db = DB()
    clear_false_positive_block(db, 5, {"video_id": "Ymfy2_h0tzw", "visibility": "private"})

    assert db.values["shorts_spam_blocked_until_5"] == ""
    verification = json.loads(db.values["spam_block_verification_5"])
    assert verification["status"] == "cleared_false_positive"
    assert verification["evidence"]["visibility"] == "private"
