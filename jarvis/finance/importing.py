"""Bank CSV import with data-quality checks and duplicate detection (v7.1).

Strict safety invariant: no network, broker, exchange or payment API. A CSV file supplied by the owner is parsed
locally with the standard library ``csv`` module and nothing is written until the owner confirms a preview.

Untrusted content
-----------------
The file is untrusted. Its cells are never logged, never put into audit events and never quoted in error
messages: every message names the problem, the column role and the row number only. Parsed values are returned
to the owner in the preview (that is the point of a preview) and stored as plain text.

Descriptions that start with ``=``, ``+``, ``-`` or ``@`` (spreadsheet formula prefixes) are kept exactly as
text. JARVIS never evaluates them; they are only data. Rows are not refused for it, because bank descriptions
legitimately start with these characters (for example ``-SEPA transfer`` or ``@Shop``). Anything that later
exports transactions to a spreadsheet must neutralize such values at export time.

Dates and times
---------------
Allowlisted formats: ``YYYY-MM-DD``, ``DD/MM/YYYY``, ``MM/DD/YYYY``, ``DD-MM-YYYY``, ``YYYY/MM/DD``,
``DD.MM.YYYY``, ``DD/MM/YY``, ``DD-Mon-YYYY``, ``DD Mon YYYY`` and ``DD-Mon-YY`` (English month abbreviations,
any case; "Sept" is accepted). Two-digit years use a fixed pivot: 00-69 are 2000-2069 and 70-99 are 1970-1999.
An optional time may follow (``HH:MM`` or ``HH:MM:SS``, optional fraction, optional ``AM``/``PM``, optional
``Z`` or an explicit offset such as ``+05:30``/``+0530``). A time with an offset is converted to UTC; a time
without one is read in the mapping's ``utc_offset_minutes`` (default 0, i.e. UTC), so an IST export uses 330.
Date-only values are stored at 12:00 UTC so the calendar day is stable.

``DD/MM/YYYY`` and ``MM/DD/YYYY`` are easy to swap. If any row fails the chosen one but parses under the other,
the file is refused (the format looks swapped). If every row has day and month both <= 12 (and at least one
differs), the preview carries a warning that the format is ambiguous and must be confirmed.

Amounts
-------
Dot or comma decimals; thousands separators must be grouped consistently (``,`` `` `` or ``'`` for dot decimals,
``.`` `` `` or ``'`` for comma decimals), and dot-decimal files also accept Indian lakh grouping
(``1,23,456.78``). Currency symbols are stripped only at the edges of the cell, as are three-letter codes and a
leading ``Rs``/``Rs.``/``INR``. Negatives: a leading or trailing minus, parentheses, or a ``DR`` suffix; ``CR``
marks a credit. In separate debit/credit columns the column gives the sign, so a negative value or a
contradicting ``DR``/``CR`` marker in either column is a row error. A zero amount in a single amount column is a
warning (imported only with ``include_warnings``).

Bounds
------
At most ``MAX_IMPORT_BYTES`` (2 MB) per file, ``MAX_IMPORT_ROWS`` (10,000) data rows, ``MAX_IMPORT_COLUMNS`` (50)
columns, and ``MAX_CELL_CHARS`` (500) characters in any mapped cell (a longer mapped cell makes that row an
error; unmapped cells are ignored). Base64 text longer than a 2 MB file can produce is refused as "too large"
(413) before decoding. Previews live in memory only (see ``ImportPreviewCache``).

Duplicates
----------
The duplicate key is (account, day, amount in cents, normalized description), where the description is
casefolded with whitespace collapsed (digits are kept). The ``n``-th occurrence of a key within a file gets the
fingerprint ``sha256(owner, account, key, n)``, stored on the transaction under a unique index, so importing the
same file again adds nothing even when it legitimately contains two identical rows (two coffees on one day).

A row is a duplicate of stored data when the account already has at least ``n`` transactions with its key
(manual or imported); those are always skipped. A later occurrence whose earlier occurrences are not all stored
is a repeat within the file: it is kept by default and shown as a warning, because banks rarely repeat a row by
mistake (``skip_duplicates=true`` skips repeats). So when file A had one coffee and file B (overlapping) has two,
the first is recognized as stored and the second is imported.
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
MAX_ABS_AMOUNT = Decimal("9999999999999.99")  # NUMERIC(15, 2): the range of amounts and of account balances
# The JSON body that carries a 2 MB file as base64 (4/3 larger) plus the mapping and field names.
MAX_BASE64_CHARS = 4 * -(-MAX_IMPORT_BYTES // 3)
IMPORT_REQUEST_BODY_BYTES = MAX_BASE64_CHARS + 16 * 1024

DATE_FORMATS = ("YYYY-MM-DD", "DD/MM/YYYY", "MM/DD/YYYY", "DD-MM-YYYY", "YYYY/MM/DD", "DD.MM.YYYY", "DD/MM/YY",
                "DD-Mon-YYYY", "DD Mon YYYY", "DD-Mon-YY")
SWAPPED_FORMATS = {"DD/MM/YYYY": "MM/DD/YYYY", "MM/DD/YYYY": "DD/MM/YYYY"}
DELIMITERS = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
          "oct": 10, "nov": 11, "dec": 12}

_TIME = (r"(?:(?:[ T]+)(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?(?:\.\d{1,6})?"
         r"(?:\s*(?P<ampm>[AaPp][Mm]))?(?:\s*(?P<tz>Z|[+-]\d{2}:?\d{2}))?)?")
_MON = r"(?P<mon>[A-Za-z]{3,4})"
_DATE_PATTERNS = {
    "YYYY-MM-DD": r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})",
    "DD/MM/YYYY": r"(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<y>\d{4})",
    "MM/DD/YYYY": r"(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4})",
    "DD-MM-YYYY": r"(?P<d>\d{1,2})-(?P<m>\d{1,2})-(?P<y>\d{4})",
    "YYYY/MM/DD": r"(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})",
    "DD.MM.YYYY": r"(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})",
    "DD/MM/YY": r"(?P<d>\d{1,2})/(?P<m>\d{1,2})/(?P<yy>\d{2})",
    "DD-Mon-YYYY": rf"(?P<d>\d{{1,2}})-{_MON}-(?P<y>\d{{4}})",
    "DD Mon YYYY": rf"(?P<d>\d{{1,2}}) +{_MON} +(?P<y>\d{{4}})",
    "DD-Mon-YY": rf"(?P<d>\d{{1,2}})-{_MON}-(?P<yy>\d{{2}})",
}
_DATE_RE = {name: re.compile(rf"^{pattern}{_TIME}$") for name, pattern in _DATE_PATTERNS.items()}

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RE = re.compile(r"\s+")
_SUFFIX_RE = re.compile(r"(?i)\s*(cr|dr)\.?$")
_SYMBOLS = "$\u20ac\u00a3\u00a5\u20b9\u20a9\u20bd\u00a2\u20ba\u20aa\u20b1\u20ab"
_EDGE_CURRENCY_RE = re.compile(rf"^(?:[{_SYMBOLS}]+|(?i:rs\.?|inr)(?![a-z])|[A-Z]{{3}}(?![A-Za-z]))\s*"
                               rf"|\s*(?:[{_SYMBOLS}]+|[A-Z]{{3}})$")
_THOUSANDS_DOT = r"\d{1,3}(?P<sep>[, '])\d{3}(?:(?P=sep)\d{3})*"
_LAKH = r"\d{1,2}(?:,\d{2})+,\d{3}"  # 1,23,456 and 12,34,567 (Indian grouping, commas only)
_DOT_DECIMAL_RE = re.compile(rf"^(?:\d+|{_THOUSANDS_DOT}|{_LAKH})?(?:\.\d+)?$")
_COMMA_DECIMAL_RE = re.compile(r"^(?:\d+|\d{1,3}(?P<sep>[. '])\d{3}(?:(?P=sep)\d{3})*)?(?:,\d+)?$")
_CENT = Decimal("0.01")


class ImportRefused(Exception):
    """The whole file is refused. The message never contains file contents."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class ImportPreviewNotFound(LookupError):
    """Unknown, expired, already committed, or another owner's preview (indistinguishable on purpose)."""


class ImportAccountNotFound(LookupError):
    """The account does not exist or belongs to another owner."""


class ImportBalanceOutOfRange(Exception):
    """The resulting account balance would not fit NUMERIC(15, 2); nothing was written."""


class AmountError(ValueError):
    pass


# ============================================================= value parsing


def clean_text(value: str) -> str:
    """Control characters removed and line breaks folded to spaces; never evaluated as a formula."""
    return _CONTROL_RE.sub("", value.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")).strip()


def normalize_description(value: str) -> str:
    return _WHITESPACE_RE.sub(" ", value.casefold()).strip()


def _year(match: re.Match[str]) -> int:
    if match.groupdict().get("yy") is not None:
        short = int(match.group("yy"))
        return 2000 + short if short <= 69 else 1900 + short  # fixed, documented pivot
    return int(match.group("y"))


def _month(match: re.Match[str]) -> int | None:
    if match.groupdict().get("mon") is not None:
        return MONTHS.get(match.group("mon").casefold())
    return int(match.group("m"))


def _offset(tz: str | None, default_minutes: int) -> timezone:
    if tz is None:
        return timezone(timedelta(minutes=default_minutes))
    if tz == "Z":
        return timezone.utc
    digits = tz[1:].replace(":", "")
    minutes = int(digits[:2]) * 60 + int(digits[2:])
    if minutes > 14 * 60 or int(digits[2:]) >= 60:
        raise ValueError("offset out of range")
    return timezone(timedelta(minutes=minutes if tz[0] == "+" else -minutes))


def parse_date(text: str, date_format: str, utc_offset_minutes: int = 0) -> datetime | None:
    """Parse with one allowlisted format into an aware UTC datetime (see the module docstring)."""
    match = _DATE_RE[date_format].fullmatch(text.strip())
    if match is None:
        return None
    month = _month(match)
    if month is None:
        return None
    try:
        year, day = _year(match), int(match.group("d"))
        if match.group("h") is None:
            return datetime(year, month, day, 12, tzinfo=timezone.utc)
        hour = int(match.group("h"))
        if match.group("ampm") is not None:
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if match.group("ampm").lower() == "pm" else 0)
        local = datetime(year, month, day, hour, int(match.group("mi")), int(match.group("s") or 0),
                         tzinfo=_offset(match.group("tz"), utc_offset_minutes))
        return local.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class ParsedAmount:
    value: Decimal  # signed
    sign_marker: bool  # a minus or parentheses
    suffix: str | None  # "cr" | "dr" | None


def parse_amount_marked(text: str, decimal_separator: str = ".") -> ParsedAmount | None:
    """Parse a money cell keeping its markers. None for an empty cell; AmountError (content-free) otherwise."""
    s = text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u2009", " ").replace("\u2212", "-").strip()
    if not s:
        return None
    negatives = 0
    positive_marker = False
    suffix_marker: str | None = None
    suffix = _SUFFIX_RE.search(s)
    if suffix is not None and suffix.start() > 0 and (s[suffix.start() - 1].isdigit() or s[suffix.start()] == " "
                                                     or s[suffix.start() - 1] in ")" + _SYMBOLS):
        suffix_marker = suffix.group(1).lower()
        s = s[: suffix.start()].strip()
    for _ in range(2):  # symbols and signs may wrap each other: "-$5", "$-5", "($5)", "$(5)", "Rs. -5"
        s = _EDGE_CURRENCY_RE.sub("", s).strip()
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
    sign_marker = negatives > 0
    if negatives > 1 or (negatives and (positive_marker or suffix_marker == "cr")):
        raise AmountError("amount has conflicting signs")
    if positive_marker and suffix_marker == "dr":
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
    negative = sign_marker or suffix_marker == "dr"
    return ParsedAmount(-value if negative else value, sign_marker, suffix_marker)


def parse_amount(text: str, decimal_separator: str = ".") -> Decimal | None:
    """Signed value of a money cell (see ``parse_amount_marked``)."""
    parsed = parse_amount_marked(text, decimal_separator)
    return None if parsed is None else parsed.value


# ============================================================= file decoding and table reading


@dataclass(frozen=True)
class DecodedTable:
    encoding: str
    delimiter: str
    header: list[str]
    rows: list[tuple[int, int, list[str]]]  # (data row number, file line number, cells); blank rows left out
    blank_rows: int
    warnings: list[str]


def too_large() -> ImportRefused:
    return ImportRefused(f"The file exceeds the maximum size of {MAX_IMPORT_BYTES // (1024 * 1024)} MB.", 413)


def decode_base64(data: str) -> bytes:
    if len(data) > MAX_BASE64_CHARS:
        raise too_large()
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
        raise too_large()
    if not raw.strip():
        raise ImportRefused("The file is empty.")
    text, encoding, warnings = decode_bytes(raw)
    if "\x00" in text:
        raise ImportRefused("The file contains NUL bytes; it does not look like a CSV text file.")
    chosen = delimiter or sniff_delimiter(text)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=chosen)
    header: list[str] | None = None
    rows: list[tuple[int, int, list[str]]] = []
    blank = 0
    try:
        start_line = 1
        for cells in reader:
            line_number, start_line = start_line, reader.line_num + 1
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
            rows.append((len(rows) + 1, line_number, cells))
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


def check_day_month_order(table: DecodedTable, date_index: int, date_format: str) -> list[str]:
    """Refuse a DD/MM vs MM/DD swap that some row proves; warn when no row can tell the two apart."""
    swapped = SWAPPED_FORMATS.get(date_format)
    if swapped is None:
        return []
    any_valid = distinguishing = differing = False
    for _, _, cells in table.rows:
        text = cells[date_index].strip() if date_index < len(cells) else ""
        if len(text) > MAX_CELL_CHARS:
            continue
        if parse_date(text, date_format) is None:
            if parse_date(text, swapped) is not None:
                raise ImportRefused(f"The date format looks swapped (DD/MM vs MM/DD): some dates are not valid "
                                    f"{date_format} dates but are valid {swapped} dates. Check the date format.")
            continue
        match = _DATE_RE[date_format].fullmatch(text)
        day, month = int(match.group("d")), int(match.group("m"))
        any_valid = True
        distinguishing = distinguishing or day > 12 or month > 12
        differing = differing or day != month
    if any_valid and not distinguishing and differing:
        return [f"Every date in the file is valid as both DD/MM and MM/DD, so the format is ambiguous; confirm "
                f"that {date_format} is right before committing."]
    return []


# ============================================================= rows and checks


@dataclass
class ParsedRow:
    row_number: int
    line_number: int = 0
    transaction_date: datetime | None = None
    amount: Decimal | None = None
    description: str | None = None
    category: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    key: str | None = None
    occurrence: int = 0
    previous_row: int | None = None
    fingerprint: str | None = None
    duplicate: str | None = None  # "existing" (always skipped) | "file" (a repeat: kept by default, a warning)

    @property
    def status(self) -> str:
        if self.errors:
            return "error"
        if self.duplicate == "existing":
            return "duplicate"
        if self.warnings or self.duplicate == "file":
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
              today: date, line_number: int = 0) -> ParsedRow:
    row = ParsedRow(row_number, line_number)
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
    parsed_date = parse_date(raw_date or "", mapping.date_format, mapping.utc_offset_minutes)
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


def _split_value(row: ParsedRow, parsed: ParsedAmount | None, role: str) -> Decimal | None:
    """A debit or credit column gives the sign itself: any minus, or a marker that contradicts it, is an error."""
    if parsed is None:
        return None
    if parsed.sign_marker:
        row.errors.append(f"the {role} column has a negative amount")
        return None
    expected = "dr" if role == "debit" else "cr"
    if parsed.suffix is not None and parsed.suffix != expected:
        row.errors.append(f"the {role} column has a {parsed.suffix.upper()} marker that contradicts the column")
        return None
    return abs(parsed.value)


def _row_amount(row: ParsedRow, cells: list[str], columns: dict[str, int], mapping: Any) -> Decimal | None:
    try:
        if "amount_column" in columns:
            value = parse_amount(_cell(cells, columns["amount_column"])[0] or "", mapping.decimal_separator)
            if value is None:
                row.errors.append("amount is empty")
                return None
            if value == 0:
                row.warnings.append("amount is zero")
            return _round_cents(row, value)
        debit_parsed = parse_amount_marked(_cell(cells, columns["debit_column"])[0] or "", mapping.decimal_separator)
        credit_parsed = parse_amount_marked(_cell(cells, columns["credit_column"])[0] or "",
                                            mapping.decimal_separator)
    except AmountError as exc:
        row.errors.append(str(exc))
        return None
    debit = _split_value(row, debit_parsed, "debit")
    credit = _split_value(row, credit_parsed, "credit")
    if row.errors:
        return None
    # A zero in the unused column (for example "0.00") counts as empty.
    has_debit = debit is not None and debit != 0
    has_credit = credit is not None and credit != 0
    if has_debit and has_credit:
        row.errors.append("both debit and credit are filled")
        return None
    if has_debit:
        return _round_cents(row, -debit)
    if has_credit:
        return _round_cents(row, credit)
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
    last_seen: dict[str, int] = {}
    counts: dict[str, int] = {}
    for row in rows:
        if row.errors or row.transaction_date is None or row.amount is None or row.description is None:
            continue
        key = duplicate_key(account_id, row.transaction_date.date(), row.amount, row.description)
        counts[key] = counts.get(key, 0) + 1
        row.key, row.occurrence = key, counts[key]
        row.previous_row = last_seen.get(key)
        last_seen[key] = row.row_number
        row.fingerprint = fingerprint(owner_id, key, row.occurrence)


def mark_duplicates(rows: list[ParsedRow], existing_counts: dict[str, int]) -> None:
    """Existing: the account already holds >= n rows with this key. A repeat: an earlier occurrence in this file
    is not stored yet (if every earlier occurrence is stored, this one is simply new)."""
    for row in rows:
        row.duplicate = None
        if row.key is None:
            continue
        stored = existing_counts.get(row.key, 0)
        if row.occurrence <= stored:
            row.duplicate = "existing"
        elif row.occurrence - 1 > stored:
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
        messages.append(f"same date, amount and description as row {row.previous_row} - kept; banks rarely "
                        f"repeat rows by mistake")
    return messages + list(row.warnings)


def clean_filename(filename: str) -> str:
    cleaned = _CONTROL_RE.sub("", filename).replace("\n", " ").replace("\r", " ").strip()
    cleaned = cleaned.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return (cleaned or "import.csv")[:MAX_FILENAME_CHARS]


def check_balance(balance: Any, change: Any) -> None:
    """Refuse a write whose resulting balance would not fit NUMERIC(15, 2)."""
    if abs(Decimal(str(balance)) + Decimal(str(change))) > MAX_ABS_AMOUNT:
        raise ImportBalanceOutOfRange("The resulting account balance would be out of range.")


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
    """In-memory previews with a TTL and per-owner and global caps.

    Previews are transient and hold parsed untrusted file content; keeping them out of PostgreSQL means the file
    content is never persisted unless the owner commits it, and a restart simply asks the owner to upload again.
    Measured memory: about 10.6 MB for a maximal preview (10,000 rows from a 2 MB file), so about 32 MB per owner
    at the per-owner cap of 3 and about 106 MB at the global cap of 10.

    Eviction: an owner's oldest preview makes room for their new one at the per-owner cap. At the global cap the
    oldest preview of the same owner goes first where they have one, else the oldest preview overall.
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
            while len(self._entries) >= self.total:
                victim = mine.pop(0) if mine else min(self._entries.values(), key=lambda e: e.created_monotonic)
                del self._entries[victim.id]
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
