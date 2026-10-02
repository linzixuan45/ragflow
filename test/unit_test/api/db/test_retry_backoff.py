"""The stale-connection retry must not add a fixed delay before its first retry.

A pooled connection that MySQL has already dropped is a benign, recurring event:
the retry path replaces it before retrying, so sleeping a full ``retry_delay``
there turns a reconnection into a one-second stall on a user request. Regression
guard for the 1.005s quantum measured in M12 (see LocalAgentOps
``开发日志/corpus_fs耗时优化/优化实验记录/21-版本V3.9-…``).
"""

from unittest import mock

from api.db.db_models import _retry_backoff


class TestRetryBackoff:
    def test_first_retry_is_immediate(self):
        with mock.patch("api.db.db_models.time.sleep") as sleep:
            _retry_backoff(1, 0)
        sleep.assert_not_called()

    def test_later_retries_keep_exponential_backoff(self):
        with mock.patch("api.db.db_models.time.sleep") as sleep:
            _retry_backoff(1, 1)
            _retry_backoff(1, 2)
            _retry_backoff(1, 3)
        assert [call.args[0] for call in sleep.call_args_list] == [1, 2, 4]

    def test_configured_delay_is_respected(self):
        with mock.patch("api.db.db_models.time.sleep") as sleep:
            _retry_backoff(0.25, 2)
        assert [call.args[0] for call in sleep.call_args_list] == [0.5]
