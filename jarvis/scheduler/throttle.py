"""Anti-fatigue notification throttling (v3.5).

Enforces a 15-minute quiet window per category for low/normal notifications
to protect the user from proactive notification fatigue. High and urgent
notifications bypass the throttle while maintaining audit accountability.
"""

from datetime import datetime, timezone
import threading
from uuid import UUID

DEFAULT_THROTTLE_SECONDS = 15 * 60  # 15 minutes


class AntiFatigueThrottler:
    def __init__(self, throttle_seconds: int = DEFAULT_THROTTLE_SECONDS) -> None:
        self.throttle_seconds = throttle_seconds
        self._lock = threading.Lock()
        # Key: (owner_id, category) -> datetime of last allowed notification
        self._last_dispatched: dict[tuple[UUID, str], datetime] = {}

    def should_allow(
        self,
        owner_id: UUID,
        category: str,
        priority: str = "normal",
        now: datetime | None = None,
    ) -> tuple[bool, str | None]:
        """Evaluates whether a notification is allowed to fire.

        Returns:
            (allowed, reason)
        """
        now = now or datetime.now(timezone.utc)
        cat_key = category.strip().lower()
        key = (owner_id, cat_key)

        # High and urgent notifications always bypass quiet periods
        if priority.lower() in ("high", "urgent"):
            with self._lock:
                self._last_dispatched[key] = now
            return True, None

        with self._lock:
            last_time = self._last_dispatched.get(key)
            if last_time is not None:
                elapsed = (now - last_time).total_seconds()
                if elapsed < self.throttle_seconds:
                    remaining = int(self.throttle_seconds - elapsed)
                    return (
                        False,
                        f"Proactive notification throttled for category '{cat_key}'. "
                        f"Cooldown active for another {remaining}s.",
                    )
            self._last_dispatched[key] = now
            return True, None

    def reset(self, owner_id: UUID | None = None) -> None:
        with self._lock:
            if owner_id is None:
                self._last_dispatched.clear()
            else:
                keys_to_del = [k for k in self._last_dispatched if k[0] == owner_id]
                for k in keys_to_del:
                    del self._last_dispatched[k]
