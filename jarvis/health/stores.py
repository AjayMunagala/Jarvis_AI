"""Persistence layer for health snapshots and file quarantine (v3.6)."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import threading
from typing import Any
from uuid import UUID

from jarvis.health.models import HealthSnapshot, QuarantinedFile


class HealthStore(ABC):
    @abstractmethod
    def save_snapshot(self, snapshot: HealthSnapshot) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_snapshot(self, owner_id: UUID) -> HealthSnapshot | None:
        raise NotImplementedError

    @abstractmethod
    def list_snapshots(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[HealthSnapshot], int]:
        raise NotImplementedError

    @abstractmethod
    def save_quarantined_file(self, record: QuarantinedFile) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_quarantined_file(self, owner_id: UUID, record_id: UUID) -> QuarantinedFile | None:
        raise NotImplementedError

    @abstractmethod
    def list_quarantined_files(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[QuarantinedFile], int]:
        raise NotImplementedError

    @abstractmethod
    def update_quarantined_file(self, record: QuarantinedFile) -> None:
        raise NotImplementedError


class MemoryHealthStore(HealthStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: dict[UUID, HealthSnapshot] = {}
        self._quarantined: dict[UUID, QuarantinedFile] = {}

    def save_snapshot(self, snapshot: HealthSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.id] = snapshot

    def get_latest_snapshot(self, owner_id: UUID) -> HealthSnapshot | None:
        with self._lock:
            matching = [s for s in self._snapshots.values() if s.owner_id == owner_id]
            if not matching:
                return None
            matching.sort(key=lambda s: s.created_at, reverse=True)
            return matching[0]

    def list_snapshots(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[HealthSnapshot], int]:
        with self._lock:
            matching = [s for s in self._snapshots.values() if s.owner_id == owner_id]
            matching.sort(key=lambda s: s.created_at, reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def save_quarantined_file(self, record: QuarantinedFile) -> None:
        with self._lock:
            self._quarantined[record.id] = record

    def get_quarantined_file(self, owner_id: UUID, record_id: UUID) -> QuarantinedFile | None:
        with self._lock:
            rec = self._quarantined.get(record_id)
            if rec and rec.owner_id == owner_id:
                return rec
            return None

    def list_quarantined_files(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[QuarantinedFile], int]:
        with self._lock:
            matching = [q for q in self._quarantined.values() if q.owner_id == owner_id]
            matching.sort(key=lambda q: q.quarantined_at, reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def update_quarantined_file(self, record: QuarantinedFile) -> None:
        with self._lock:
            self._quarantined[record.id] = record


class PostgresHealthStore(HealthStore):
    def __init__(
        self,
        db_url: str,
        connect_timeout_seconds: float = 5.0,
        operation_timeout_seconds: float = 10.0,
        **kwargs: Any,
    ) -> None:
        self.db_url = db_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.operation_timeout_seconds = operation_timeout_seconds

    def _get_connection(self) -> Any:
        import psycopg
        return psycopg.connect(
            self.db_url,
            connect_timeout=int(self.connect_timeout_seconds),
        )

    def save_snapshot(self, snapshot: HealthSnapshot) -> None:
        query = """
            INSERT INTO jarvis_app.health_snapshots (
                id, owner_id, cpu_percent, memory_percent, disk_free_gb, services_status, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s);
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        snapshot.id,
                        snapshot.owner_id,
                        snapshot.cpu_percent,
                        snapshot.memory_percent,
                        snapshot.disk_free_gb,
                        json.dumps(snapshot.services_status),
                        snapshot.created_at,
                    ),
                )
            conn.commit()

    def get_latest_snapshot(self, owner_id: UUID) -> HealthSnapshot | None:
        query = """
            SELECT id, owner_id, cpu_percent, memory_percent, disk_free_gb, services_status, created_at
            FROM jarvis_app.health_snapshots
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT 1;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (owner_id,))
                row = cur.fetchone()
                if not row:
                    return None
                return HealthSnapshot(
                    id=row[0],
                    owner_id=row[1],
                    cpu_percent=None if row[2] is None else float(row[2]),
                    memory_percent=None if row[3] is None else float(row[3]),
                    disk_free_gb=None if row[4] is None else float(row[4]),
                    services_status=row[5] if isinstance(row[5], dict) else json.loads(row[5]),
                    created_at=row[6],
                )

    def list_snapshots(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[HealthSnapshot], int]:
        count_q = "SELECT COUNT(*) FROM jarvis_app.health_snapshots WHERE owner_id = %s;"
        query = """
            SELECT id, owner_id, cpu_percent, memory_percent, disk_free_gb, services_status, created_at
            FROM jarvis_app.health_snapshots
            WHERE owner_id = %s
            ORDER BY created_at DESC
            LIMIT %s OFFSET %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_q, (owner_id,))
                total = cur.fetchone()[0]
                cur.execute(query, (owner_id, limit, offset))
                rows = cur.fetchall()
                snaps = [
                    HealthSnapshot(
                        id=r[0],
                        owner_id=r[1],
                        cpu_percent=None if r[2] is None else float(r[2]),
                        memory_percent=None if r[3] is None else float(r[3]),
                        disk_free_gb=None if r[4] is None else float(r[4]),
                        services_status=r[5] if isinstance(r[5], dict) else json.loads(r[5]),
                        created_at=r[6],
                    )
                    for r in rows
                ]
                return snaps, total

    def save_quarantined_file(self, record: QuarantinedFile) -> None:
        query = """
            INSERT INTO jarvis_app.quarantined_files (
                id, owner_id, original_path, quarantine_path, reason, hash_sha256,
                approved_by, quarantined_at, restored_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        record.id,
                        record.owner_id,
                        record.original_path,
                        record.quarantine_path,
                        record.reason,
                        record.hash_sha256,
                        record.approved_by,
                        record.quarantined_at,
                        record.restored_at,
                    ),
                )
            conn.commit()

    def get_quarantined_file(self, owner_id: UUID, record_id: UUID) -> QuarantinedFile | None:
        query = """
            SELECT id, owner_id, original_path, quarantine_path, reason, hash_sha256,
                   approved_by, quarantined_at, restored_at
            FROM jarvis_app.quarantined_files
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (record_id, owner_id))
                row = cur.fetchone()
                if not row:
                    return None
                return QuarantinedFile(
                    id=row[0],
                    owner_id=row[1],
                    original_path=row[2],
                    quarantine_path=row[3],
                    reason=row[4],
                    hash_sha256=row[5],
                    approved_by=row[6],
                    quarantined_at=row[7],
                    restored_at=row[8],
                )

    def list_quarantined_files(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[QuarantinedFile], int]:
        count_q = "SELECT COUNT(*) FROM jarvis_app.quarantined_files WHERE owner_id = %s;"
        query = """
            SELECT id, owner_id, original_path, quarantine_path, reason, hash_sha256,
                   approved_by, quarantined_at, restored_at
            FROM jarvis_app.quarantined_files
            WHERE owner_id = %s
            ORDER BY quarantined_at DESC
            LIMIT %s OFFSET %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_q, (owner_id,))
                total = cur.fetchone()[0]
                cur.execute(query, (owner_id, limit, offset))
                rows = cur.fetchall()
                records = [
                    QuarantinedFile(
                        id=r[0],
                        owner_id=r[1],
                        original_path=r[2],
                        quarantine_path=r[3],
                        reason=r[4],
                        hash_sha256=r[5],
                        approved_by=r[6],
                        quarantined_at=r[7],
                        restored_at=r[8],
                    )
                    for r in rows
                ]
                return records, total

    def update_quarantined_file(self, record: QuarantinedFile) -> None:
        query = """
            UPDATE jarvis_app.quarantined_files
            SET restored_at = %s
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (record.restored_at, record.id, record.owner_id))
            conn.commit()
