"""Background scheduler runner (v4.5).

A single daemon thread calls ``SchedulerService.run_due`` every
``interval_seconds``: due reminders are delivered, due jobs run (each due run
at most once, by an atomic claim), and notifications held during quiet hours
are released. v7.3: after that, each registered hook runs (event sources poll
their due sources), each in its own try/except so a failing hook never fails the
tick. It is off unless ``JARVIS_SCHEDULER_RUNNER_ENABLED=true``,
following the project's execution-disabled defaults. A tick that fails is
logged and the next tick runs normally; stopping waits for the current tick.
"""

import logging
import threading
from typing import Callable
from uuid import UUID

logger = logging.getLogger(__name__)


class SchedulerRunner:
    def __init__(self, service, owner_id: UUID, *, interval_seconds: float = 30.0):
        self.service = service
        self.owner_id = owner_id
        self.interval_seconds = interval_seconds
        self.ticks = 0
        self.failed_ticks = 0
        self.consecutive_failures = 0
        self.last_summary: dict[str, int] | None = None
        # v7.3: (name, hook(owner_id) -> counts) run after the scheduler's own work on every tick.
        self.hooks: list[tuple[str, Callable[[UUID], dict[str, int]]]] = []
        self.failed_hooks = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="jarvis-scheduler-runner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def tick(self) -> dict[str, int] | None:
        try:
            self.last_summary = dict(self.service.run_due(self.owner_id))
            self.consecutive_failures = 0
            for name, hook in self.hooks:
                try:
                    for key, value in (hook(self.owner_id) or {}).items():
                        self.last_summary[f"{name}_{key}"] = value
                except Exception as exc:  # noqa: BLE001 - one failed hook must not fail the tick
                    self.failed_hooks += 1
                    logger.warning("Scheduler hook %s failed: %s", name, type(exc).__name__)
            return self.last_summary
        except Exception as exc:  # noqa: BLE001 - one failed tick must not stop the runner
            self.failed_ticks += 1
            self.consecutive_failures += 1
            logger.warning("Scheduler tick failed: %s", type(exc).__name__)
            return None
        finally:
            self.ticks += 1

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval_seconds)
