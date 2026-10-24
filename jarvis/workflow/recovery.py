"""Cross-module workflow recovery, 4-tier rollback classification, and self-healing (v3.8).

Defines recovery metadata schemas, retry policies, rollback tiers, and compensation handlers.
Truthful reporting: distinguishes failed_compensated from failed_uncompensated without silent swallow.
"""

from datetime import datetime, timezone
from enum import StrEnum
import logging
import time
from typing import Any, Callable
from uuid import UUID

logger = logging.getLogger(__name__)

MAX_STEP_RETRIES = 2
BACKOFF_SCHEDULE = [1.0, 4.0]  # Backoff delays in seconds


class RollbackTier(StrEnum):
    TIER_1_IDEMPOTENT = "TIER_1_IDEMPOTENT"
    TIER_2_COMPENSATING = "TIER_2_COMPENSATING"
    TIER_3_REVERSIBLE = "TIER_3_REVERSIBLE"
    TIER_4_MANUAL = "TIER_4_MANUAL"


class CompensationStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    COMPENSATED = "compensated"
    FAILED = "failed"
    MANUAL_REQUIRED = "manual_required"


def classify_action_tier(step_type: str, action: str) -> RollbackTier:
    """Classifies a workflow step action into one of four recovery tiers."""
    act = action.strip().lower()

    # Tier 4: External side effects / notification alerts already delivered
    if "notification" in act or "alert" in act or act == "notifications.send":
        return RollbackTier.TIER_4_MANUAL

    # Tier 1: Read-only, searches, listing, idempotent queries
    if (
        act.endswith(".list")
        or act.endswith(".search")
        or act.endswith(".get")
        or "diagnostics.run" in act
        or "snapshot" in act
        or step_type in ("noop", "transform")
    ):
        return RollbackTier.TIER_1_IDEMPOTENT

    # Tier 3: Reversible via snapshot or file restore
    if "quarantine" in act or "file" in act:
        return RollbackTier.TIER_3_REVERSIBLE

    # Tier 2: Entity creation with direct inverse deletion
    if (
        act.endswith(".create")
        or act.endswith(".record")
        or act.endswith(".ingest")
        or act.endswith(".parse")
    ):
        return RollbackTier.TIER_2_COMPENSATING

    return RollbackTier.TIER_2_COMPENSATING


class DeadLetterRecord:
    def __init__(
        self,
        run_id: UUID,
        step_id: str,
        owner_id: UUID,
        reason: str,
        tier: RollbackTier,
        details: dict[str, Any],
        created_at: datetime | None = None,
    ) -> None:
        self.run_id = run_id
        self.step_id = step_id
        self.owner_id = owner_id
        self.reason = reason
        self.tier = tier
        self.details = details
        self.created_at = created_at or datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "step_id": self.step_id,
            "owner_id": str(self.owner_id),
            "reason": self.reason,
            "tier": self.tier.value,
            "details": self.details,
            "created_at": self.created_at.isoformat(),
        }
