"""System health diagnostic probes and secret pattern scanner (v3.6)."""

import os
from pathlib import Path
import re
import shutil
import sys
import time
from typing import Any

from jarvis.health.models import SecretFinding

SECRET_PATTERNS = [
    ("aws_access_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "api_secret_key",
        re.compile(
            r"(?i)\b(?:api_key|apikey|secret_key|client_secret|bearer)\s*[:=]\s*['\"]([a-zA-Z0-9_\-\.]{20,})['\"]"
        ),
    ),
]

IGNORE_DIRS = {".git", ".venv", ".venv-dev", "node_modules", "__pycache__", ".quarantine", "dist", "build"}


def _windows_memory_percent() -> float | None:
    import ctypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)) or not status.ullTotalPhys:
        return None
    return round(100.0 * (status.ullTotalPhys - status.ullAvailPhys) / status.ullTotalPhys, 2)


def _windows_cpu_times() -> tuple[int, int] | None:
    import ctypes
    from ctypes import wintypes

    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None

    def ticks(value) -> int:
        return (value.dwHighDateTime << 32) | value.dwLowDateTime

    # Kernel time includes idle time on Windows.
    return ticks(idle), ticks(kernel) + ticks(user)


def _linux_memory_percent() -> float | None:
    values = {}
    with open("/proc/meminfo", encoding="ascii") as handle:
        for line in handle:
            name, _, rest = line.partition(":")
            values[name] = int(rest.split()[0])
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None
    return round(100.0 * (total - available) / total, 2)


def _linux_cpu_times() -> tuple[int, int] | None:
    with open("/proc/stat", encoding="ascii") as handle:
        fields = [int(item) for item in handle.readline().split()[1:]]
    if len(fields) < 5:
        return None
    idle = fields[3] + fields[4]  # idle + iowait
    return idle, sum(fields)


def measure_memory_percent() -> float | None:
    try:
        if sys.platform == "win32":
            return _windows_memory_percent()
        if sys.platform.startswith("linux"):
            return _linux_memory_percent()
    except (OSError, ValueError, AttributeError):
        return None
    return None


def measure_cpu_percent(interval_seconds: float = 0.2) -> float | None:
    """Overall CPU busy percentage over a short sampling interval, or None if unmeasurable."""
    sampler = _windows_cpu_times if sys.platform == "win32" else (
        _linux_cpu_times if sys.platform.startswith("linux") else None)
    if sampler is None:
        return None
    try:
        first = sampler()
        time.sleep(interval_seconds)
        second = sampler()
    except (OSError, ValueError, AttributeError):
        return None
    if first is None or second is None:
        return None
    idle_delta, total_delta = second[0] - first[0], second[1] - first[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, 100.0 * (1 - idle_delta / total_delta))), 2)


def get_system_telemetry(repo_root: Path | None = None) -> dict[str, float | None]:
    """Measured CPU percent, memory percent and free disk space (GB).

    v4.2: values are measured from the operating system (Windows API or
    /proc) without extra dependencies. A value that cannot be measured is
    None ("unavailable"); nothing is invented. Before v4.2 CPU and memory
    were fixed constants (15.0 and 42.0) and disk fell back to 50.0.
    """
    target_path = repo_root or Path.cwd()
    try:
        usage = shutil.disk_usage(target_path)
        disk_free_gb: float | None = round(usage.free / (1024 ** 3), 2)
    except OSError:
        disk_free_gb = None
    return {
        "cpu_percent": measure_cpu_percent(),
        "memory_percent": measure_memory_percent(),
        "disk_free_gb": disk_free_gb,
    }


def scan_file_for_secrets(file_path: Path) -> list[SecretFinding]:
    """Inspects a single text file for leaked credentials or secret tokens."""
    findings: list[SecretFinding] = []
    try:
        if not file_path.is_file() or file_path.stat().st_size > 2 * 1024 * 1024:
            return findings

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f, start=1):
                for secret_type, pattern in SECRET_PATTERNS:
                    match = pattern.search(line)
                    if match:
                        matched_val = match.group(0)
                        masked = (
                            matched_val[:6] + "*" * (len(matched_val) - 6)
                            if len(matched_val) > 6
                            else "******"
                        )
                        snippet = f"Line {idx}: {masked}"
                        findings.append(
                            SecretFinding(
                                file_path=str(file_path),
                                secret_type=secret_type,
                                line_number=idx,
                                snippet_masked=snippet[:200],
                            )
                        )
    except Exception:
        pass
    return findings


def scan_directory_for_secrets(
    root_dir: Path, max_findings: int = 50
) -> list[SecretFinding]:
    """Walks the directory tree, skipping ignored roots, and aggregates secret findings."""
    all_findings: list[SecretFinding] = []
    for root, dirs, files in os.walk(root_dir):
        # In-place prune of ignored directories
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in (".py", ".env", ".json", ".yaml", ".yml", ".ts", ".tsx", ".js", ".sql", ".sh", ".txt"):
                file_findings = scan_file_for_secrets(Path(root) / fname)
                all_findings.extend(file_findings)
                if len(all_findings) >= max_findings:
                    return all_findings[:max_findings]
    return all_findings
