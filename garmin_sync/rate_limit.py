"""
Rate limiter and exponential backoff helper.

All Garmin API calls should go through RateLimiter.execute() to get:
  - a configurable base delay between every call
  - random jitter
  - retry with exponential backoff on 429 and connection errors
  - immediate stop on 429 during login
"""

import logging
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class LoginRateLimitedError(Exception):
    """Raised when Garmin returns 429 during the login flow. Do not retry."""


class RateLimitExceeded(Exception):
    """Raised after max_retries 429 responses on a non-login call."""


class RateLimiter:
    def __init__(
        self,
        base_delay_s: float = 1.0,
        jitter_factor: float = 0.5,
        max_retries: int = 3,
        backoff_base_s: float = 2.0,
    ) -> None:
        self.base_delay_s = base_delay_s
        self.jitter_factor = jitter_factor
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s

    def _jitter(self) -> float:
        return self.base_delay_s * self.jitter_factor * random.random()

    def _backoff(self, attempt: int) -> float:
        return self.backoff_base_s * (2**attempt) + random.uniform(0, 1)

    def execute(
        self,
        fn: Callable[..., T],
        *args: Any,
        is_login: bool = False,
        **kwargs: Any,
    ) -> T:
        """
        Call fn(*args, **kwargs) with request spacing and retry/backoff.

        is_login=True: raise LoginRateLimitedError immediately on 429 (no retry).
        """
        # Import here to avoid circular dependency; garmin exceptions are only
        # available after garminconnect is installed.
        try:
            from garminconnect import (
                GarminConnectAuthenticationError,
                GarminConnectConnectionError,
                GarminConnectTooManyRequestsError,
            )
        except ImportError as exc:
            raise ImportError(
                "garminconnect is not installed. Run: pip install garminconnect"
            ) from exc

        delay = self.base_delay_s + self._jitter()
        logger.debug("Rate limiter: sleeping %.2fs before call", delay)
        time.sleep(delay)

        last_exc: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                return fn(*args, **kwargs)

            except GarminConnectTooManyRequestsError as exc:
                if is_login:
                    raise LoginRateLimitedError(
                        "Garmin returned 429 during login. Stopping immediately."
                    ) from exc
                last_exc = exc
                if attempt >= self.max_retries:
                    raise RateLimitExceeded(
                        f"Garmin rate-limited after {self.max_retries} retries."
                    ) from exc
                backoff = self._backoff(attempt)
                logger.warning(
                    "429 Too Many Requests. Backing off %.1fs (attempt %d/%d).",
                    backoff,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(backoff)

            except GarminConnectConnectionError as exc:
                # 4xx errors are client errors — never retryable.
                msg = str(exc)
                if "400" in msg or "401" in msg or "403" in msg or "404" in msg:
                    raise
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                backoff = self._backoff(attempt)
                logger.warning(
                    "Connection error: %s. Backing off %.1fs (attempt %d/%d).",
                    exc,
                    backoff,
                    attempt + 1,
                    self.max_retries,
                )
                time.sleep(backoff)

            except GarminConnectAuthenticationError:
                raise  # Not retryable.

        # Should not reach here, but satisfies type checker.
        raise last_exc  # type: ignore[misc]
