"""Nubank Conta Corrente (BRL personal checking) — CSV statement.

Format (UTF-8):

    Data,Valor,Identificador,Descrição
    10/04/2026,4000.00,69d933ae-...,Transferência Recebida - Sam Rivera - ...
    01/04/2026,-302.14,69cd16c5-...,Aplicação RDB

- Date: DD/MM/YYYY.
- Valor: signed decimal (positive = inflow, negative = outflow).
- Descrição: free-form Pix/transfer text; the shared `classify_description`
  keyword tables route the partner's deposits to RENT_DEPOSIT, the
  Aplicação/Resgate RDB pair to INTERNAL_TRANSFER, and the holder's own
  sweep (in/out of a second personal account) to INTERNAL_TRANSFER.

Special handling
- `skip_snapshot=True`: the holder keeps this account at ~zero (cash is swept
  to invest products immediately; the investment result is recorded by hand
  via /savings). A SavingsSnapshot here would distort net worth.
- Partner → holder rent deposits are classified RENT_DEPOSIT and routed
  to `income_entries.RENTS_BRAZIL` for `month+1` by the importer (lag-1
  convention).
"""
from __future__ import annotations

import csv
import io
import re
from datetime import date as date_type, datetime
from decimal import Decimal
from io import BytesIO

import pdfplumber

from app.services.parsers.registry import ParserKind, ParserSpec
from app.services.parsers.checking import (
    CheckingActivity,
    CheckingParseResult,
    MatchRules,
    classify_description,
)


# Reported for display only: the savings-snapshot side effect keys on
# `payment_methods.name`, and this parser opts out of snapshots anyway.
_ACCOUNT_NAME = "Nubank Checking"

_MONTHS_PT = {
    "JAN": 1, "FEV": 2, "MAR": 3, "ABR": 4, "MAI": 5, "JUN": 6,
    "JUL": 7, "AGO": 8, "SET": 9, "OUT": 10, "NOV": 11, "DEZ": 12,
}
_PDF_DAY_RE = re.compile(r"^(\d{2})\s+([A-Z]{3})\s+(\d{4})\s+Total de (entradas|saídas)\s*[+-]?\s*[\d.,]+$")
_PDF_AMOUNT_TAIL_RE = re.compile(r"\s+([\d.]+,\d{2})\s*$")


def _parse_brl_date(s: str) -> date_type:
    return datetime.strptime(s.strip(), "%d/%m/%Y").date()


def _parse_brl_amount(s: str) -> Decimal:
    """'12.366,00' or '302,14' → Decimal."""
    s = s.strip().replace(".", "").replace(",", ".")
    return Decimal(s)


def _parse_pdf(content: bytes, rules: MatchRules) -> CheckingParseResult:
    """Extract transactions from a Nubank Conta Corrente PDF extract.

    Layout per day:
      DD MMM YYYY Total de entradas + X,YY
        <merchant line 1> NN,NN
        <continuation>            ← optional, no trailing amount
        ...
      Total de saídas - Z,ZZ
        <merchant line 1> MM,MM
        <continuation>
        ...
    """
    with pdfplumber.open(BytesIO(content)) as pdf:
        text = "\n".join(p.extract_text() or "" for p in pdf.pages)

    activities: list[CheckingActivity] = []
    dates: list[date_type] = []
    current_date: date_type | None = None
    current_sign: int = 0  # +1 entrada, -1 saída
    buffer: list[str] = []
    last_amount: Decimal | None = None

    def flush_buffer():
        nonlocal buffer, last_amount
        if current_date is None or last_amount is None or not buffer:
            buffer = []
            last_amount = None
            return
        desc = re.sub(r"\s+", " ", " ".join(buffer)).strip()
        signed = last_amount * current_sign
        if signed == 0 or not desc:
            buffer = []
            last_amount = None
            return
        classification, hint = classify_description(desc, signed, rules=rules)
        activities.append(CheckingActivity(
            activity_date=current_date,
            description=desc,
            amount=signed,
            running_balance=None,
            classification=classification,
            match_hint=hint,
            raw_lines=[desc],
        ))
        dates.append(current_date)
        buffer = []
        last_amount = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Day anchor with section toggle: switch to entrada / saída scope.
        m = _PDF_DAY_RE.match(line)
        if m:
            flush_buffer()
            dd, mon_pt, yyyy, kind = m.group(1), m.group(2), m.group(3), m.group(4)
            month = _MONTHS_PT.get(mon_pt)
            if month is None:
                current_date = None
                continue
            current_date = date_type(int(yyyy), month, int(dd))
            current_sign = +1 if kind.startswith("entrada") else -1
            continue

        # Section toggle inside the same day (after entradas come saídas).
        # Format: "Total de saídas - X,YY" (no leading day token).
        if line.startswith("Total de saídas"):
            flush_buffer()
            current_sign = -1
            continue
        if line.startswith("Total de entradas"):
            flush_buffer()
            current_sign = +1
            continue

        # Boilerplate / page noise — skip and end any buffer.
        if (
            any(line.upper().startswith(p) for p in rules.noise_prefixes)
            or line.startswith("CPF")
            or line.startswith("Agência")
            or line.startswith("Saldo")
            or line.startswith("Rendimento")
            or line.startswith("R$ ")
            or line.startswith("Movimentações")
            or line.startswith("Tem alguma dúvida")
            or line.startswith("Caso a solução")
            or line.startswith("metropolitanas)")
            or line.startswith("Extrato gerado")
            or line.startswith("O saldo líquido")
            or line.startswith("Não nos responsabilizamos")
            or line.startswith("Asseguramos")
            or line.startswith("Nu Financeira")
            or line.startswith("Investimento")
            or line.startswith("CNPJ:")
            or "VALORES EM R$" in line
            or line.startswith("disponíveis em nubank.com.br")
        ):
            flush_buffer()
            continue

        if current_date is None:
            continue

        # Transaction line: ends with an amount.
        amt_m = _PDF_AMOUNT_TAIL_RE.search(line)
        if amt_m:
            # A new transaction starts here — flush any prior one.
            flush_buffer()
            try:
                last_amount = _parse_brl_amount(amt_m.group(1))
            except (ValueError, ArithmeticError):
                last_amount = None
                continue
            head = line[: amt_m.start()].strip()
            buffer = [head] if head else []
        else:
            # Continuation of the previous transaction's description.
            if buffer:
                buffer.append(line)

    flush_buffer()

    period_m = re.search(
        r"(\d{2})\s+DE\s+([A-Z]+)\s+DE\s+(\d{4})\s+a\s+(\d{2})\s+DE\s+([A-Z]+)\s+DE\s+(\d{4})",
        text.upper(),
    )
    _MONTHS_FULL = {
        "JANEIRO": 1, "FEVEREIRO": 2, "MARÇO": 3, "ABRIL": 4, "MAIO": 5, "JUNHO": 6,
        "JULHO": 7, "AGOSTO": 8, "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12,
    }
    if period_m and dates:
        try:
            ps = date_type(int(period_m.group(3)), _MONTHS_FULL[period_m.group(2)], int(period_m.group(1)))
            pe = date_type(int(period_m.group(6)), _MONTHS_FULL[period_m.group(5)], int(period_m.group(4)))
        except (KeyError, ValueError):
            ps, pe = min(dates), max(dates)
    elif dates:
        ps, pe = min(dates), max(dates)
    else:
        ps = pe = date_type.today()

    return CheckingParseResult(
        parser="nubank_conta",
        account_name=_ACCOUNT_NAME,
        period_start=ps,
        period_end=pe,
        beginning_balance=Decimal("0"),
        ending_balance=Decimal("0"),
        activities=activities,
        skip_snapshot=True,
    )


def _parse_csv(content: bytes, rules: MatchRules) -> CheckingParseResult:
    text = content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(text))

    activities: list[CheckingActivity] = []
    dates: list[date_type] = []

    for row in reader:
        try:
            txn_date = _parse_brl_date(row["Data"])
            amount = Decimal(str(row["Valor"]).strip())
        except (KeyError, ValueError):
            continue

        description = (row.get("Descrição") or "").strip()
        classification, hint = classify_description(description, amount, rules=rules)

        activities.append(
            CheckingActivity(
                activity_date=txn_date,
                description=description,
                amount=amount,
                running_balance=None,
                classification=classification,
                match_hint=hint,
                raw_lines=[",".join(str(v) for v in row.values())],
            )
        )
        dates.append(txn_date)

    if not activities:
        return CheckingParseResult(
            parser="nubank_conta",
            account_name=_ACCOUNT_NAME,
            period_start=date_type.today(),
            period_end=date_type.today(),
            beginning_balance=Decimal("0"),
            ending_balance=Decimal("0"),
            activities=[],
            skip_snapshot=True,
        )

    return CheckingParseResult(
        parser="nubank_conta",
        account_name=_ACCOUNT_NAME,
        period_start=min(dates),
        period_end=max(dates),
        beginning_balance=Decimal("0"),
        ending_balance=Decimal("0"),
        activities=activities,
        skip_snapshot=True,
    )


def parse(content: bytes, rules: MatchRules) -> CheckingParseResult:
    if content[:4] == b"%PDF":
        return _parse_pdf(content, rules)
    return _parse_csv(content, rules)


SPEC = ParserSpec(
    source="CHECKING_NUBANK",
    parse=parse,
    kind=ParserKind.CHECKING,
    order=30,
    patterns=(("nubank", "extrato"), ("nubank", "conta")),
    formats=("CSV", "PDF"),
)
