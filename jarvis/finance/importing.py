"""Bank CSV import with data-quality checks and duplicate detection (v7.1).

Strict safety invariant: no network, broker, exchange or payment API. A CSV file supplied by the owner is parsed
locally with the standard library ``csv`` module and nothing is written until the owner confirms a preview.

Untrusted content
-----------------
The file is untrusted. Its cells are never logged, never put into audit events and never quoted in error
messages: every message names the problem and the row number only. Parsed values are returned to the owner in
the preview (that is the point of a preview) and stored as plain text.

Descriptions that start with ``=``, ``+``, ``-`` or ``@`` (spreadsheet formula prefixes) are kept exactly as
text. JARVIS never evaluates them; they are only data. Rows are not refused for it, because bank descriptions
legitimately start with these characters (for example ``-SEPA transfer`` or ``@Shop``). Anything that later
exports transactions to a spreadsheet must neutralize such values at export time.

Bounds
------
At most ``MAX_IMPORT_BYTES`` (2 MB) per file, ``MAX_IMPORT_ROWS`` (10,000) data rows, ``MAX_IMPORT_COLUMNS`` (50)
columns, and ``MAX_CELL_CHARS`` (500) characters in any mapped cell (a longer mapped cell makes that row an
error; unmapped cells are ignored). Previews live in memory only (see ``ImportPreviewCache``).

Duplicates
----------
The duplicate key is (account, day, amount in cents, normalized description), where the description is
casefolded with whitespace collapsed (digits are kept). The ``n``-th occurrence of a key within a file gets the
fingerprint ``sha256(owner, account, key, n)``, stored on the transaction under a unique index, so importing the
same file again adds nothing even when it legitimately contains two identical rows (two coffees on one day).
A row is a duplicate of stored data when the account already has at least ``n`` transactions with its key
(manual or imported); otherwise a repeat of an earlier row in the same file is an in-file duplicate.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import re
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Callable
from uuid import UUID

MAX_IMPORT_BYTES = 2 * 1024 * 1024
MAX_IMPORT_ROWS = 10_000
MAX_IMPORT_COLUMNS = 50
MAX_CELL_CHARS = 500
MAX_CATEGORY_CHARS = 100
MAX_FILENAME_CHARS = 255
PREVIEW_ROWS = 50
PREVIEW_TTL_SECONDS = 30 * 60
MAX_PREVIEWS_PER_OWNER = 3
MAX_PREVIEWS_TOTAL = 10
MAX_EXISTING_SCAN = 50_000
MAX_AGE_YEARS = 20
OUTLIER_FACTOR = Decimal(10)
OUTLIER_FLOOR = Decimal(1000)
MAX_ABS_AMOUNT = Decimal("9999999999999.99")  # NUMERIC(15, 2)
# The JSON body that carries a 2 MB file as base64 (4/3 larger) plus the mapping and field names.
MAX_BASE64_CHARS = 4 * -(-MAX_IMPORT_BYTES // 3)
IMPORT_REQUEST_BODY_BYTES = MAX_BASE64_CHARS + 16 * 1024

DATE_FORMATS = ("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "YYYY/MM/DD", "DD.MM.YYYY")
DELIMITERS = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}

_TIME = r"(?:(?:[ T]+)(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?(?:\.\d{1,6})?Z?)?"
_DATE_PATTERNS = {
    "YYYY-MM-DD": r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})",
    "DD/MM/YYYY": r"(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})",
    "MM/DD/YYYY": r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})",
    "DD-MM-YYYY": r"(?P<d>\d{1,2})-(?P<m>\d{1,2})-(?P<y>\d{4})",
    "YYYY/MM/DD": r"(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})",
    "DD.MM.YYYY": r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})",
}
_DATE_RE = {name: re.compile(rf"^{pattern}{_TIME}$") for name, pattern in _DATE_PATTERNS.items()}

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_SUFFIX_RE = re.compile(r"(?i)\s*(cr|dr)\.?$")
_CURRENCY_SYMBOLS_RE = re.compile(r"[$\u20ac\u00a3\u00a5\u20b9\u20a9\u20bd\u00a2\u20ba\u20aa\u20b1\u20ab]")
_CURRENCY_CODE_RE = re.compile(r"^[A-Z]{3}\s*|\s*[A-Z]{3}$")
_DOT_DECIMAL_RE = re.compile(r"^(?:\d+|\d{1,3}(?P<sep>[, '])\d{3}(?:(?P=sep)\d{3})*)?(?:\.\d+)?$")
_COMMA_DECIMAL_RE = re.compile(r"^(?:\d+|\d{1,3}(?P<sep>[. '])\d{3}(?:(?P=sep)\d{3})*)?(?:,\d+)?$")
_CENT = Decimal("0.01")


class ImportRefused(Exception):
    """The whole file is refused. The message never contains file contents."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class AmountError(ValueError):
    pass


# ============================================================= value parsing


def clean_text(value: str) -> str:
    """Control characters removed and line breaks folded to spaces; never evaluated as a formula."""
    return _CONTROL_RE.sub("", value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")).strip()


def normalize_description(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.casefold()).strip()


def parse_date(text: str, date_format: str) -> datetime | None:
    """Parse with one allowlisted format; date-only values are stored at 12:00 UTC so the day is stable."""
    match = _DATE_RE[date_format].fullmatch(text.strip())
    if match is None:
        return None
    try:
        if match.group("h") is None:
            return datetime(int(match.group("y")), int(match.group("m")), int(match.group("d")), 12,
                            tzinfo=timezone.utc)
        return datetime(int(match.group("y")), int(match.group("m")), int(match.group("d")),
                        int(match.group("h")), int(match.group("mi")), int(match.group("s") or 0),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_amount(text: str, decimal_separator: str = ".") -> Decimal | None:
    """Parse a money cell. Returns None for an empty cell; raises AmountError (content-free message) otherwise.

    Handles currency symbols and three-letter codes, a leading or trailing minus, a leading plus, parentheses
    for negatives, ``CR`` (credit, positive) and ``DR`` (debit, negative) suffixes, and thousands separators
    that must be grouped consistently (so a comma decimal in a dot-decimal file is an error, not a silent x100).
    """
    s = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").replace("\u2212", "-").strip()
    if not s:
        return None
    negatives = 0
    positive_marker = False
    suffix = _SUFFIX_RE.search(s)
    if suffix is not None and suffix.start() > 0:
        if suffix.group(1).lower() == "dr":
            negatives += 1
        else:
            positive_marker = True
        s = s[: suffix.start()].strip()
    for _ in range(2):  # symbols and signs may wrap each other: "-$5", "$-5", "($5)", "$(5)"
        s = _CURRENCY_SYMBOLS_RE.sub("", s).strip()
        s = _CURRENCY_CODE_RE.sub("", s).strip()
        if s.startswith("(") and s.endswith(")"):
            negatives += 1
            s = s[1:-1].strip()
        if s.startswith("-"):
            negatives += 1
            s = s[1:].strip()
        elif s.startswith("+"):
            positive_marker = True
            s = s[1:].strip()
        elif s.endswith("-"):
            negatives += 1
            s = s[:-1].strip()
    if negatives > 1 or (negatives and positive_marker):
        raise AmountError("amount has conflicting signs")
    pattern = _DOT_DECIMAL_RE if decimal_separator == "." else _COMMA_DECIMAL_RE
    if not s or not any(ch.isdigit() for ch in s) or pattern.fullmatch(s) is None:
        raise AmountError("amount is not a number in the selected decimal style")
    group = "," if decimal_separator == "." else "."
    digits = s.replace(" ", "").replace("'", "").replace(group, "")
    if decimal_separator == ",":
        digits = digits.replace(",", ".")
    try:
        value = Decimal(digits)
    except InvalidOperation as exc:
        raise AmountError("amount is not a number in the selected decimal style") from exc
    if not value.is_finite():
        raise AmountError("amount is not finite")
    return -value if negatives else value


# ============================================================= file decoding and table reading


@dataclass(frozen=True)
class DecodedTable:
    encoding: str
    delimiter: str
    header: list[str]
    rows: list[tuple[int, list[str]]]  # (data row number, cells); blank rows are left out
    blank_rows: int
    warnings: list[str]


def decode_base64(data: str) -> bytes:
    if len(data) > MAX_BASE64_CHARS:
        raise ImportRefused(f"The file exceeds the maximum size of {MAX_IMPORT_BYTES // (1024 * 1024)} MB.", 413)
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImportRefused("data_base64 is not valid base64.") from exc
    return raw


def decode_bytes(raw: bytes) -> tuple[str, str, list[str]]:
    """UTF-8 (BOM removed), UTF-16 with a BOM, else Windows-1252 with a warning."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw, label = raw[3:], "utf-8-sig"
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16"), "utf-16", []
        except UnicodeDecodeError as exc:
            raise ImportRefused("The file has a UTF-16 byte order mark but is not valid UTF-16.") from exc
    else:
        label = "utf-8"
    try:
        return raw.decode("utf-8"), label, []
    except UnicodeDecodeError:
        pass
    try:
        text = raw.decode("cp1252")
    except UnicodeDecodeError as exc:
        raise ImportRefused("The file is neither UTF-8 nor Windows-1252 text.") from exc
    return text, "cp1252", ["The file is not UTF-8; it was read as Windows-1252. Check accented characters."]


def sniff_delimiter(text: str) -> str:
    """Choose among , ; tab | the delimiter that splits the first lines into the most consistent column count."""
    sample_lines = [line for line in text[:64 * 1024].splitlines() if line.strip()][:30]
    if not sample_lines:
        raise ImportRefused("The file has no data rows.")
    best: tuple[float, int, int] | None = None
    chosen = ","
    for priority, delimiter in enumerate(DELIMITERS):
        try:
            counts = [len(row) for row in csv.reader(sample_lines, delimiter=delimiter)]
        except csv.Error:
            continue
        header_count = counts[0]
        if header_count < 2:
            continue
        consistency = sum(1 for count in counts if count == header_count) / len(counts)
        score = (consistency, header_count, -priority)
        if best is None or score > best:
            best, chosen = score, delimiter
    return chosen


def read_table(raw: bytes, delimiter: str | None = None) -> DecodedTable:
    if len(raw) > MAX_IMPORT_BYTES:
        raise ImportRefused(f"The file exceeds the maximum size of {MAX_IMPORT_BYTES // (1024 * 1024)} MB.", 413)
    if not raw.strip():
        raise ImportRefused("The file is empty.")
    text, encoding, warnings = decode_bytes(raw)
    if "\x00" in text:
        raise ImportRefused("The file contains NUL bytes; it does not look like a CSV text file.")
    chosen = delimiter or sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=chosen)
    header: list[str] | None = None
    rows: list[tuple[int, list[str]]] = []
    blank = 0
    try:
        for cells in reader:
            if len(cells) > MAX_IMPORT_COLUMNS:
                raise ImportRefused(f"The file has more than {MAX_IMPORT_COLUMNS} columns.")
            if not any(cell.strip() for cell in cells):
                if header is not None:
                    blank += 1
                continue
            if header is None:
                header = [clean_text(cell)[:MAX_CELL_CHARS] for cell in cells]
                continue
            if len(rows) >= MAX_IMPORT_ROWS:
                raise ImportRefused(f"The file has more than {MAX_IMPORT_ROWS} data rows.")
            rows.append((len(rows) + 1, cells))
    except csv.Error as exc:
        raise ImportRefused("The file is not valid CSV (for example a cell is too large or a quote is unclosed).") \
            from exc
    if header is None:
        raise ImportRefused("The file has no header row.")
    if not rows:
        raise ImportRefused("The file has no data rows.")
    return DecodedTable(encoding, chosen, header, rows, blank, warnings)


def resolve_columns(header: list[str], mapping: Any) -> dict[str, int]:
    """Map each named column (case-insensitive, trimmed) to its index; a missing or ambiguous name is refused."""
    positions: dict[str, list[int]] = {}
    for index, name in enumerate(header):
        positions.setdefault(name.strip().casefold(), []).append(index)
    resolved: dict[str, int] = {}
    for role in ("date_column", "description_column", "amount_column", "debit_column", "credit_column",
                 "category_column"):
        name = getattr(mapping, role)
        if name is None:
            continue
        found = positions.get(name.strip().casefold(), [])
        if not found:
            raise ImportRefused(f"The mapped {role.replace('_', ' ')} is not in the file's header row.")
        if len(found) > 1:
            raise ImportRefused(f"The mapped {role.replace('_', ' ')} appears more than once in the header row.")
        resolved[role] = found[0]
    return resolved


# ============================================================= rows and checks


@dataclass
class ParsedRow:
    row_number: int
    transaction_date: datetime | None = None
    amount: Decimal | None = None
    description: str | None = None
    category: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    key: str | None = None
    occurrence: int = 0
    first_row: int | None = None
    fingerprint: str | None = None
    duplicate: str | None = None  # "file" | "existing"

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.duplicate:
            return "duplicate"
        if self.warnings:
            return "warning"
        return "ok"

    @property
    def valid(self) -> bool:
        return self.status in ("ok", "warning")


def _cell(cells: list[str], index: int | None) -> tuple[str | None, bool]:
    """(value, too_long); None when the column is unmapped or the row is short."""
    if index is None or index >= len(cells):
        return None, False
    value = cells[index]
    return value, len(value) > MAX_CELL_CHARS


def parse_row(row_number: int, cells: list[str], columns: dict[str, int], mapping: Any, header_len: int,
              today: date) -> ParsedRow:
    row = ParsedRow(row_number)
    needed = max(columns.values())
    if len(cells) <= needed:
        row.errors.append("row has fewer columns than the mapping needs")
        return row
    if len(cells) != header_len:
        row.warnings.append("row has a different number of columns than the header")
    too_long = [role for role, index in columns.items() if _cell(cells, index)[1]]
    if too_long:
        row.errors.append(f"a mapped cell exceeds {MAX_CELL_CHARS} characters")
        return row

    raw_date, _ = _cell(cells, columns["date_column"])
    parsed_date = parse_date(raw_date or "", mapping.date_format)
    if parsed_date is None:
        row.errors.append(f"date is not a valid {mapping.date_format} date")
    else:
        row.transaction_date = parsed_date
        day = parsed_date.date()
        if day > today + timedelta(days=1):
            row.errors.append("date is in the future (after tomorrow)")
        elif day < _years_before(today, MAX_AGE_YEARS):
            row.errors.append(f"date is more than {MAX_AGE_YEARS} years old")

    description = clean_text(_cell(cells, columns["description_column"])[0] or "")
    if not description:
        row.errors.append("description is empty")
    else:
        row.description = description

    row.amount = _row_amount(row, cells, columns, mapping)
    if row.amount is not None and abs(row.amount) > MAX_ABS_AMOUNT:
        row.errors.append("amount is out of range")
        row.amount = None

    if "category_column" in columns:
        category = clean_text(_cell(cells, columns["category_column"])[0] or "")
        if len(category) > MAX_CATEGORY_CHARS:
            row.warnings.append(f"category is longer than {MAX_CATEGORY_CHARS} characters; it will be assigned")
        elif category:
            row.category = category
    return row


def _round_cents(row: ParsedRow, value: Decimal) -> Decimal:
    rounded = value.quantize(_CENT, rounding=ROUND_HALF_UP)
    if rounded != value:
        row.warnings.append("amount has more than 2 decimal places; it was rounded to cents")
    return rounded


def _row_amount(row: ParsedRow, cells: list[str], columns: dict[str, int], mapping: Any) -> Decimal | None:
    try:
        if "amount_column" in columns:
            value = parse_amount(_cell(cells, columns["amount_column"])[0] or "", mapping.decimal_separator)
            if value is None:
                row.errors.append("amount is empty")
                return None
            return _round_cents(row, value)
        debit = parse_amount(_cell(cells, columns["debit_column"])[0] or "", mapping.decimal_separator)
        credit = parse_amount(_cell(cells, columns["credit_column"])[0] or "", mapping.decimal_separator)
    except AmountError as exc:
        row.errors.append(str(exc))
        return None
    # A zero in the unused column (for example "0.00") counts as empty.
    has_debit = debit is not None and debit != 0
    has_credit = credit is not None and credit != 0
    if has_debit and has_credit:
        row.errors.append("both debit and credit are filled")
        return None
    if has_debit:
        return _round_cents(row, -abs(debit))
    if has_credit:
        return _round_cents(row, abs(credit))
    row.errors.append("neither debit nor credit has an amount")
    return None


def _years_before(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year - years)
    except ValueError:  # 29 February
        return day.replace(year=day.year - years, day=28)


def duplicate_key(account_id: UUID, day: date, amount: Decimal, description: str) -> str:
    cents = int((amount * 100).to_integral_value(rounding=ROUND_HALF_UP))
    return f"{account_id}|{day.isoformat()}|{cents}|{normalize_description(description)}"


def fingerprint(owner_id: UUID, key: str, occurrence: int) -> str:
    return hashlib.sha256(f"jarvis-import-v1|{owner_id}|{key}|{occurrence}".encode("utf-8")).hexdigest()


def flag_outliers(rows: list[ParsedRow]) -> None:
    """|amount| > 10x the file's median absolute amount and > 1000 is a warning, never an error."""
    amounts = [abs(row.amount) for row in rows if row.amount is not None and not row.errors]
    if not amounts:
        return
    median = Decimal(statistics.median(amounts))
    for row in rows:
        if row.amount is not None and not row.errors:
            magnitude = abs(row.amount)
            if magnitude > OUTLIER_FACTOR * median and magnitude > OUTLIER_FLOOR:
                row.warnings.append("amount is unusually large for this file (more than 10x the median)")


def assign_keys(owner_id: UUID, account_id: UUID, rows: list[ParsedRow]) -> None:
    first_seen: dict[str, int] = {}
    counts: dict[str, int] = {}
    for row in rows:
        if row.errors or row.transaction_date is None or row.amount is None or row.description is None:
            continue
        key = duplicate_key(account_id, row.transaction_date.date(), row.amount, row.description)
        counts[key] = counts.get(key, 0) + 1
        row.key, row.occurrence = key, counts[key]
        row.first_row = first_seen.setdefault(key, row.row_number)
        row.fingerprint = fingerprint(owner_id, key, row.occurrence)


def mark_duplicates(rows: list[ParsedRow], existing_counts: dict[str, int]) -> None:
    """Existing duplicates: the account already holds >= n rows with this key. Else a repeat is in-file."""
    for row in rows:
        row.duplicate = None
        if row.key is None:
            continue
        if row.occurrence <= existing_counts.get(row.key, 0):
            row.duplicate = "existing"
        elif row.occurrence > 1:
            row.duplicate = "file"


def existing_key_counts(account_id: UUID, stored: list[tuple[datetime, Any, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for when, amount, description in stored:
        moment = when if when.tzinfo is not None else when.replace(tzinfo=timezone.utc)
        key = duplicate_key(account_id, moment.astimezone(timezone.utc).date(), Decimal(str(amount)), description)
        counts[key] = counts.get(key, 0) + 1
    return counts


def date_window(rows: list[ParsedRow]) -> tuple[datetime, datetime] | None:
    days = [row.transaction_date.date() for row in rows if row.key is not None and row.transaction_date]
    if not days:
        return None
    start = datetime.combine(min(days), datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(max(days) + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    return start, end


def row_messages(row: ParsedRow) -> list[str]:
    messages = list(row.errors)
    if row.duplicate == "existing":
        messages.append("same date, amount and description as a transaction already stored for this account")
    elif row.duplicate == "file":
        messages.append(f"repeats row {row.first_row} of this file (same date, amount and description)")
    return messages + list(row.warnings)


def clean_filename(filename: str) -> str:
    cleaned = _CONTROL_RE.sub("", filename).replace("\n", " ").replace("\r", " ").strip()
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (cleaned or "import.csv")[:MAX_FILENAME_CHARS]


# ============================================================= preview cache


@dataclass
class PreviewEntry:
    id: UUID
    owner_id: UUID
    account_id: UUID
    filename: str
    rows: list[ParsedRow]
    created_at: datetime
    expires_at: datetime
    created_monotonic: float
    meta: dict[str, Any] = field(default_factory=dict)


class ImportPreviewCache:
    """In-memory previews with a TTL and per-owner and global caps (oldest evicted first).

    Previews are transient and hold parsed untrusted file content; keeping them out of PostgreSQL means the file
    content is never persisted unless the owner commits it, and a restart simply asks the owner to upload again.
    Worst case memory is MAX_PREVIEWS_TOTAL previews of at most 10,000 rows from a 2 MB file each.
    """

    def __init__(self, ttl_seconds: float = PREVIEW_TTL_SECONDS, per_owner: int = MAX_PREVIEWS_PER_OWNER,
                 total: int = MAX_PREVIEWS_TOTAL, clock: Callable[[], float] = time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self.per_owner = per_owner
        self.total = total
        self.clock = clock
        self._lock = threading.Lock()
        self._entries: dict[UUID, PreviewEntry] = {}

    def _expired(self, entry: PreviewEntry, now: float) -> bool:
        return now - entry.created_monotonic >= self.ttl_seconds

    def _purge(self, now: float) -> None:
        for key in [key for key, entry in self._entries.items() if self._expired(entry, now)]:
            del self._entries[key]

    def put(self, entry: PreviewEntry) -> None:
        with self._lock:
            now = self.clock()
            self._purge(now)
            mine = sorted((e for e in self._entries.values() if e.owner_id == entry.owner_id),
                          key=lambda e: e.created_monotonic)
            while len(mine) >= self.per_owner:
                del self._entries[mine.pop(0).id]
            everyone = sorted(self._entries.values(), key=lambda e: e.created_monotonic)
            while len(everyone) >= self.total:
                del self._entries[everyone.pop(0).id]
            self._entries[entry.id] = entry

    def take(self, owner_id: UUID, preview_id: UUID) -> PreviewEntry | None:
        """Remove and return the owner's live preview; a second take finds nothing (consumed)."""
        with self._lock:
            now = self.clock()
            self._purge(now)
            entry = self._entries.get(preview_id)
            if entry is None or entry.owner_id != owner_id:
                return None
            del self._entries[preview_id]
            return entry

    def restore(self, entry: PreviewEntry) -> None:
        """Put back a preview whose commit failed before anything was stored (if it has not expired)."""
        with self._lock:
            if not self._expired(entry, self.clock()):
                self._entries.setdefault(entry.id, entry)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class ImportPreviewNotFound(LookupError):
    """Unknown, expired, already committed, or another owner's preview (indistinguishable on purpose)."""


class ImportAccountNotFound(LookupError):
    """The account does not exist or belongs to another owner."""
