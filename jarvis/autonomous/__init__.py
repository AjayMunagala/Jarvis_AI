"""Autonomous AI Operating System Capstone Package (v4.0).

Coordinates and orchestrates all modular subsystems:
- Assistant (Tasks, Notes, Calendar)
- Workflow Engine (DAG, Rollback, Recovery)
- Knowledge Intelligence (Hybrid Search, Context Budgeting)
- Research & Document Intelligence (Parsing, Fencing, Synthesis)
- Proactive Assistance (Scheduling, Throttling)
- System Health & Telemetry (Diagnostics, Quarantine)
- Finance Intelligence (Accounts, Categorization, Net Worth)
- Controlled Self-Improvement (Offline Benchmarks, Diff Gating, 1-Click Restore)
"""

from jarvis.autonomous.coordinator import AutonomousCoordinator
from jarvis.autonomous.models import (
    AutonomousCommandRequest,
    AutonomousCommandResult,
    AutonomousStepLog,
    SubsystemStatus,
    SystemOverview,
)

__all__ = [
    "AutonomousCoordinator",
    "AutonomousCommandRequest",
    "AutonomousCommandResult",
    "AutonomousStepLog",
    "SubsystemStatus",
    "SystemOverview",
]
