"""
GarminClient: the single point of access to the Garmin Connect API.

All calls to the garminconnect library go through this class.
Do not instantiate garminconnect.Garmin anywhere else in the codebase.

Authentication strategy (verified against garminconnect==0.3.5):
  - Garmin.login(tokenstore=path) handles both token-reuse and credential login.
  - If tokenstore exists and tokens are valid, credentials are not sent.
  - If tokens are absent/invalid, login falls back to credentials and saves
    fresh tokens to tokenstore automatically.
  - We set retry_attempts=0 so our RateLimiter controls all retry logic.

Rate limiting:
  - Every API call goes through RateLimiter.execute() for spacing and backoff.
  - 429 during login → LoginRateLimitedError (stop immediately, no retry).
  - 429 on other calls → exponential backoff up to max_retries.
"""

import logging
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from garmin_sync.config import Config
from garmin_sync.rate_limit import LoginRateLimitedError, RateLimiter

logger = logging.getLogger(__name__)


class GarminClient:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._client: Garmin | None = None
        self._rate_limiter = RateLimiter(
            base_delay_s=config.garmin_request_delay_seconds,
            max_retries=config.garmin_max_retries,
            backoff_base_s=config.garmin_backoff_base_seconds,
        )

    def authenticate(self) -> None:
        """Verify or establish authentication. Called by the `auth` CLI command."""
        self._ensure_client()
        logger.info("Authentication successful.")

    def _ensure_client(self) -> Garmin:
        if self._client is not None:
            return self._client

        email = self._config.garmin_email
        password = self._config.garmin_password.get_secret_value()
        token_path = self._config.garmin_token_path.expanduser().resolve()

        token_path.mkdir(parents=True, exist_ok=True)
        token_path_str = str(token_path)

        logger.debug("Initialising Garmin client (tokenstore=%s)", token_path_str)

        # retry_attempts=0: we handle retries via RateLimiter, not the library.
        # prompt_mfa: called by the library if Garmin requires a 2FA code.
        # Uses input() so it works in any terminal without importing typer here.
        garmin = Garmin(
            email=email,
            password=password,
            retry_attempts=0,
            prompt_mfa=lambda: input("Enter Garmin MFA/2FA code: "),
        )

        try:
            # login(tokenstore=...) tries tokens first; falls back to credentials
            # if needed, and saves fresh tokens automatically.
            garmin.login(tokenstore=token_path_str)
        except GarminConnectTooManyRequestsError as exc:
            raise LoginRateLimitedError(
                "Garmin returned 429 during login. Stopping immediately. "
                "Wait before retrying."
            ) from exc
        except GarminConnectAuthenticationError:
            logger.error("Garmin authentication failed. Check credentials in .env.")
            raise

        self._client = garmin
        logger.info("Garmin client authenticated.")
        return self._client

    # ------------------------------------------------------------------
    # Public API methods — all calls go through the rate limiter.
    # ------------------------------------------------------------------

    def get_activities(
        self,
        start: int = 0,
        limit: int = 20,
        activity_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Fetch activity summaries.

        :param start: Offset from the most recent activity (0 = newest).
        :param limit: Number of activities to return.
        :param activity_type: Optional Garmin activity type filter string.
        :return: List of raw activity dicts from Garmin.
        """
        client = self._ensure_client()
        logger.debug("get_activities(start=%d, limit=%d)", start, limit)

        kwargs: dict[str, Any] = {}
        if activity_type is not None:
            kwargs["activitytype"] = activity_type

        result = self._rate_limiter.execute(
            client.get_activities, start, limit, **kwargs
        )
        return result if isinstance(result, list) else []

    # ------------------------------------------------------------------
    # Health / wellness data
    # ------------------------------------------------------------------

    def get_user_summary(self, cdate: str) -> dict[str, Any]:
        """Daily wellness summary for a calendar date (YYYY-MM-DD)."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_user_summary, cdate)
        return result if isinstance(result, dict) else {}

    def get_sleep_data(self, cdate: str) -> dict[str, Any]:
        """Sleep data for a calendar date."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_sleep_data, cdate)
        return result if isinstance(result, dict) else {}

    def get_hrv_data(self, cdate: str) -> dict[str, Any]:
        """HRV summary for a calendar date. Returns empty dict if not available."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_hrv_data, cdate)
        return result if isinstance(result, dict) else {}

    def get_stress_data(self, cdate: str) -> dict[str, Any]:
        """Stress summary for a calendar date."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_stress_data, cdate)
        return result if isinstance(result, dict) else {}

    def get_body_battery(self, startdate: str, enddate: str) -> list[dict[str, Any]]:
        """Body battery readings for a date range (YYYY-MM-DD)."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_body_battery, startdate, enddate)
        return result if isinstance(result, list) else []

    def get_heart_rates(self, cdate: str) -> dict[str, Any]:
        """All-day heart rate data for a calendar date."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_heart_rates, cdate)
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Activity data
    # ------------------------------------------------------------------

    def get_activity_detail(self, activity_id: str) -> dict[str, Any]:
        """
        Fetch a single activity with its laps.

        Calls get_activity(activity_id) which returns the activity summary
        plus a 'laps' list. This is distinct from get_activity_details()
        which returns GPS/HR time-series data.

        :param activity_id: Garmin activity ID as a string.
        :return: Raw activity dict including 'laps' if Garmin returns them.
        """
        client = self._ensure_client()
        logger.debug("get_activity_detail(activity_id=%s)", activity_id)
        result = self._rate_limiter.execute(client.get_activity, activity_id)
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Performance / training metrics (Phase 5b)
    # ------------------------------------------------------------------

    def get_lactate_threshold(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Lactate threshold measurements for a date range."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(
            client.get_lactate_threshold,
            start_date=start_date, end_date=end_date, latest=False, aggregation="daily",
        )
        return result if isinstance(result, dict) else {}

    def get_race_predictions(self, start_date: str, end_date: str) -> list[dict[str, Any]]:
        """Race time predictions for a date range (max 1-year window per call)."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(
            client.get_race_predictions, startdate=start_date, enddate=end_date, _type="daily",
        )
        return result if isinstance(result, list) else []

    def get_endurance_score(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Endurance score for a date range."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(
            client.get_endurance_score, startdate=start_date, enddate=end_date,
        )
        return result if isinstance(result, dict) else {}

    def get_hill_score(self, start_date: str, end_date: str) -> dict[str, Any]:
        """Hill score for a date range."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(
            client.get_hill_score, startdate=start_date, enddate=end_date,
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------
    # Per-day performance metrics (Phase 5b.2)
    # ------------------------------------------------------------------

    def get_training_status(self, cdate: str) -> dict[str, Any]:
        """Training status (VO2max, training load balance, acute/chronic load) for a calendar date."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_training_status, cdate)
        return result if isinstance(result, dict) else {}

    def get_training_readiness(self, cdate: str) -> list[dict[str, Any]]:
        """Training readiness snapshots for a calendar date. Returns list (may have multiple entries)."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_training_readiness, cdate)
        return result if isinstance(result, list) else []

    def get_max_metrics(self, cdate: str) -> list[dict[str, Any]]:
        """VO2max and fitness age metrics for a calendar date."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_max_metrics, cdate)
        return result if isinstance(result, list) else []

    def get_fitnessage_data(self, cdate: str) -> dict[str, Any]:
        """Fitness age, achievable fitness age, and contributing components for a calendar date."""
        client = self._ensure_client()
        result = self._rate_limiter.execute(client.get_fitnessage_data, cdate)
        return result if isinstance(result, dict) else {}
