"""
Tests for RateLimiter behaviour.

Uses mock Garmin exceptions (not the real library) so no credentials needed.
Patches time.sleep to avoid actual delays.
"""

import time
from unittest.mock import MagicMock, call, patch

import pytest


# Create lightweight stand-in exceptions that mirror garminconnect's hierarchy.
class FakeGarminTooManyRequests(Exception):
    pass


class FakeGarminConnectionError(Exception):
    pass


class FakeGarminAuthError(Exception):
    pass


@pytest.fixture(autouse=True)
def patch_garmin_exceptions(monkeypatch):
    """Patch garminconnect exceptions so RateLimiter can be tested without the real library."""
    import garmin_sync.rate_limit as rl_module

    monkeypatch.setattr(
        "garminconnect.GarminConnectTooManyRequestsError", FakeGarminTooManyRequests
    )
    monkeypatch.setattr(
        "garminconnect.GarminConnectConnectionError", FakeGarminConnectionError
    )
    monkeypatch.setattr(
        "garminconnect.GarminConnectAuthenticationError", FakeGarminAuthError
    )


from garmin_sync.rate_limit import LoginRateLimitedError, RateLimitExceeded, RateLimiter


@pytest.fixture()
def limiter():
    return RateLimiter(
        base_delay_s=0.0,     # No real delay in tests.
        jitter_factor=0.0,    # No jitter in tests.
        max_retries=2,
        backoff_base_s=0.0,   # No backoff delay in tests.
    )


class TestBaseDelay:
    def test_sleep_called_before_fn(self, limiter):
        with patch("garmin_sync.rate_limit.time.sleep") as mock_sleep:
            fn = MagicMock(return_value="ok")
            result = limiter.execute(fn)
        assert result == "ok"
        mock_sleep.assert_called()  # At least one sleep call.

    def test_fn_return_value_passed_through(self, limiter):
        fn = MagicMock(return_value={"data": 42})
        result = limiter.execute(fn)
        assert result == {"data": 42}

    def test_fn_args_forwarded(self, limiter):
        fn = MagicMock(return_value=None)
        limiter.execute(fn, "arg1", key="val")
        fn.assert_called_once_with("arg1", key="val")


class TestTooManyRequestsRetry:
    def test_retries_on_429_then_raises(self, limiter):
        fn = MagicMock(side_effect=FakeGarminTooManyRequests("rate limited"))
        with pytest.raises(RateLimitExceeded):
            limiter.execute(fn)
        # Called once per attempt: initial + max_retries.
        assert fn.call_count == limiter.max_retries + 1

    def test_success_after_one_429(self, limiter):
        fn = MagicMock(side_effect=[FakeGarminTooManyRequests("rate limited"), "ok"])
        result = limiter.execute(fn)
        assert result == "ok"
        assert fn.call_count == 2

    def test_login_429_raises_immediately_without_retry(self, limiter):
        fn = MagicMock(side_effect=FakeGarminTooManyRequests("rate limited"))
        with pytest.raises(LoginRateLimitedError):
            limiter.execute(fn, is_login=True)
        fn.assert_called_once()  # No retry.

    def test_login_429_does_not_raise_rate_limit_exceeded(self, limiter):
        fn = MagicMock(side_effect=FakeGarminTooManyRequests())
        with pytest.raises(LoginRateLimitedError):
            limiter.execute(fn, is_login=True)


class TestConnectionErrorRetry:
    def test_retries_on_connection_error(self, limiter):
        fn = MagicMock(side_effect=FakeGarminConnectionError("timeout"))
        with pytest.raises(FakeGarminConnectionError):
            limiter.execute(fn)
        assert fn.call_count == limiter.max_retries + 1

    def test_success_after_connection_error(self, limiter):
        fn = MagicMock(side_effect=[FakeGarminConnectionError("timeout"), "recovered"])
        result = limiter.execute(fn)
        assert result == "recovered"


class TestAuthErrorNotRetried:
    def test_auth_error_raises_immediately(self, limiter):
        fn = MagicMock(side_effect=FakeGarminAuthError("bad creds"))
        with pytest.raises(FakeGarminAuthError):
            limiter.execute(fn)
        fn.assert_called_once()  # No retry.


class TestSuccessOnFirstAttempt:
    def test_clean_call_no_retry(self, limiter):
        fn = MagicMock(return_value=[1, 2, 3])
        result = limiter.execute(fn)
        assert result == [1, 2, 3]
        fn.assert_called_once()
