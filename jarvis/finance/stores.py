"""Persistence layer for financial accounts, transactions, and snapshots (v3.7)."""

from abc import ABC, abstractmethod
from datetime import datetime, timezone
import json
import threading
from typing import Any
from uuid import UUID

from jarvis.finance.models import (
    FinancialAccount,
    FinancialSnapshot,
    FinancialTransaction,
)


class FinanceStore(ABC):
    @abstractmethod
    def save_account(self, account: FinancialAccount) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_account(self, owner_id: UUID, account_id: UUID) -> FinancialAccount | None:
        raise NotImplementedError

    @abstractmethod
    def list_accounts(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[FinancialAccount], int]:
        raise NotImplementedError

    @abstractmethod
    def delete_account(self, owner_id: UUID, account_id: UUID) -> bool:
        raise NotImplementedError

    @abstractmethod
    def update_account_balance(self, owner_id: UUID, account_id: UUID, delta: float) -> None:
        raise NotImplementedError

    @abstractmethod
    def save_transaction(self, tx: FinancialTransaction) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_transaction(self, owner_id: UUID, tx_id: UUID) -> FinancialTransaction | None:
        raise NotImplementedError

    @abstractmethod
    def list_transactions(
        self,
        owner_id: UUID,
        account_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FinancialTransaction], int]:
        raise NotImplementedError

    @abstractmethod
    def delete_transaction(self, owner_id: UUID, tx_id: UUID) -> bool:
        raise NotImplementedError

    @abstractmethod
    def record_transaction(self, tx: FinancialTransaction) -> None:
        """v4.4: store the transaction and adjust the account balance together, or do neither."""
        raise NotImplementedError

    @abstractmethod
    def delete_transaction_reversing(self, owner_id: UUID, tx_id: UUID) -> FinancialTransaction | None:
        """v4.4: delete the transaction and reverse its balance together; None if it is already gone."""
        raise NotImplementedError

    @abstractmethod
    def save_snapshot(self, snapshot: FinancialSnapshot) -> None:
        raise NotImplementedError

    @abstractmethod
    def get_latest_snapshot(self, owner_id: UUID) -> FinancialSnapshot | None:
        raise NotImplementedError


class MemoryFinanceStore(FinanceStore):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._accounts: dict[UUID, FinancialAccount] = {}
        self._transactions: dict[UUID, FinancialTransaction] = {}
        self._snapshots: dict[UUID, FinancialSnapshot] = {}

    def save_account(self, account: FinancialAccount) -> None:
        with self._lock:
            self._accounts[account.id] = account

    def get_account(self, owner_id: UUID, account_id: UUID) -> FinancialAccount | None:
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc and acc.owner_id == owner_id:
                return acc
            return None

    def list_accounts(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[FinancialAccount], int]:
        with self._lock:
            matching = [a for a in self._accounts.values() if a.owner_id == owner_id]
            matching.sort(key=lambda a: (a.created_at, a.id), reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def delete_account(self, owner_id: UUID, account_id: UUID) -> bool:
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc and acc.owner_id == owner_id:
                del self._accounts[account_id]
                # Cascade transactions
                to_del = [t.id for t in self._transactions.values() if t.account_id == account_id]
                for tid in to_del:
                    del self._transactions[tid]
                return True
            return False

    def update_account_balance(self, owner_id: UUID, account_id: UUID, delta: float) -> None:
        with self._lock:
            acc = self._accounts.get(account_id)
            if acc and acc.owner_id == owner_id:
                new_bal = round(acc.balance + delta, 2)
                self._accounts[account_id] = FinancialAccount(
                    id=acc.id,
                    owner_id=acc.owner_id,
                    name=acc.name,
                    account_type=acc.account_type,
                    institution=acc.institution,
                    balance=new_bal,
                    currency=acc.currency,
                    metadata=acc.metadata,
                    created_at=acc.created_at,
                    updated_at=datetime.now(timezone.utc),
                )

    def save_transaction(self, tx: FinancialTransaction) -> None:
        with self._lock:
            self._transactions[tx.id] = tx

    def get_transaction(self, owner_id: UUID, tx_id: UUID) -> FinancialTransaction | None:
        with self._lock:
            t = self._transactions.get(tx_id)
            if t and t.owner_id == owner_id:
                return t
            return None

    def list_transactions(
        self,
        owner_id: UUID,
        account_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FinancialTransaction], int]:
        with self._lock:
            matching = [
                t for t in self._transactions.values()
                if t.owner_id == owner_id and (account_id is None or t.account_id == account_id)
            ]
            # Unique tiebreaker so bounded reads are deterministic (matches PostgreSQL ordering).
            matching.sort(key=lambda t: (t.transaction_date, t.id), reverse=True)
            total = len(matching)
            return matching[offset : offset + limit], total

    def delete_transaction(self, owner_id: UUID, tx_id: UUID) -> bool:
        with self._lock:
            t = self._transactions.get(tx_id)
            if t and t.owner_id == owner_id:
                del self._transactions[tx_id]
                return True
            return False

    def record_transaction(self, tx: FinancialTransaction) -> None:
        """v4.4: store the transaction and adjust the balance together, or do neither."""
        with self._lock:
            acc = self._accounts.get(tx.account_id)
            if acc is None or acc.owner_id != tx.owner_id:
                raise ValueError(f"Account {tx.account_id} not found.")
            self._transactions[tx.id] = tx
            self._accounts[tx.account_id] = acc.model_copy(update={
                "balance": round(acc.balance + tx.amount, 2), "updated_at": datetime.now(timezone.utc)})

    def delete_transaction_reversing(self, owner_id: UUID, tx_id: UUID) -> FinancialTransaction | None:
        """v4.4: delete the transaction and reverse its balance change together; None if it is already gone."""
        with self._lock:
            tx = self._transactions.get(tx_id)
            if tx is None or tx.owner_id != owner_id:
                return None
            del self._transactions[tx_id]
            acc = self._accounts.get(tx.account_id)
            if acc is not None and acc.owner_id == owner_id:
                self._accounts[tx.account_id] = acc.model_copy(update={
                    "balance": round(acc.balance - tx.amount, 2), "updated_at": datetime.now(timezone.utc)})
            return tx

    def save_snapshot(self, snapshot: FinancialSnapshot) -> None:
        with self._lock:
            self._snapshots[snapshot.id] = snapshot

    def get_latest_snapshot(self, owner_id: UUID) -> FinancialSnapshot | None:
        with self._lock:
            matching = [s for s in self._snapshots.values() if s.owner_id == owner_id]
            if not matching:
                return None
            matching.sort(key=lambda s: s.as_of_date, reverse=True)
            return matching[0]


class PostgresFinanceStore(FinanceStore):
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

    def save_account(self, account: FinancialAccount) -> None:
        query = """
            INSERT INTO jarvis_app.financial_accounts (
                id, owner_id, name, account_type, institution, balance, currency,
                metadata, created_at, updated_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                account_type = EXCLUDED.account_type,
                institution = EXCLUDED.institution,
                balance = EXCLUDED.balance,
                currency = EXCLUDED.currency,
                metadata = EXCLUDED.metadata,
                updated_at = EXCLUDED.updated_at;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        account.id,
                        account.owner_id,
                        account.name,
                        account.account_type,
                        account.institution,
                        account.balance,
                        account.currency,
                        json.dumps(account.metadata),
                        account.created_at,
                        account.updated_at,
                    ),
                )
            conn.commit()

    def get_account(self, owner_id: UUID, account_id: UUID) -> FinancialAccount | None:
        query = """
            SELECT id, owner_id, name, account_type, institution, balance, currency,
                   metadata, created_at, updated_at
            FROM jarvis_app.financial_accounts
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (account_id, owner_id))
                r = cur.fetchone()
                if not r:
                    return None
                return FinancialAccount(
                    id=r[0],
                    owner_id=r[1],
                    name=r[2],
                    account_type=r[3],
                    institution=r[4],
                    balance=float(r[5]),
                    currency=r[6],
                    metadata=r[7] if isinstance(r[7], dict) else json.loads(r[7]),
                    created_at=r[8],
                    updated_at=r[9],
                )

    def list_accounts(
        self, owner_id: UUID, limit: int = 50, offset: int = 0
    ) -> tuple[list[FinancialAccount], int]:
        count_q = "SELECT COUNT(*) FROM jarvis_app.financial_accounts WHERE owner_id = %s;"
        query = """
            SELECT id, owner_id, name, account_type, institution, balance, currency,
                   metadata, created_at, updated_at
            FROM jarvis_app.financial_accounts
            WHERE owner_id = %s
            ORDER BY created_at DESC, id DESC
            LIMIT %s OFFSET %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_q, (owner_id,))
                total = cur.fetchone()[0]
                cur.execute(query, (owner_id, limit, offset))
                rows = cur.fetchall()
                accs = [
                    FinancialAccount(
                        id=r[0],
                        owner_id=r[1],
                        name=r[2],
                        account_type=r[3],
                        institution=r[4],
                        balance=float(r[5]),
                        currency=r[6],
                        metadata=r[7] if isinstance(r[7], dict) else json.loads(r[7]),
                        created_at=r[8],
                        updated_at=r[9],
                    )
                    for r in rows
                ]
                return accs, total

    def delete_account(self, owner_id: UUID, account_id: UUID) -> bool:
        query = "DELETE FROM jarvis_app.financial_accounts WHERE id = %s AND owner_id = %s;"
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (account_id, owner_id))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    def update_account_balance(self, owner_id: UUID, account_id: UUID, delta: float) -> None:
        query = """
            UPDATE jarvis_app.financial_accounts
            SET balance = balance + %s, updated_at = CURRENT_TIMESTAMP
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (delta, account_id, owner_id))
            conn.commit()

    def save_transaction(self, tx: FinancialTransaction) -> None:
        query = """
            INSERT INTO jarvis_app.financial_transactions (
                id, account_id, owner_id, amount, category, description,
                transaction_date, created_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        tx.id,
                        tx.account_id,
                        tx.owner_id,
                        tx.amount,
                        tx.category,
                        tx.description,
                        tx.transaction_date,
                        tx.created_at,
                    ),
                )
            conn.commit()

    def get_transaction(self, owner_id: UUID, tx_id: UUID) -> FinancialTransaction | None:
        query = """
            SELECT id, account_id, owner_id, amount, category, description,
                   transaction_date, created_at
            FROM jarvis_app.financial_transactions
            WHERE id = %s AND owner_id = %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (tx_id, owner_id))
                r = cur.fetchone()
                if not r:
                    return None
                return FinancialTransaction(
                    id=r[0],
                    account_id=r[1],
                    owner_id=r[2],
                    amount=float(r[3]),
                    category=r[4],
                    description=r[5],
                    transaction_date=r[6],
                    created_at=r[7],
                )

    def list_transactions(
        self,
        owner_id: UUID,
        account_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[FinancialTransaction], int]:
        filter_acc = "AND account_id = %s" if account_id else ""
        params_count = [owner_id] + ([account_id] if account_id else [])
        count_q = f"SELECT COUNT(*) FROM jarvis_app.financial_transactions WHERE owner_id = %s {filter_acc};"
        params_query = params_count + [limit, offset]
        query = f"""
            SELECT id, account_id, owner_id, amount, category, description,
                   transaction_date, created_at
            FROM jarvis_app.financial_transactions
            WHERE owner_id = %s {filter_acc}
            ORDER BY transaction_date DESC, id DESC
            LIMIT %s OFFSET %s;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(count_q, tuple(params_count))
                total = cur.fetchone()[0]
                cur.execute(query, tuple(params_query))
                rows = cur.fetchall()
                txs = [
                    FinancialTransaction(
                        id=r[0],
                        account_id=r[1],
                        owner_id=r[2],
                        amount=float(r[3]),
                        category=r[4],
                        description=r[5],
                        transaction_date=r[6],
                        created_at=r[7],
                    )
                    for r in rows
                ]
                return txs, total

    def record_transaction(self, tx: FinancialTransaction) -> None:
        """v4.4: insert the transaction and adjust the balance in one database transaction."""
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE jarvis_app.financial_accounts SET balance = balance + %s, updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s AND owner_id = %s""",
                    (tx.amount, tx.account_id, tx.owner_id),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    raise ValueError(f"Account {tx.account_id} not found.")
                cur.execute(
                    """INSERT INTO jarvis_app.financial_transactions (
                           id, account_id, owner_id, amount, category, description, transaction_date, created_at
                       ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                    (tx.id, tx.account_id, tx.owner_id, tx.amount, tx.category, tx.description,
                     tx.transaction_date, tx.created_at),
                )
            conn.commit()

    def delete_transaction_reversing(self, owner_id: UUID, tx_id: UUID) -> FinancialTransaction | None:
        """v4.4: delete and reverse the balance in one database transaction; None if it is already gone."""
        tx = self.get_transaction(owner_id, tx_id)
        if tx is None:
            return None
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """DELETE FROM jarvis_app.financial_transactions WHERE id = %s AND owner_id = %s
                       RETURNING amount, account_id""",
                    (tx_id, owner_id),
                )
                row = cur.fetchone()
                if row is None:  # deleted concurrently: the balance was already reversed by that delete
                    conn.rollback()
                    return None
                cur.execute(
                    """UPDATE jarvis_app.financial_accounts SET balance = balance - %s, updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s AND owner_id = %s""",
                    (row[0], row[1], owner_id),
                )
            conn.commit()
        return tx

    def delete_transaction(self, owner_id: UUID, tx_id: UUID) -> bool:
        query = "DELETE FROM jarvis_app.financial_transactions WHERE id = %s AND owner_id = %s;"
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (tx_id, owner_id))
                deleted = cur.rowcount > 0
            conn.commit()
            return deleted

    def save_snapshot(self, snapshot: FinancialSnapshot) -> None:
        query = """
            INSERT INTO jarvis_app.financial_snapshots (
                id, owner_id, total_assets, total_liabilities, net_worth,
                monthly_cash_flow, breakdown_by_category, projected_runway_months, as_of_date
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    query,
                    (
                        snapshot.id,
                        snapshot.owner_id,
                        snapshot.total_assets,
                        snapshot.total_liabilities,
                        snapshot.net_worth,
                        snapshot.monthly_cash_flow,
                        json.dumps(snapshot.breakdown_by_category),
                        snapshot.projected_runway_months,
                        snapshot.as_of_date,
                    ),
                )
            conn.commit()

    def get_latest_snapshot(self, owner_id: UUID) -> FinancialSnapshot | None:
        query = """
            SELECT id, owner_id, total_assets, total_liabilities, net_worth,
                   monthly_cash_flow, breakdown_by_category, projected_runway_months, as_of_date
            FROM jarvis_app.financial_snapshots
            WHERE owner_id = %s
            ORDER BY as_of_date DESC
            LIMIT 1;
        """
        with self._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (owner_id,))
                r = cur.fetchone()
                if not r:
                    return None
                return FinancialSnapshot(
                    id=r[0],
                    owner_id=r[1],
                    total_assets=float(r[2]),
                    total_liabilities=float(r[3]),
                    net_worth=float(r[4]),
                    monthly_cash_flow=float(r[5]),
                    breakdown_by_category=r[6] if isinstance(r[6], dict) else json.loads(r[6]),
                    projected_runway_months=float(r[7]) if r[7] is not None else None,
                    as_of_date=r[8],
                )
