"""Service CU monthly member statement (PDF) parser.

A statement covers every share (envelope) the member holds, one section per
share. Sections are keyed by the share-number suffix ("XXXXXX6980") because
the display names are nicknames that change over time (e.g. "Primary
Savings" was renamed "Paycheck Holdings"); continuation headers even use the
product name instead of the nickname.

The extracted text gives, per row: effective date, description, an unsigned
amount, and the running balance. Debit vs credit is inferred from the
balance chain — prev + amount == balance means credit, prev - amount ==
balance means debit — and every row must chain exactly or the section is
rejected. That makes the parser self-verifying: a mis-parsed row cannot
slip through silently.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .money import to_cents

SECTION_RE = re.compile(r"^(.{2,60}?) - (X{4,8})(\d{3,4})\b(.*)$")
ROW_RE = re.compile(
    r"^(\d{2}/\d{2})\s+(?:(\d{2}/\d{2})\s+)?(.*?)\s+"
    r"\$(-?[\d,]+\.\d{2})(?:\s+\$(-?[\d,]+\.\d{2}))?\s*$"
)
PERIOD_RE = re.compile(r"\b(\d{2}/\d{2}/\d{4})\b")

# Share suffix -> Money Hub account name (Service CU, member XXXXX9401).
DEFAULT_SHARE_MAP = {
    "6970": "SCU Paycheck Holdings",
    "6980": "SCU Spending",
    "6130": "SCU Bills",
    "6590": "SCU E-Fund",
    "6690": "SCU PC Savings",
}


class StatementParseError(RuntimeError):
    pass


@dataclass
class StatementRow:
    eff_date: date
    description: str
    amount_cents: int  # signed; negative = outflow
    balance_after_cents: int


@dataclass
class StatementSection:
    suffix: str
    name: str
    beginning_cents: int | None = None
    beginning_date: date | None = None
    ending_cents: int | None = None
    ending_date: date | None = None
    rows: list[StatementRow] = field(default_factory=list)
    _running: int | None = None


@dataclass
class Statement:
    period_ending: date
    sections: dict[str, StatementSection]  # keyed by share suffix


def _parse_period_ending(text: str) -> date:
    m = PERIOD_RE.search(text)
    if not m:
        raise StatementParseError("could not find the statement period-ending date")
    month, day, year = m.group(1).split("/")
    return date(int(year), int(month), int(day))


def _parse_mmdd(mmdd: str, period_ending: date) -> date:
    month, day = (int(p) for p in mmdd.split("/"))
    return date(period_ending.year, month, day)


def parse_statement_pages(pages: list[str]) -> Statement:
    period_ending = _parse_period_ending(pages[0])
    sections: dict[str, StatementSection] = {}
    current: StatementSection | None = None
    pending: str | None = None  # wrapped row fragment awaiting its money columns

    for page_text in pages:
        for raw_line in page_text.splitlines():
            line = raw_line.rstrip()

            header = SECTION_RE.match(line)
            if header is not None:
                name, suffix, tail = header.group(1).strip(), header.group(3), header.group(4)
                section = sections.get(suffix)
                if section is None:
                    section = StatementSection(suffix=suffix, name=name)
                    sections[suffix] = section
                elif "Continued" not in tail and section.name != name:
                    section.name = name  # prefer the nickname over product names
                current = section
                pending = None
                continue

            if current is None:
                continue
            if pending is not None:
                line = f"{pending} {line.strip()}"
                pending = None

            row = ROW_RE.match(line)
            if row is None:
                if re.match(r"^\d{2}/\d{2}\s+\S", line) and "$" not in line:
                    pending = line  # description wrapped onto the next line
                continue

            mmdd, _tran, description, first_amt, second_amt = row.groups()
            eff = _parse_mmdd(mmdd, period_ending)
            if second_amt is None:
                balance = to_cents(first_amt)
                if "Beginning Balance" in description:
                    current.beginning_cents = balance
                    current.beginning_date = eff
                    current._running = balance
                elif "Ending Balance" in description:
                    current.ending_cents = balance
                    current.ending_date = eff
                continue

            amount = to_cents(first_amt)
            balance = to_cents(second_amt)
            prev = current._running
            if prev is None:
                raise StatementParseError(
                    f"{current.name}: transaction before Beginning Balance: {line!r}"
                )
            if prev + amount == balance:
                signed = amount
            elif prev - amount == balance:
                signed = -amount
            else:
                raise StatementParseError(
                    f"{current.name}: balance chain broken at {line!r} "
                    f"(prev {prev}, amount {amount}, balance {balance})"
                )
            current._running = balance
            current.rows.append(
                StatementRow(
                    eff_date=eff,
                    description=description.strip(),
                    amount_cents=signed,
                    balance_after_cents=balance,
                )
            )

    for section in sections.values():
        if section.ending_cents is not None and section._running is not None:
            if section._running != section.ending_cents:
                raise StatementParseError(
                    f"{section.name}: rows end at {section._running} cents "
                    f"but Ending Balance says {section.ending_cents}"
                )

    if not sections:
        raise StatementParseError("no share sections found — is this a Service CU statement?")
    return Statement(period_ending=period_ending, sections=sections)


def statement_snapshot_dates(section: StatementSection) -> list[tuple[date, int]]:
    """(as_of, balance) pairs for the section, using end-of-day convention.

    The Beginning Balance dated the 1st is the end-of-day balance of the last
    day of the previous month.
    """
    out = []
    if section.beginning_cents is not None and section.beginning_date is not None:
        out.append((section.beginning_date - timedelta(days=1), section.beginning_cents))
    if section.ending_cents is not None and section.ending_date is not None:
        out.append((section.ending_date, section.ending_cents))
    return out


def extract_pdf_pages(path) -> list[str]:
    from pypdf import PdfReader

    return [page.extract_text() or "" for page in PdfReader(str(path)).pages]
