"""Helpers for bounding synchronous PostgreSQL repository operations."""

from time import monotonic
from typing import Callable


class OperationBudgetExceeded(TimeoutError):
    """The repository operation budget expired."""


def operation_deadline(
    timeout_seconds: int,
    *,
    outer_deadline: float | None = None,
    clock: Callable[[], float] = monotonic,
) -> float:
    """Return a local absolute deadline, never extending a caller's deadline."""
    local_deadline = clock() + timeout_seconds
    return min(local_deadline, outer_deadline) if outer_deadline is not None else local_deadline


def bounded_connect_timeout(
    configured_seconds: int, deadline: float, *, clock: Callable[[], float] = monotonic
) -> int:
    """Return a positive whole-second libpq timeout within the remaining budget."""
    remaining = deadline - clock()
    whole_seconds = int(remaining)
    if whole_seconds < 1:
        raise OperationBudgetExceeded()
    return min(configured_seconds, whole_seconds)


def execute_with_deadline(
    connection,
    deadline: float,
    query: str,
    parameters=(),
    *,
    clock: Callable[[], float] = monotonic,
):
    """Set a transaction-local statement timeout, then execute one statement."""
    remaining_ms = int((deadline - clock()) * 1000)
    if remaining_ms <= 0:
        raise OperationBudgetExceeded()
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (f"{remaining_ms}ms",),
    )
    if deadline - clock() <= 0:
        raise OperationBudgetExceeded()
    return connection.execute(query, parameters)
