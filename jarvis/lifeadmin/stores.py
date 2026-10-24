"""Life administration storage: in-memory (non-durable) and PostgreSQL (durable) (v7.5).

Caps are checked in the same lock (memory) or transaction (PostgreSQL: a per-owner advisory lock for documents
and bills, the bill row lock for payments) as the insert, so concurrent creates cannot pass a cap. Recording a
payment advances the bill and stores the payment in one transaction, and only when the bill is still due on the
date the caller saw, so two concurrent "paid" calls cannot advance it twice. PostgreSQL queries are parameterized,
owner-scoped and run under a bounded operation deadline; failures are re-raised without driver messages or values.
"""

from datetime import date
from time import monotonic
import threading
from typing import Any, Callable, Literal, Protocol
from uuid import UUID

from jarvis.lifeadmin.models import Bill, Document, Payment, ReminderLink
from jarvis.persistence.operation_timeout import (
    bounded_connect_timeout, execute_with_deadline, operation_deadline,
)


class LifeAdminStorageError(Exception):
    """Sanitized storage failure for life administration."""


PaidOutcome = Literal["missing", "stale"] | int


class LifeAdminStore(Protocol):
    durable: bool

    def save_document(self, document: Document, *, max_documents: int) -> bool: ...
    def get_document(self, owner_id: UUID, document_id: UUID) -> Document | None: ...
    def list_documents(self, owner_id: UUID, *, limit: int, offset: int,
                       status: str | None = None) -> tuple[list[Document], int]: ...
    def update_document(self, document: Document) -> bool: ...
    def delete_document(self, owner_id: UUID, document_id: UUID) -> bool: ...
    def save_bill(self, bill: Bill, *, max_bills: int) -> bool: ...
    def get_bill(self, owner_id: UUID, bill_id: UUID) -> Bill | None: ...
    def list_bills(self, owner_id: UUID, *, limit: int, offset: int,
                   status: str | None = None) -> tuple[list[Bill], int]: ...
    def update_bill(self, bill: Bill) -> bool: ...
    def delete_bill(self, owner_id: UUID, bill_id: UUID) -> bool: ...
    def record_payment(self, bill: Bill, payment: Payment, *, expected_due: date | None,
                       max_payments: int) -> PaidOutcome: ...
    def list_payments(self, owner_id: UUID, bill_id: UUID, *, limit: int, offset: int) -> tuple[list[Payment], int]: ...
    def list_links(self, owner_id: UUID) -> list[ReminderLink]: ...
    def get_link(self, owner_id: UUID, item_id: UUID) -> ReminderLink | None: ...
    def save_link(self, link: ReminderLink) -> bool: ...
    def update_link(self, link: ReminderLink) -> bool: ...
    def delete_link(self, owner_id: UUID, item_id: UUID) -> bool: ...


def _document_order(item: Document):
    return (item.expires_on is None, item.expires_on or date.max, item.title.casefold(), str(item.id))


def _bill_order(item: Bill):
    return (item.next_due_on is None, item.next_due_on or date.max, item.name.casefold(), str(item.id))


def _payment_order(item: Payment):
    return (item.paid_on, item.created_at, str(item.id))


# ====================================================================== memory


class MemoryLifeAdminStore:
    """Thread-safe, non-durable store."""

    durable = False

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._documents: dict[UUID, Document] = {}
        self._bills: dict[UUID, Bill] = {}
        self._payments: dict[UUID, Payment] = {}
        self._links: dict[UUID, ReminderLink] = {}

    def __repr__(self) -> str:  # counts only: the stored values are the owner's private data
        return (f"MemoryLifeAdminStore(documents={len(self._documents)}, bills={len(self._bills)}, "
                f"payments={len(self._payments)}, links={len(self._links)})")

    # ---------------------------------------------------------------- documents

    def save_document(self, document: Document, *, max_documents: int) -> bool:
        with self._lock:
            if sum(1 for item in self._documents.values() if item.owner_id == document.owner_id) >= max_documents:
                return False
            self._documents[document.id] = document
            return True

    def get_document(self, owner_id, document_id):
        with self._lock:
            item = self._documents.get(document_id)
            return item if item is not None and item.owner_id == owner_id else None

    def list_documents(self, owner_id, *, limit, offset, status=None):
        with self._lock:
            items = [item for item in self._documents.values()
                     if item.owner_id == owner_id and (status is None or item.status == status)]
        items.sort(key=_document_order)
        return items[offset:offset + limit], len(items)

    def update_document(self, document: Document) -> bool:
        with self._lock:
            current = self._documents.get(document.id)
            if current is None or current.owner_id != document.owner_id:
                return False
            self._documents[document.id] = document
            return True

    def delete_document(self, owner_id, document_id) -> bool:
        with self._lock:
            item = self._documents.get(document_id)
            if item is None or item.owner_id != owner_id:
                return False
            del self._documents[document_id]
            self._links.pop(document_id, None)
            return True

    # ---------------------------------------------------------------- bills

    def save_bill(self, bill: Bill, *, max_bills: int) -> bool:
        with self._lock:
            if sum(1 for item in self._bills.values() if item.owner_id == bill.owner_id) >= max_bills:
                return False
            self._bills[bill.id] = bill
            return True

    def get_bill(self, owner_id, bill_id):
        with self._lock:
            item = self._bills.get(bill_id)
            return item if item is not None and item.owner_id == owner_id else None

    def list_bills(self, owner_id, *, limit, offset, status=None):
        with self._lock:
            items = [item for item in self._bills.values()
                     if item.owner_id == owner_id and (status is None or item.status == status)]
        items.sort(key=_bill_order)
        return items[offset:offset + limit], len(items)

    def update_bill(self, bill: Bill) -> bool:
        with self._lock:
            current = self._bills.get(bill.id)
            if current is None or current.owner_id != bill.owner_id:
                return False
            self._bills[bill.id] = bill
            return True

    def delete_bill(self, owner_id, bill_id) -> bool:
        with self._lock:
            item = self._bills.get(bill_id)
            if item is None or item.owner_id != owner_id:
                return False
            del self._bills[bill_id]
            for key in [key for key, payment in self._payments.items() if payment.bill_id == bill_id]:
                del self._payments[key]
            self._links.pop(bill_id, None)
            return True

    def record_payment(self, bill: Bill, payment: Payment, *, expected_due, max_payments):
        with self._lock:
            current = self._bills.get(bill.id)
            if current is None or current.owner_id != bill.owner_id:
                return "missing"
            if current.next_due_on != expected_due:
                return "stale"
            self._bills[bill.id] = bill
            self._payments[payment.id] = payment
            mine = sorted((item for item in self._payments.values() if item.bill_id == bill.id), key=_payment_order)
            excess = mine[:max(0, len(mine) - max_payments)]
            for item in excess:
                del self._payments[item.id]
            return len(excess)

    def list_payments(self, owner_id, bill_id, *, limit, offset):
        with self._lock:
            items = [item for item in self._payments.values() if item.owner_id == owner_id and item.bill_id == bill_id]
        items.sort(key=_payment_order, reverse=True)
        return items[offset:offset + limit], len(items)

    # ---------------------------------------------------------------- reminder links

    def list_links(self, owner_id):
        with self._lock:
            return [link for link in self._links.values() if link.owner_id == owner_id]

    def get_link(self, owner_id, item_id):
        with self._lock:
            link = self._links.get(item_id)
            return link if link is not None and link.owner_id == owner_id else None

    def save_link(self, link: ReminderLink) -> bool:
        with self._lock:
            items = self._documents if link.item_kind == "document" else self._bills
            item = items.get(link.item_id)
            if link.item_id in self._links or item is None or item.owner_id != link.owner_id:
                return False
            self._links[link.item_id] = link
            return True

    def update_link(self, link: ReminderLink) -> bool:
        with self._lock:
            current = self._links.get(link.item_id)
            if current is None or current.owner_id != link.owner_id or current.item_kind != link.item_kind:
                return False
            self._links[link.item_id] = link
            return True

    def delete_link(self, owner_id, item_id) -> bool:
        with self._lock:
            link = self._links.get(item_id)
            if link is None or link.owner_id != owner_id:
                return False
            del self._links[item_id]
            return True


# ====================================================================== PostgreSQL

_DOCUMENT_COLUMNS = ("id, owner_id, kind, title, holder_name, issuer, identifier_last4, issued_on, expires_on, "
                     "renewal_lead_days, notes, status, created_at, updated_at")
_BILL_COLUMNS = ("id, owner_id, name, payee, amount, currency, recurrence, interval_days, next_due_on, anchor_day, "
                 "end_on, autopay, account_id, category, notes, remind_days_before, status, created_at, updated_at")
_PAYMENT_COLUMNS = "id, owner_id, bill_id, paid_on, due_on, amount, currency, note, created_at"
_LINK_COLUMNS = "owner_id, item_kind, item_id, reminder_id, due_on, remind_at, scheduled_at, created_at, updated_at"


def _names(columns: str) -> list[str]:
    return [name.strip() for name in columns.split(",")]


def _placeholders(columns: str) -> str:
    return ", ".join(["%s"] * len(columns.split(",")))


def _row(model, columns: str, row):
    return model(**dict(zip(_names(columns), row)))


def _params(item, columns: str) -> tuple:
    return tuple(getattr(item, name) for name in _names(columns))


def _assignments(columns: str, skip: tuple[str, ...]) -> tuple[str, list[str]]:
    names = [name for name in _names(columns) if name not in skip]
    return ", ".join(f"{name} = %s" for name in names), names


class PostgresLifeAdminStore:
    durable = True

    def __init__(self, database_url: str, *, connect_timeout_seconds: int = 3, operation_timeout_seconds: int = 5,
                 connect: Callable | None = None) -> None:
        if connect is None:
            try:
                import psycopg
            except ImportError as exc:
                raise RuntimeError("PostgreSQL storage requires psycopg.") from exc
            connect = psycopg.connect
        self._database_url = database_url
        self._connect = connect
        self._connect_timeout = connect_timeout_seconds
        self._operation_timeout = operation_timeout_seconds

    def __repr__(self) -> str:
        return "PostgresLifeAdminStore()"

    def _run(self, work: Callable[[Callable[..., Any]], Any], failure: str) -> Any:
        deadline = operation_deadline(self._operation_timeout, clock=monotonic)
        try:
            timeout = bounded_connect_timeout(self._connect_timeout, deadline, clock=monotonic)
            with self._connect(self._database_url, connect_timeout=timeout) as conn:
                return work(lambda sql, params=(): execute_with_deadline(conn, deadline, sql, params, clock=monotonic))
        except Exception as exc:  # noqa: BLE001 - sanitized: no driver message, SQL, URL or value is exposed
            raise LifeAdminStorageError(failure) from exc

    def _save_capped(self, table: str, columns: str, item, cap: int, failure: str) -> bool:
        def work(execute):
            execute(f"SELECT pg_advisory_xact_lock(hashtextextended('jarvis.{table}:' || %s::text, 0))",
                    (item.owner_id,))
            if execute(f"SELECT count(*) FROM jarvis_app.{table} WHERE owner_id = %s",
                       (item.owner_id,)).fetchone()[0] >= cap:
                return False
            execute(f"INSERT INTO jarvis_app.{table} ({columns}) VALUES ({_placeholders(columns)})",
                    _params(item, columns))
            return True

        return self._run(work, failure)

    def _update(self, table: str, columns: str, item, failure: str) -> bool:
        assignments, names = _assignments(columns, ("id", "owner_id", "created_at"))
        params = tuple(getattr(item, name) for name in names) + (item.id, item.owner_id)
        return self._run(lambda execute: execute(
            f"UPDATE jarvis_app.{table} SET {assignments} WHERE id = %s AND owner_id = %s", params).rowcount == 1,
            failure)

    def _list(self, table: str, columns: str, model, owner_id, limit, offset, status, order: str, failure: str):
        where, params = "owner_id = %s", [owner_id]
        if status is not None:
            where += " AND status = %s"
            params.append(status)

        def work(execute):
            total = execute(f"SELECT count(*) FROM jarvis_app.{table} WHERE {where}", tuple(params)).fetchone()[0]
            rows = execute(f"SELECT {columns} FROM jarvis_app.{table} WHERE {where} ORDER BY {order} "
                           "LIMIT %s OFFSET %s", tuple(params) + (limit, offset)).fetchall()
            return [_row(model, columns, row) for row in rows], int(total)

        return self._run(work, failure)

    # ---------------------------------------------------------------- documents

    def save_document(self, document: Document, *, max_documents: int) -> bool:
        return self._save_capped("lifeadmin_documents", _DOCUMENT_COLUMNS, document, max_documents,
                                 "Failed to store the document.")

    def get_document(self, owner_id, document_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_DOCUMENT_COLUMNS} FROM jarvis_app.lifeadmin_documents WHERE id = %s AND owner_id = %s",
            (document_id, owner_id)).fetchone(), "Failed to read the document.")
        return _row(Document, _DOCUMENT_COLUMNS, row) if row else None

    def list_documents(self, owner_id, *, limit, offset, status=None):
        return self._list("lifeadmin_documents", _DOCUMENT_COLUMNS, Document, owner_id, limit, offset, status,
                          "expires_on ASC NULLS LAST, lower(title), id", "Failed to list documents.")

    def update_document(self, document: Document) -> bool:
        return self._update("lifeadmin_documents", _DOCUMENT_COLUMNS, document, "Failed to update the document.")

    def delete_document(self, owner_id, document_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.lifeadmin_documents WHERE id = %s AND owner_id = %s",
            (document_id, owner_id)).rowcount == 1, "Failed to delete the document.")

    # ---------------------------------------------------------------- bills

    def save_bill(self, bill: Bill, *, max_bills: int) -> bool:
        return self._save_capped("lifeadmin_bills", _BILL_COLUMNS, bill, max_bills, "Failed to store the bill.")

    def get_bill(self, owner_id, bill_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_BILL_COLUMNS} FROM jarvis_app.lifeadmin_bills WHERE id = %s AND owner_id = %s",
            (bill_id, owner_id)).fetchone(), "Failed to read the bill.")
        return _row(Bill, _BILL_COLUMNS, row) if row else None

    def list_bills(self, owner_id, *, limit, offset, status=None):
        return self._list("lifeadmin_bills", _BILL_COLUMNS, Bill, owner_id, limit, offset, status,
                          "next_due_on ASC NULLS LAST, lower(name), id", "Failed to list bills.")

    def update_bill(self, bill: Bill) -> bool:
        return self._update("lifeadmin_bills", _BILL_COLUMNS, bill, "Failed to update the bill.")

    def delete_bill(self, owner_id, bill_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.lifeadmin_bills WHERE id = %s AND owner_id = %s",
            (bill_id, owner_id)).rowcount == 1, "Failed to delete the bill.")

    def record_payment(self, bill: Bill, payment: Payment, *, expected_due, max_payments):
        assignments, names = _assignments(_BILL_COLUMNS, ("id", "owner_id", "created_at"))

        def work(execute):
            row = execute("SELECT next_due_on FROM jarvis_app.lifeadmin_bills WHERE id = %s AND owner_id = %s "
                          "FOR UPDATE", (bill.id, bill.owner_id)).fetchone()
            if row is None:
                return "missing"
            if row[0] != expected_due:
                return "stale"
            execute(f"UPDATE jarvis_app.lifeadmin_bills SET {assignments} WHERE id = %s AND owner_id = %s",
                    tuple(getattr(bill, name) for name in names) + (bill.id, bill.owner_id))
            execute(f"INSERT INTO jarvis_app.lifeadmin_payments ({_PAYMENT_COLUMNS}) "
                    f"VALUES ({_placeholders(_PAYMENT_COLUMNS)})", _params(payment, _PAYMENT_COLUMNS))
            # Keep the newest entries: delete everything after the first max_payments, newest first.
            return execute(
                """DELETE FROM jarvis_app.lifeadmin_payments WHERE id IN (
                       SELECT id FROM jarvis_app.lifeadmin_payments WHERE bill_id = %s AND owner_id = %s
                       ORDER BY paid_on DESC, created_at DESC, id DESC OFFSET %s)""",
                (bill.id, bill.owner_id, max_payments)).rowcount

        return self._run(work, "Failed to record the payment.")

    def list_payments(self, owner_id, bill_id, *, limit, offset):
        def work(execute):
            total = execute("SELECT count(*) FROM jarvis_app.lifeadmin_payments WHERE bill_id = %s AND owner_id = %s",
                            (bill_id, owner_id)).fetchone()[0]
            rows = execute(f"SELECT {_PAYMENT_COLUMNS} FROM jarvis_app.lifeadmin_payments "
                           "WHERE bill_id = %s AND owner_id = %s ORDER BY paid_on DESC, created_at DESC, id DESC "
                           "LIMIT %s OFFSET %s", (bill_id, owner_id, limit, offset)).fetchall()
            return [_row(Payment, _PAYMENT_COLUMNS, row) for row in rows], int(total)

        return self._run(work, "Failed to list payments.")

    # ---------------------------------------------------------------- reminder links

    def list_links(self, owner_id):
        rows = self._run(lambda execute: execute(
            f"SELECT {_LINK_COLUMNS} FROM jarvis_app.lifeadmin_reminder_links WHERE owner_id = %s",
            (owner_id,)).fetchall(), "Failed to list reminder links.")
        return [_row(ReminderLink, _LINK_COLUMNS, row) for row in rows]

    def get_link(self, owner_id, item_id):
        row = self._run(lambda execute: execute(
            f"SELECT {_LINK_COLUMNS} FROM jarvis_app.lifeadmin_reminder_links WHERE item_id = %s AND owner_id = %s",
            (item_id, owner_id)).fetchone(), "Failed to read the reminder link.")
        return _row(ReminderLink, _LINK_COLUMNS, row) if row else None

    def save_link(self, link: ReminderLink) -> bool:
        """Insert unless the item already has a link (or no longer exists); False means nothing was stored."""
        table = "lifeadmin_documents" if link.item_kind == "document" else "lifeadmin_bills"

        def work(execute):
            if execute(f"SELECT 1 FROM jarvis_app.{table} WHERE id = %s AND owner_id = %s",
                       (link.item_id, link.owner_id)).fetchone() is None:
                return False
            document_id = link.item_id if link.item_kind == "document" else None
            bill_id = link.item_id if link.item_kind == "bill" else None
            return execute(
                f"INSERT INTO jarvis_app.lifeadmin_reminder_links ({_LINK_COLUMNS}, document_id, bill_id) "
                f"VALUES ({_placeholders(_LINK_COLUMNS)}, %s, %s) ON CONFLICT (item_id) DO NOTHING",
                _params(link, _LINK_COLUMNS) + (document_id, bill_id)).rowcount == 1

        return self._run(work, "Failed to store the reminder link.")

    def update_link(self, link: ReminderLink) -> bool:
        return self._run(lambda execute: execute(
            """UPDATE jarvis_app.lifeadmin_reminder_links SET reminder_id = %s, due_on = %s, remind_at = %s,
                   scheduled_at = %s, updated_at = %s WHERE item_id = %s AND owner_id = %s AND item_kind = %s""",
            (link.reminder_id, link.due_on, link.remind_at, link.scheduled_at, link.updated_at, link.item_id,
             link.owner_id, link.item_kind)).rowcount == 1, "Failed to update the reminder link.")

    def delete_link(self, owner_id, item_id) -> bool:
        return self._run(lambda execute: execute(
            "DELETE FROM jarvis_app.lifeadmin_reminder_links WHERE item_id = %s AND owner_id = %s",
            (item_id, owner_id)).rowcount == 1, "Failed to delete the reminder link.")
