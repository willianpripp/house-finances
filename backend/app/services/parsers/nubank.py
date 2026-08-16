"""Nubank Crédito (BRL credit card) — CSV export from the app
(`date,title,amount`) **and** the monthly fatura PDF.

Magic-byte dispatch: `%PDF` → `_parse_pdf`, otherwise `_parse_csv`.

Convention: `amount` is positive for purchases (BRL Decimal). Payment rows
(rare on this statement — the card is paid from the Nubank conta corrente, a
separate file) are surfaced via `is_payment=True` and routed to
`result.payments`.

PDF layout — last page carries the transaction list:

    TRANSAÇÕES DE 01 ABR A 01 MAI
    Alex Rivera R$ 313,10                         ← cardholder roll-up
    DD MMM •••• NNNN Merchant Name R$ NN,NN       ← purchase
    DD MMM IOF de "Merchant Name" R$ N,NN         ← IOF on FX purchase
    BRL 39.90 = USD 7.74                          ← FX info (ignored)
    Conversão: BRL 5.29 = USD 1 = R$ 5,29         ← FX info (ignored)
    Pagamentos e Financiamentos R$ 0,00           ← section header (ignored)
    DD MMM Saldo restante da fatura anterior R$ 0,00  ← carryover (ignored)
"""
from __future__ import annotations

import re
from datetime import date as date_type
from decimal import Decimal
from io import BytesIO

import pandas as pd
import pdfplumber

from app.services.parsers.registry import ParserKind, ParserSpec
from app.services.parsers.types import (
    EARLIEST_IMPORT_YEAR,
    ParsedTransaction,
    ParseResult,
)

PAYMENT_KEYWORDS = ("PAGAMENTO", "ESTORNO")

_MONTHS_PT = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}
# Each non-IOF/non-saldo charge row begins with `DD MMM •••• NNNN`.
_PDF_CHARGE_RE = re.compile(
    r"^(\d{2})\s+([A-Z]{3})\s+•+\s*\d{4}\s+(.+?)\s+R\$\s*([\d.]+,\d{2})\s*$"
)
# IOF rows omit the card mask: `DD MMM IOF de "..." R$ N,NN`.
_PDF_IOF_RE = re.compile(
    r"^(\d{2})\s+([A-Z]{3})\s+(IOF de [\"“][^\"”]+[\"”])\s+R\$\s*([\d.]+,\d{2})\s*$"
)
# `FATURA 08 MAI 2026` header → cycle year (and the date itself doubles
# as the due date for Nubank Crédito faturas — "fatura 08 mai 2026" =
# "pay this fatura by 08 mai 2026").
_PDF_FATURA_RE = re.compile(r"FATURA\s+(\d{2})\s+([A-Z]{3})\s+(\d{4})")
# `Fechamento 01 MAI` (or with year) → statement close date.
_PDF_FECHAMENTO_RE = re.compile(
    r"FECHAMENTO\s+(\d{2})\s+([A-Z]{3})(?:\s+(\d{4}))?",
    re.IGNORECASE,
)


def _parse_brl_amount(s: str) -> Decimal:
    return Decimal(s.replace(".", "").replace(",", "."))


def _parse_csv(content: bytes) -> ParseResult:
    # sep=None auto-detects the delimiter (native export is comma-separated;
    # a copy re-saved from Excel pt-BR may come tab-separated).
    df = pd.read_csv(BytesIO(content), sep=None, engine="python")
    result = ParseResult(parser="nubank_credito")

    for _, row in df.iterrows():
        try:
            desc = str(row["title"]).strip() if pd.notna(row["title"]) else ""
            # Accept dot-decimal (native export) and comma-decimal (pt-BR Excel,
            # e.g. "14,62" / "- 382,89"); tolerate a space after the sign.
            raw_amt = str(row["amount"]).strip().replace(" ", "")
            amount = _parse_brl_amount(raw_amt) if "," in raw_amt \
                else Decimal(str(round(float(raw_amt), 2)))
            txn_date = pd.to_datetime(row["date"]).date()
        except (KeyError, ValueError, TypeError):
            result.skipped += 1
            continue

        if txn_date.year < EARLIEST_IMPORT_YEAR:
            result.skipped += 1
            continue

        is_payment = amount < 0 or any(kw in desc.upper() for kw in PAYMENT_KEYWORDS)
        parsed = ParsedTransaction(
            transaction_date=txn_date,
            description=desc,
            amount=amount,
            is_payment=is_payment,
            raw={"date": str(txn_date), "title": desc, "amount": str(amount)},
        )

        if is_payment:
            result.payments.append(parsed)
        else:
            result.transactions.append(parsed)

    return result


def _parse_pdf(content: bytes) -> ParseResult:
    result = ParseResult(parser="nubank_credito")
    with pdfplumber.open(BytesIO(content)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    text_upper = text.upper()
    fatura_m = _PDF_FATURA_RE.search(text_upper)
    if fatura_m:
        due_day = int(fatura_m.group(1))
        due_mon = _MONTHS_PT.get(fatura_m.group(2))
        cycle_year = int(fatura_m.group(3))
        if due_mon is not None:
            try:
                result.due_date = date_type(cycle_year, due_mon, due_day)
            except ValueError:
                pass
    else:
        cycle_year = date_type.today().year

    fech_m = _PDF_FECHAMENTO_RE.search(text_upper)
    if fech_m:
        cd = int(fech_m.group(1))
        cm = _MONTHS_PT.get(fech_m.group(2))
        cy = int(fech_m.group(3)) if fech_m.group(3) else cycle_year
        if cm is not None:
            try:
                result.statement_close_date = date_type(cy, cm, cd)
            except ValueError:
                pass

    in_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.upper().startswith("TRANSAÇÕES DE"):
            in_section = True
            continue
        if not in_section:
            continue
        if line.startswith("Em cumprimento") or line.startswith("Como assegurado"):
            # End of transactions block (legal boilerplate at page bottom).
            break
        if line.startswith("Pagamentos e Financiamentos"):
            continue
        if "Saldo restante da fatura anterior" in line:
            continue
        if line.startswith("BRL ") or line.startswith("Conversão"):
            continue

        m = _PDF_CHARGE_RE.match(line)
        _is_iof = False  # parsed but unused today; kept for a future IOF rule
        if m is None:
            m = _PDF_IOF_RE.match(line)
            if m is None:
                continue
            _is_iof = True

        dd_s, mon_pt, desc, amount_s = m.group(1), m.group(2), m.group(3), m.group(4)
        month = _MONTHS_PT.get(mon_pt)
        if month is None:
            result.skipped += 1
            continue
        try:
            txn_date = date_type(cycle_year, month, int(dd_s))
        except ValueError:
            result.skipped += 1
            continue
        try:
            amount = _parse_brl_amount(amount_s)
        except (ValueError, ArithmeticError):
            result.skipped += 1
            continue

        description = re.sub(r"\s+", " ", desc).strip()
        # Nubank Crédito faturas never carry PAYMENT/ESTORNO rows — the
        # fatura payment lives on the Conta Corrente side as "PAGAMENTO DE
        # FATURA" (classified by the checking importer). All charge-line
        # regex matches are real purchases. IOF rows keep their literal
        # description so a categorizer rule can route them to Taxes.
        parsed = ParsedTransaction(
            transaction_date=txn_date,
            description=description,
            amount=amount,
            is_payment=False,
            raw={"date": str(txn_date), "title": description, "amount": str(amount)},
        )
        result.transactions.append(parsed)

    return result


def parse(content: bytes) -> ParseResult:
    if content[:4] == b"%PDF":
        return _parse_pdf(content)
    return _parse_csv(content)


SPEC = ParserSpec(
    source="NUBANK_CREDITO",
    parse=parse,
    kind=ParserKind.CARD,
    order=80,
    patterns=(("nubank_credito",), ("nubank_card",), ("nubank_fatura",)),
    formats=("CSV", "PDF"),
)
