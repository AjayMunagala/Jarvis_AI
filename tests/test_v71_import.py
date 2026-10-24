"""v7.1: bank CSV import with data-quality checks, duplicate detection, two-step commit and undo."""

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from jarvis.core.config import Settings
from jarvis.finance import importing
from jarvis.finance.importing import (
    AmountError,
    ImportPreviewCache,
    ImportPreviewNotFound,
    ImportRefused,
    parse_amount,
    parse_date,
)
from jarvis.finance.models import (
    AccountCreateRequest,
    ImportCommitRequest,
    ImportMapping,
    ImportPreviewRequest,
    TransactionCreateRequest,
)
from jarvis.finance.service import FinanceService
from jarvis.finance.stores import MemoryFinanceStore
from jarvis.main import create_app
from jarvis.security.policy import Capability, CapabilityPolicy

OWNER = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
OTHER = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
UTC = timezone.utc


class RecordingAudit:
    def __init__(self):
        self.events = []

    def record(self, **kwargs):
        self.events.append(kwargs)


def b64(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.b64encode(data).decode("ascii")


def make_mapping(**overrides):
    values = {"date_column": "Date", "date_format": "YYYY-MM-DD", "description_column": "Description",
              "amount_column": "Amount"}
    values.update(overrides)
    return ImportMapping(**values)


def setup(balance=100.0, audit=None):
    service = FinanceService(MemoryFinanceStore(), audit_service=audit)
    account = service.create_account(OWNER, AccountCreateRequest(
        name="Checking", account_type="checking", institution="Bank", balance=balance))
    return service, account


def preview(service, account, data, owner=OWNER, now=NOW, filename="bank.csv", **mapping):
    request = ImportPreviewRequest(account_id=account.id, filename=filename, data_base64=b64(data),
                                   mapping=make_mapping(**mapping))
    return service.preview_import(owner, request, now=now)


def commit(service, result, owner=OWNER, **options):
    return service.commit_import(owner, result.id, ImportCommitRequest(**options))


def csv_text(*rows, header="Date,Description,Amount"):
    return "\n".join([header, *rows]) + "\n"


def stored(service, account):
    return service.list_transactions(OWNER, account_id=account.id, limit=200).items


# ============================================================= dates


@pytest.mark.parametrize("fmt,text", [
    ("YYYY-MM-DD", "2026-03-05"), ("DD/MM/YYYY", "05/03/2026"), ("MM/DD/YYYY", "03/05/2026"),
    ("DD-MM-YYYY", "05-03-2026"), ("YYYY/MM/DD", "2026/03/05"), ("DD.MM.YYYY", "05.03.2026"),
    ("DD.MM.YYYY", "5.3.2026"),
])
def test_each_date_format_parses_to_noon_utc(fmt, text):
    assert parse_date(text, fmt) == datetime(2026, 3, 5, 12, tzinfo=UTC)


@pytest.mark.parametrize("fmt,text,expected", [
    ("YYYY-MM-DD", "2026-03-05 14:30", datetime(2026, 3, 5, 14, 30, tzinfo=UTC)),
    ("YYYY-MM-DD", "2026-03-05T14:30:15Z", datetime(2026, 3, 5, 14, 30, 15, tzinfo=UTC)),
    ("DD/MM/YYYY", "05/03/2026 08:05:09", datetime(2026, 3, 5, 8, 5, 9, tzinfo=UTC)),
])
def test_dates_with_an_optional_time(fmt, text, expected):
    assert parse_date(text, fmt) == expected


@pytest.mark.parametrize("fmt,text", [
    ("DD/MM/YYYY", "31/02/2026"), ("DD/MM/YYYY", "2026-03-05"), ("MM/DD/YYYY", "13/01/2026"),
    ("YYYY-MM-DD", "yesterday"), ("YYYY-MM-DD", ""), ("YYYY-MM-DD", "2026-03-05 25:00"),
])
def test_invalid_dates_are_refused(fmt, text):
    assert parse_date(text, fmt) is None


def test_ambiguous_day_month_follows_the_chosen_format():
    service, account = setup()
    data = csv_text("03/05/2026,Rent,-100")
    us = preview(service, account, data, date_format="MM/DD/YYYY")
    eu = preview(service, account, data, date_format="DD/MM/YYYY")
    assert us.rows[0].transaction_date.date().isoformat() == "2026-03-05"
    assert eu.rows[0].transaction_date.date().isoformat() == "2026-05-03"


# ============================================================= amounts


@pytest.mark.parametrize("text,expected", [
    ("1,234.56", "1234.56"), ("$1,234.56", "1234.56"), ("-12.30", "-12.30"), ("(12.30)", "-12.30"),
    ("12.30-", "-12.30"), ("12.30 DR", "-12.30"), ("12.30 CR", "12.30"), ("12.30cr", "12.30"),
    ("USD 5.00", "5.00"), ("5.00 EUR", "5.00"), ("\u20ac5", "5"), ("+5", "5"), ("1 234.50", "1234.50"),
    ("1'234.50", "1234.50"), ("-$5.10", "-5.10"), ("$(5.10)", "-5.10"), ("1234", "1234"), (".5", "0.5"),
    ("\u00a3 1,000,000.00", "1000000.00"), ("\u22127.25", "-7.25"),
])
def test_dot_decimal_amounts(text, expected):
    assert parse_amount(text, ".") == Decimal(expected)


@pytest.mark.parametrize("text,expected", [
    ("1.234,56", "1234.56"), ("-3,50", "-3.50"), ("(3,50)", "-3.50"), ("\u20ac 1.234,56", "1234.56"),
    ("3,5 DR", "-3.5"), ("1 234,56", "1234.56"), ("12", "12"), ("1.234.567,00 EUR", "1234567.00"),
])
def test_comma_decimal_amounts(text, expected):
    assert parse_amount(text, ",") == Decimal(expected)


@pytest.mark.parametrize("text,style", [
    ("1,5", "."), ("1.234,56", "."), ("abc", "."), ("NaN", "."), ("inf", "."), ("Infinity", "."),
    ("--5", "."), ("(-5)", "."), ("-5 CR", "."), ("1,23,4", "."), ("1.5", ","), ("$", "."), ("1e5", "."),
])
def test_unparseable_or_non_finite_amounts_are_refused(text, style):
    with pytest.raises(AmountError):
        parse_amount(text, style)


def test_empty_amount_is_none():
    assert parse_amount("   ", ".") is None


def test_amount_errors_are_row_errors_with_no_cell_content():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Coffee,SECRETAMT", "2026-03-02,Tea,", "2026-03-03,Bun,1e400"))
    assert [row.status for row in result.rows] == ["error", "error", "error"]
    assert result.counts.errors == 3 and result.counts.valid == 0
    assert all("SECRETAMT" not in message for row in result.rows for message in row.messages)
    assert "amount is empty" in result.rows[1].messages


def test_amounts_with_more_than_two_decimals_are_rounded_with_a_warning():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Fuel,-10.005"))
    assert result.rows[0].status == "warning" and result.rows[0].amount == -10.01
    assert any("rounded" in message for message in result.rows[0].messages)


def test_out_of_range_amount_is_an_error():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Huge,99999999999999.00"))
    assert result.rows[0].status == "error" and "amount is out of range" in result.rows[0].messages


# ============================================================= debit / credit


def test_debit_and_credit_columns():
    service, account = setup()
    data = csv_text("2026-03-01,Groceries,12.00,", "2026-03-02,Salary,,2500.00", "2026-03-03,Both,5.00,5.00",
                    "2026-03-04,Neither,,", "2026-03-05,Zero credit,7.50,0.00", "2026-03-06,Signed debit,-3.00,",
                    header="Date,Description,Debit,Credit")
    result = preview(service, account, data, amount_column=None, debit_column="Debit", credit_column="Credit")
    by_row = {row.row_number: row for row in result.rows}
    assert by_row[1].amount == -12.0 and by_row[1].status == "ok"
    assert by_row[2].amount == 2500.0
    assert by_row[3].status == "error" and "both debit and credit are filled" in by_row[3].messages
    assert by_row[4].status == "error" and "neither debit nor credit has an amount" in by_row[4].messages
    assert by_row[5].amount == -7.5 and by_row[5].status == "ok"
    assert by_row[6].amount == -3.0
    assert result.total_valid_credits == 2500.0 and result.total_valid_debits == -22.5


def test_mapping_requires_exactly_one_amount_layout():
    with pytest.raises(ValidationError):
        make_mapping(amount_column="Amount", debit_column="Debit", credit_column="Credit")
    with pytest.raises(ValidationError):
        make_mapping(amount_column=None, debit_column="Debit")
    with pytest.raises(ValidationError):
        make_mapping(amount_column="Description")  # same column twice
    with pytest.raises(ValidationError):
        make_mapping(date_format="%Y-%m-%d")  # only the allowlisted formats


# ============================================================= encodings and delimiters


def test_utf8_bom_is_removed():
    service, account = setup()
    result = preview(service, account, "\ufeff" + csv_text("2026-03-01,Caf\u00e9,-4.00"))
    assert result.encoding == "utf-8-sig" and result.columns[0] == "Date"
    assert result.rows[0].description == "Caf\u00e9" and result.file_warnings == []


def test_cp1252_fallback_with_a_warning():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Caf\u00e9 \u20ac,-4.00").encode("cp1252"))
    assert result.encoding == "cp1252" and result.rows[0].description == "Caf\u00e9 \u20ac"
    assert any("Windows-1252" in warning for warning in result.file_warnings)


def test_utf16_with_bom_is_read():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Tea,-2.00", header="Date\tDescription\tAmount")
                     .replace(",", "\t").encode("utf-16"))
    assert result.encoding == "utf-16" and result.delimiter == "tab" and result.rows[0].amount == -2.0


def test_undecodable_bytes_are_refused():
    service, account = setup()
    with pytest.raises(ImportRefused):
        preview(service, account, b"Date,Description,Amount\n2026-03-01,\x81\x8d\x8f\x90,1\n")


@pytest.mark.parametrize("delimiter,name", [(",", "comma"), (";", "semicolon"), ("\t", "tab"), ("|", "pipe")])
def test_delimiter_is_sniffed(delimiter, name):
    service, account = setup()
    rows = ["2026-03-01", "Coffee, large", "-3.50"], ["2026-03-02", "Rent", "-900.00"]
    lines = [delimiter.join(["Date", "Description", "Amount"])]
    for row in rows:
        lines.append(delimiter.join(f'"{cell}"' if delimiter in cell or "," in cell else cell for cell in row))
    result = preview(service, account, "\n".join(lines) + "\n")
    assert result.delimiter == name
    assert [row.description for row in result.rows] == ["Coffee, large", "Rent"]


def test_semicolon_file_with_comma_decimals_is_not_mistaken_for_commas():
    service, account = setup()
    data = "Datum;Omschrijving;Bedrag\n01.03.2026;Koffie;-3,50\n02.03.2026;Huur;-1.200,00\n"
    result = preview(service, account, data, date_column="Datum", date_format="DD.MM.YYYY",
                     description_column="Omschrijving", amount_column="Bedrag", decimal_separator=",")
    assert result.delimiter == "semicolon" and [row.amount for row in result.rows] == [-3.5, -1200.0]


def test_explicit_delimiter_overrides_sniffing():
    service, account = setup()
    result = preview(service, account, "Date|Description|Amount\n2026-03-01|A,B|-1.00\n", delimiter="|")
    assert result.delimiter == "pipe" and result.rows[0].description == "A,B"


def test_header_matching_is_case_insensitive_and_missing_columns_are_refused():
    service, account = setup()
    result = preview(service, account, " DATE ,description,AMOUNT\n2026-03-01,Tea,-1\n")
    assert result.rows[0].status == "ok"
    with pytest.raises(ImportRefused) as refused:
        preview(service, account, csv_text("2026-03-01,Tea,-1"), amount_column="Value")
    assert "not in the file's header row" in refused.value.message
    with pytest.raises(ImportRefused):
        preview(service, account, "Date,Description,Amount,amount\n2026-03-01,Tea,-1,2\n")


# ============================================================= quality checks


def test_empty_description_is_an_error():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,   ,-1.00"))
    assert result.rows[0].status == "error" and "description is empty" in result.rows[0].messages


def test_future_dates_beyond_tomorrow_are_errors():
    service, account = setup()
    tomorrow = (NOW + timedelta(days=1)).date().isoformat()
    later = (NOW + timedelta(days=2)).date().isoformat()
    result = preview(service, account, csv_text(f"{tomorrow},Early,-1.00", f"{later},Late,-1.00"))
    assert result.rows[0].status == "ok"
    assert result.rows[1].status == "error" and "date is in the future (after tomorrow)" in result.rows[1].messages


def test_dates_older_than_twenty_years_are_errors():
    service, account = setup()
    result = preview(service, account, csv_text("2006-06-16,Recent enough,-1.00", "2006-06-14,Too old,-1.00"))
    assert result.rows[0].status == "ok"
    assert result.rows[1].status == "error" and "date is more than 20 years old" in result.rows[1].messages


def test_unparseable_date_is_an_error_naming_the_format_only():
    service, account = setup()
    result = preview(service, account, csv_text("not-a-date,Tea,-1.00"))
    assert result.rows[0].messages == ["date is not a valid YYYY-MM-DD date"]


def test_outliers_are_warnings_not_errors():
    service, account = setup()
    rows = [f"2026-03-{day:02d},Shop {day},-20.00" for day in range(1, 10)]
    result = preview(service, account, csv_text(*rows, "2026-03-20,Car,-15000.00", "2026-03-21,Laptop,-199.00"))
    car = result.rows[9]
    assert car.status == "warning" and any("unusually large" in m for m in car.messages)
    assert result.rows[10].status == "ok"  # over 10x the median but not over 1000
    assert result.counts.warnings == 1 and result.counts.errors == 0 and result.counts.valid == 11


def test_short_rows_are_errors_and_ragged_rows_warnings():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Tea", "2026-03-02,Coffee,-2.00,extra"))
    assert result.rows[0].status == "error"
    assert result.rows[1].status == "warning"


def test_blank_rows_are_skipped_and_counted():
    service, account = setup()
    result = preview(service, account, "Date,Description,Amount\n\n2026-03-01,Tea,-1\n,,\n")
    assert result.counts.rows == 1 and result.counts.blank_rows_skipped == 2


def test_duplicates_within_the_file():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Coffee  Shop,-3.50", "2026-03-01,coffee shop,-3.50",
                                                "2026-03-01,Coffee Shop 2,-3.50", "2026-03-02,Coffee Shop,-3.50"))
    assert [row.status for row in result.rows] == ["ok", "duplicate", "ok", "ok"]
    assert result.rows[1].messages == ["repeats row 1 of this file (same date, amount and description)"]
    assert result.counts.duplicates_in_file == 1 and result.counts.valid == 3


def test_duplicates_of_stored_transactions():
    service, account = setup()
    service.record_transaction(OWNER, TransactionCreateRequest(
        account_id=account.id, amount=-42.0, description="ACME   Store", transaction_date=datetime(2026, 3, 1, 9, tzinfo=UTC)))
    result = preview(service, account, csv_text("2026-03-01,acme store,-42.00", "2026-03-01,acme store,-42.01"))
    assert [row.status for row in result.rows] == ["duplicate", "ok"]
    assert result.counts.duplicates_existing == 1
    assert "already stored" in result.rows[0].messages[0]


def test_preview_writes_nothing_and_returns_the_first_fifty_rows():
    service, account = setup()
    rows = [f"2026-03-{1 + i % 28:02d},Item {i},-{i + 1}.00" for i in range(120)]
    result = preview(service, account, csv_text(*rows, "bad,Broken,-1"))
    assert len(result.rows) == 50 and result.counts.rows == 121
    assert result.issues[-1].row_number == 121 and result.issues[-1].status == "error"
    assert stored(service, account) == [] and service.get_account(OWNER, account.id).balance == 100.0
    assert result.date_from.date().isoformat() == "2026-03-01" and result.date_to.date().isoformat() == "2026-03-28"
    assert result.total_valid_amount == -sum(range(1, 121))


# ============================================================= commit, re-import, atomicity


def test_commit_inserts_valid_rows_with_categories_and_balance():
    service, account = setup(balance=1000.0)
    data = csv_text("2026-03-01,Grocery store,-50.25", "2026-03-02,Payroll ACME,2000.00", "bad,Broken,1",
                    "2026-03-03,Thing,-10.00")
    result = preview(service, account, data)
    done = commit(service, result)
    assert done.inserted == 3 and done.skipped_errors == 1 and done.balance_change == 1939.75
    assert done.account_balance == 2939.75
    txs = {tx.description: tx for tx in stored(service, account)}
    assert txs["Grocery store"].category == "food" and txs["Payroll ACME"].category == "income"
    assert txs["Thing"].category == "other"
    assert all(tx.import_batch_id == done.batch_id for tx in txs.values())


def test_category_column_is_used_when_present():
    service, account = setup()
    data = csv_text("2026-03-01,Grocery store,-5.00,Household", "2026-03-02,Netflix,-9.99,",
                    header="Date,Description,Amount,Category")
    done = commit(service, preview(service, account, data, category_column="Category"))
    categories = {tx.description: tx.category for tx in stored(service, account)}
    assert done.inserted == 2 and categories == {"Grocery store": "Household", "Netflix": "entertainment"}


def test_reimporting_the_same_file_adds_nothing():
    service, account = setup()
    data = csv_text("2026-03-01,Coffee,-3.50", "2026-03-02,Rent,-900.00")
    first = commit(service, preview(service, account, data))
    again = preview(service, account, data)
    assert again.counts.duplicates_existing == 2 and again.counts.valid == 0
    second = commit(service, again)
    assert second.inserted == 0 and second.batch_id is None and second.skipped_duplicates_existing == 2
    assert len(stored(service, account)) == 2 and service.get_account(OWNER, account.id).balance == round(100 - 903.5, 2)
    assert service.list_import_batches(OWNER).total == 1 and first.batch_id is not None


def test_identical_rows_can_be_kept_and_still_never_reimported():
    service, account = setup()
    data = csv_text("2026-03-01,Coffee,-3.50", "2026-03-01,Coffee,-3.50")
    kept = commit(service, preview(service, account, data), skip_duplicates=False)
    assert kept.inserted == 2
    again = preview(service, account, data)
    assert again.counts.duplicates_existing == 2
    assert commit(service, again, skip_duplicates=False).inserted == 0
    assert len(stored(service, account)) == 2


def test_existing_duplicates_are_skipped_even_when_duplicates_are_allowed():
    service, account = setup()
    service.record_transaction(OWNER, TransactionCreateRequest(
        account_id=account.id, amount=-5.0, description="Tea", transaction_date=datetime(2026, 3, 1, 12, tzinfo=UTC)))
    done = commit(service, preview(service, account, csv_text("2026-03-01,Tea,-5.00", "2026-03-01,Tea,-5.00")),
                  skip_duplicates=False)
    assert done.inserted == 1 and done.skipped_duplicates_existing == 1


def test_fingerprints_stop_rows_stored_between_preview_and_commit():
    service, account = setup()
    data = csv_text("2026-03-01,Coffee,-3.50")
    first, second = preview(service, account, data), preview(service, account, data)
    assert commit(service, first).inserted == 1
    late = commit(service, second)
    assert late.inserted == 0 and late.skipped_duplicates_existing == 1
    assert len(stored(service, account)) == 1


def test_include_warnings_false_skips_warning_rows():
    service, account = setup()
    data = csv_text("2026-03-01,Fuel,-10.005", "2026-03-02,Tea,-2.00")
    done = commit(service, preview(service, account, data), include_warnings=False)
    assert done.inserted == 1 and done.skipped_warnings == 1


def test_commit_is_all_or_nothing(monkeypatch):
    service, account = setup(balance=500.0)
    data = csv_text(*[f"2026-03-0{day},Row {day},-{day}.00" for day in range(1, 6)])
    result = preview(service, account, data)
    calls = {"n": 0}
    original = MemoryFinanceStore._stage_import_row

    def failing(self, staged, tx, key):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("simulated storage failure")
        original(self, staged, tx, key)

    monkeypatch.setattr(MemoryFinanceStore, "_stage_import_row", failing)
    with pytest.raises(RuntimeError):
        commit(service, result)
    assert stored(service, account) == [] and service.get_account(OWNER, account.id).balance == 500.0
    assert service.list_import_batches(OWNER).total == 0
    monkeypatch.setattr(MemoryFinanceStore, "_stage_import_row", original)
    retried = commit(service, result)  # the preview was kept for a retry
    assert retried.inserted == 5 and service.get_account(OWNER, account.id).balance == 485.0


def test_a_preview_commits_only_once():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Coffee,-3.50"))
    assert commit(service, result).inserted == 1
    with pytest.raises(ImportPreviewNotFound):
        commit(service, result)
    assert len(stored(service, account)) == 1 and service.get_account(OWNER, account.id).balance == 96.5


def test_previews_expire_after_thirty_minutes():
    service, account = setup()
    clock = {"t": 1000.0}
    service.import_previews.clock = lambda: clock["t"]
    result = preview(service, account, csv_text("2026-03-01,Coffee,-3.50"))
    assert result.expires_at - result.created_at == timedelta(minutes=30)
    clock["t"] += 30 * 60
    with pytest.raises(ImportPreviewNotFound):
        commit(service, result)
    assert stored(service, account) == [] and len(service.import_previews) == 0


def test_previews_are_capped_per_owner_and_in_total():
    service, account = setup()
    service.import_previews = ImportPreviewCache(per_owner=2, total=3)
    data = csv_text("2026-03-01,Coffee,-3.50")
    first, second, third = (preview(service, account, data) for _ in range(3))
    with pytest.raises(ImportPreviewNotFound):
        commit(service, first)  # evicted, oldest first
    assert commit(service, third).inserted == 1
    assert len(service.import_previews) == 1 and second.id


def test_another_owner_cannot_use_a_preview_or_import_into_an_account():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Coffee,-3.50"))
    with pytest.raises(ImportPreviewNotFound):
        commit(service, result, owner=OTHER)
    assert commit(service, result).inserted == 1  # still the owner's to commit
    with pytest.raises(importing.ImportAccountNotFound):
        preview(service, account, csv_text("2026-03-02,Tea,-1.00"), owner=OTHER)


def test_commit_into_a_deleted_account_is_refused_and_writes_nothing():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01,Coffee,-3.50"))
    service.delete_account(OWNER, account.id)
    with pytest.raises(importing.ImportAccountNotFound):
        commit(service, result)


# ============================================================= undo


def test_undo_removes_only_that_batch_and_restores_the_balance():
    service, account = setup(balance=1000.0)
    other_account = service.create_account(OWNER, AccountCreateRequest(
        name="Savings", account_type="savings", institution="Bank", balance=50.0))
    manual = service.record_transaction(OWNER, TransactionCreateRequest(
        account_id=account.id, amount=-25.0, description="Manual", transaction_date=datetime(2026, 3, 1, tzinfo=UTC)))
    one = commit(service, preview(service, account, csv_text("2026-03-01,A,-10.00", "2026-03-02,B,-20.00")))
    two = commit(service, preview(service, account, csv_text("2026-03-03,C,-5.00")))
    three = commit(service, preview(service, other_account, csv_text("2026-03-01,A,-10.00")))
    assert service.get_account(OWNER, account.id).balance == 940.0
    undone = service.undo_import(OWNER, one.batch_id)
    assert undone.removed == 2 and undone.balance_change == 30.0
    assert service.get_account(OWNER, account.id).balance == 970.0
    assert service.get_account(OWNER, other_account.id).balance == 40.0
    remaining = {tx.description for tx in stored(service, account)}
    assert remaining == {"Manual", "C"} and manual.id
    assert {b.id for b in service.list_import_batches(OWNER).items} == {two.batch_id, three.batch_id}
    assert service.undo_import(OWNER, one.batch_id) is None  # undone once
    # The undone rows can be imported again.
    assert commit(service, preview(service, account, csv_text("2026-03-01,A,-10.00"))).inserted == 1


def test_another_owner_cannot_undo_a_batch():
    service, account = setup()
    done = commit(service, preview(service, account, csv_text("2026-03-01,A,-10.00")))
    assert service.undo_import(OTHER, done.batch_id) is None
    assert len(stored(service, account)) == 1 and service.get_account(OWNER, account.id).balance == 90.0
    assert service.list_import_batches(OTHER).total == 0


def test_deleting_an_imported_transaction_frees_its_fingerprint():
    service, account = setup()
    data = csv_text("2026-03-01,A,-10.00")
    commit(service, preview(service, account, data))
    tx = stored(service, account)[0]
    assert service.delete_transaction(OWNER, tx.id)
    assert commit(service, preview(service, account, data)).inserted == 1


# ============================================================= bounds and untrusted content


def test_file_size_bound():
    service, account = setup()
    head = csv_text("2026-03-01,Tea,-1.00")
    big = head + "x" * (importing.MAX_IMPORT_BYTES + 1 - len(head))  # one byte over; still within the base64 cap
    with pytest.raises(ImportRefused) as refused:
        preview(service, account, big)
    assert refused.value.status_code == 413
    with pytest.raises(ImportRefused) as raw_refused:
        importing.read_table(b"x" * (importing.MAX_IMPORT_BYTES + 1))
    assert raw_refused.value.status_code == 413
    with pytest.raises(ValidationError):  # the request model caps the base64 text as well
        preview(service, account, "x" * (importing.MAX_IMPORT_BYTES + 3))


def test_row_bound():
    service, account = setup()
    rows = ["2026-03-01,T,-1"] * (importing.MAX_IMPORT_ROWS + 1)
    with pytest.raises(ImportRefused) as refused:
        preview(service, account, csv_text(*rows))
    assert "10000 data rows" in refused.value.message
    accepted = preview(service, account, csv_text(*rows[:importing.MAX_IMPORT_ROWS]))
    assert accepted.counts.rows == importing.MAX_IMPORT_ROWS


def test_column_bound():
    service, account = setup()
    header = ",".join(["Date", "Description", "Amount"] + [f"c{i}" for i in range(48)])
    with pytest.raises(ImportRefused) as refused:
        preview(service, account, header + "\n" + ",".join(["2026-03-01", "T", "-1"] + ["x"] * 48) + "\n")
    assert "more than 50 columns" in refused.value.message


def test_cells_over_500_characters_make_the_row_an_error():
    service, account = setup()
    result = preview(service, account, csv_text("2026-03-01," + "D" * 501 + ",-1.00", "2026-03-02," + "D" * 500 + ",-1"))
    assert result.rows[0].status == "error" and result.rows[0].description is None
    assert "a mapped cell exceeds 500 characters" in result.rows[0].messages
    assert result.rows[1].status == "ok" and len(result.rows[1].description) == 500


@pytest.mark.parametrize("data,fragment", [
    (b"   \n", "empty"), (b"Date,Description,Amount\n", "no data rows"),
    (b"Date,Description,Amount\n2026-03-01,\x00,1\n", "NUL"),
    (b'Date,Description,Amount\n2026-03-01,"unclosed,1\n' + b"x" * 140_000, "not valid CSV"),
], ids=["blank", "header-only", "nul", "oversized-cell"])
def test_malformed_files_are_refused(data, fragment):
    service, account = setup()
    with pytest.raises(ImportRefused) as refused:
        preview(service, account, data)
    assert fragment in refused.value.message


def test_invalid_base64_is_refused():
    service, account = setup()
    request = ImportPreviewRequest(account_id=account.id, filename="a.csv", data_base64="not base64!",
                                   mapping=make_mapping())
    with pytest.raises(ImportRefused):
        service.preview_import(OWNER, request)


def test_formula_prefixed_descriptions_are_stored_as_plain_text():
    service, account = setup()
    data = csv_text('2026-03-01,"=HYPERLINK(""http://x"")",-1.00', "2026-03-02,+1 transfer,-2.00",
                    "2026-03-03,-SEPA debit,-3.00", "2026-03-04,@shop,-4.00")
    result = preview(service, account, data)
    assert all(row.status == "ok" for row in result.rows)
    commit(service, result)
    descriptions = {tx.description for tx in stored(service, account)}
    assert descriptions == {'=HYPERLINK("http://x")', "+1 transfer", "-SEPA debit", "@shop"}


def test_control_characters_and_line_breaks_are_cleaned_from_descriptions():
    service, account = setup()
    result = preview(service, account, 'Date,Description,Amount\n2026-03-01,"Two\nlines\x07",-1.00\n')
    assert result.rows[0].description == "Two lines"


def test_audit_records_counts_only():
    audit = RecordingAudit()
    service, account = setup(audit=audit)
    done = commit(service, preview(service, account, csv_text("2026-03-01,SECRET-DESC,-1.00", "x,SECRET2,1"),
                                   filename="SECRET-FILE.csv"))
    service.undo_import(OWNER, done.batch_id)
    events = [e for e in audit.events if e["event_type"].startswith("finance.import")]
    assert [e["event_type"] for e in events] == ["finance.import.commit", "finance.import.undo"]
    assert events[0]["details"]["inserted"] == 1 and events[0]["details"]["skipped_errors"] == 1
    assert events[1]["details"]["removed"] == 1
    assert "SECRET" not in repr(events)


def test_filenames_are_cleaned():
    assert importing.clean_filename("C:\\Users\\me\\bank\x07.csv") == "bank.csv"
    assert importing.clean_filename("../../etc/passwd") == "passwd"
    assert importing.clean_filename("   ") == "import.csv"


# ============================================================= API


TOKEN = "finance-import-test-token-at-least-32"
OTHER_TOKEN = "finance-import-other-token-at-least-32"


class DenyPolicy(CapabilityPolicy):
    def __init__(self, owner, denied):
        super().__init__(owner)
        self.denied = denied

    def allows(self, owner_id, capability, **kwargs):
        return capability not in self.denied and super().allows(owner_id, capability, **kwargs)


def make_client(owner=OWNER, token=TOKEN, **overrides):
    settings = Settings(_env_file=None, environment="test", log_level="ERROR", local_auth_token=SecretStr(token),
                        owner_id=owner, **overrides)
    app = create_app(settings=settings)
    return app, TestClient(app, headers={"Authorization": f"Bearer {token}"})


def api_preview_body(account_id, data, **mapping):
    values = {"date_column": "Date", "date_format": "YYYY-MM-DD", "description_column": "Description",
              "amount_column": "Amount"}
    values.update(mapping)
    return {"account_id": account_id, "filename": "bank.csv", "data_base64": b64(data), "mapping": values}


def recent(days_ago):
    return (datetime.now(UTC) - timedelta(days=days_ago)).date().isoformat()


def test_api_requires_authentication():
    app, _ = make_client()
    with TestClient(app) as anonymous:
        some_id = str(uuid4())
        assert anonymous.post("/finance/imports/preview", json=api_preview_body(some_id, "x")).status_code == 401
        assert anonymous.post(f"/finance/imports/{some_id}/commit", json={}).status_code == 401
        assert anonymous.get("/finance/imports").status_code == 401
        assert anonymous.delete(f"/finance/imports/{some_id}").status_code == 401


def test_api_capabilities():
    app, client = make_client()
    with client:
        some_id = str(uuid4())
        app.state.authorization_policy = DenyPolicy(OWNER, {Capability.FINANCE_WRITE})
        assert client.post("/finance/imports/preview", json=api_preview_body(some_id, "x")).status_code == 403
        assert client.post(f"/finance/imports/{some_id}/commit", json={}).status_code == 403
        assert client.delete(f"/finance/imports/{some_id}").status_code == 403
        assert client.get("/finance/imports").status_code == 200
        app.state.authorization_policy = DenyPolicy(OWNER, {Capability.FINANCE_READ})
        assert client.get("/finance/imports").status_code == 403


def test_api_full_flow_preview_commit_list_undo():
    app, client = make_client()
    with client:
        account = client.post("/finance/accounts", json={"name": "Checking", "account_type": "checking",
                                                         "institution": "Bank", "balance": 100.0}).json()
        data = csv_text(f"{recent(3)},Grocery,-30.00", f"{recent(2)},Salary,1000.00", "bad,Broken,1")
        previewed = client.post("/finance/imports/preview", json=api_preview_body(account["id"], data))
        assert previewed.status_code == 201
        body = previewed.json()
        assert body["counts"]["valid"] == 2 and body["counts"]["errors"] == 1 and body["delimiter"] == "comma"
        assert client.get(f"/finance/accounts/{account['id']}").json()["balance"] == 100.0  # nothing written yet
        committed = client.post(f"/finance/imports/{body['id']}/commit", json={})
        assert committed.status_code == 200 and committed.json()["inserted"] == 2
        batch_id = committed.json()["batch_id"]
        assert client.post(f"/finance/imports/{body['id']}/commit", json={}).status_code == 404  # consumed
        assert client.get(f"/finance/accounts/{account['id']}").json()["balance"] == 1070.0
        txs = client.get("/finance/transactions", params={"account_id": account["id"]}).json()["items"]
        assert {tx["import_batch_id"] for tx in txs} == {batch_id}
        batches = client.get("/finance/imports").json()
        assert batches["total"] == 1 and batches["items"][0]["rows_inserted"] == 2
        assert batches["items"][0]["filename"] == "bank.csv"
        undone = client.delete(f"/finance/imports/{batch_id}")
        assert undone.status_code == 200 and undone.json()["removed"] == 2
        assert client.delete(f"/finance/imports/{batch_id}").status_code == 404
        assert client.get(f"/finance/accounts/{account['id']}").json()["balance"] == 100.0


def test_api_errors_are_content_free():
    app, client = make_client()
    with client:
        account = client.post("/finance/accounts", json={"name": "C", "account_type": "checking",
                                                         "institution": "B", "balance": 0}).json()
        missing = client.post("/finance/imports/preview",
                              json=api_preview_body(account["id"], "SECRETHEADER\n", amount_column="Amount"))
        assert missing.status_code == 422 and "SECRET" not in missing.text
        bad_mapping = api_preview_body(account["id"], "a")
        bad_mapping["mapping"]["debit_column"] = "Debit"
        assert client.post("/finance/imports/preview", json=bad_mapping).status_code == 422
        unknown = client.post("/finance/imports/preview", json=api_preview_body(str(uuid4()), "a"))
        assert unknown.status_code == 404
        assert client.post(f"/finance/imports/{uuid4()}/commit", json={}).status_code == 404
        extra = client.post(f"/finance/imports/{uuid4()}/commit", json={"unexpected": 1})
        assert extra.status_code == 422


def test_api_accepts_an_upload_larger_than_the_general_body_limit_only_on_preview():
    app, client = make_client()
    with client:
        account = client.post("/finance/accounts", json={"name": "C", "account_type": "checking",
                                                         "institution": "B", "balance": 0}).json()
        rows = [f"{recent(1 + i % 300)},Purchase number {i} at a shop,-{1 + i % 50}.25" for i in range(3000)]
        data = csv_text(*rows)
        assert len(b64(data)) > 65536  # larger than the default request body limit
        response = client.post("/finance/imports/preview", json=api_preview_body(account["id"], data))
        assert response.status_code == 201 and response.json()["counts"]["rows"] == 3000
        too_big = client.post("/finance/accounts", json={"name": "x" * 70000, "account_type": "checking",
                                                         "institution": "B"})
        assert too_big.status_code == 413
        oversized = {"account_id": account["id"], "filename": "a.csv", "data_base64": "A" * 3_000_000,
                     "mapping": api_preview_body(account["id"], "a")["mapping"]}
        assert client.post("/finance/imports/preview", json=oversized).status_code == 413


def test_api_owner_isolation_for_previews_and_batches():
    app_a, client_a = make_client()
    app_b, client_b = make_client(owner=OTHER, token=OTHER_TOKEN)
    with client_a, client_b:
        app_b.state.finance_service = app_a.state.finance_service  # one shared store, two owners
        account = client_a.post("/finance/accounts", json={"name": "C", "account_type": "checking",
                                                           "institution": "B", "balance": 10.0}).json()
        data = csv_text(f"{recent(1)},Tea,-1.00")
        assert client_b.post("/finance/imports/preview", json=api_preview_body(account["id"], data)).status_code == 404
        preview_id = client_a.post("/finance/imports/preview", json=api_preview_body(account["id"], data)).json()["id"]
        assert client_b.post(f"/finance/imports/{preview_id}/commit", json={}).status_code == 404
        committed = client_a.post(f"/finance/imports/{preview_id}/commit", json={})
        assert committed.status_code == 200 and committed.json()["inserted"] == 1
        batch_id = committed.json()["batch_id"]
        assert client_b.get("/finance/imports").json()["total"] == 0
        assert client_b.delete(f"/finance/imports/{batch_id}").status_code == 404
        assert client_a.get("/finance/imports").json()["total"] == 1
        assert client_a.get(f"/finance/accounts/{account['id']}").json()["balance"] == 9.0
