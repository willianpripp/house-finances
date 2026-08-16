"""Statement import endpoints.

Two flavours:
- credit-card statements (CSV/MD) -> /api/imports/preview, /api/imports/commit
- bank-checking statements (PDF)   -> /api/imports/checking/preview, /api/imports/checking/commit

Preview is read-only and returns the parsed/categorized rows as JSON. Commit
re-parses the file and persists.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import ImportSource, PaymentMethod
from app.services.checking_importer import (
    CheckingContractConversion,
    CheckingImportCommitResult,
    CheckingImportPreview,
    build_checking_preview,
    commit_checking_import,
)
from app.services.importer import (
    DEFAULT_OWNER_USER_ID,
    ImportCommitResult,
    ImportPreview,
    build_preview,
    commit_import,
)
from app.services.parsers.cc_paste import parse_cc_paste
from app.services.parsers.detect import detect, detect_checking
from app.services.match_rules import load_match_rules
from app.services.parsers.checking_paste import parse_paste

router = APIRouter(prefix="/api/imports", tags=["imports"])


def _block_if_plaid(db: Session, payment_method_id: int) -> None:
    """Hard guard: a payment method fed by a provider auto-pull (Plaid or
    Pluggy) must not also be imported manually (would risk duplicates on
    posted-vs-authorized date drift). The provider is the single source for
    those accounts."""
    pm = db.get(PaymentMethod, payment_method_id)
    if pm is None:
        return
    provider = (
        "Plaid" if pm.plaid_account_id else "Pluggy" if pm.pluggy_account_id else None
    )
    if provider:
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{pm.name}' is auto-pulled via {provider} — manual import is "
                f"disabled for it. Use Connections → Pull now instead."
            ),
        )


class DetectOut(BaseModel):
    kind: str | None  # "checking" | "card" | None


@router.get("/detect", response_model=DetectOut)
def detect_endpoint(filename: str) -> DetectOut:
    """Which flow a filename routes to. The UI asks instead of mirroring the
    keyword tables, so no institution keyword ships in a template."""
    if detect_checking(filename) is not None:
        return DetectOut(kind="checking")
    if detect(filename) is not None:
        return DetectOut(kind="card")
    return DetectOut(kind=None)


@router.post("/preview", response_model=ImportPreview)
async def preview_endpoint(
    file: UploadFile = File(...),
    payment_method_id: int = Form(...),
    default_owner_user_id: int = Form(DEFAULT_OWNER_USER_ID),
    db: Session = Depends(get_db),
) -> ImportPreview:
    _block_if_plaid(db, payment_method_id)
    content = await file.read()
    try:
        return build_preview(
            db,
            file_content=content,
            filename=file.filename or "unnamed",
            payment_method_id=payment_method_id,
            default_owner_user_id=default_owner_user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _parse_json_list(raw: str | None, name: str, validator) -> list | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {name}: {exc}")
    if not isinstance(payload, list) or not all(validator(x) for x in payload):
        raise HTTPException(status_code=400, detail=f"Invalid {name}: type mismatch")
    return payload


@router.post("/commit", response_model=ImportCommitResult)
async def commit_endpoint(
    file: UploadFile = File(...),
    payment_method_id: int = Form(...),
    default_owner_user_id: int = Form(DEFAULT_OWNER_USER_ID),
    owner_user_ids: str | None = Form(None),
    skip_indices: str | None = Form(None),
    category_overrides: str | None = Form(None),
    merchant_overrides: str | None = Form(None),
    new_merchant_names: str | None = Form(None),
    save_rule_flags: str | None = Form(None),
    save_rule_amount_flags: str | None = Form(None),
    db: Session = Depends(get_db),
) -> ImportCommitResult:
    _block_if_plaid(db, payment_method_id)
    content = await file.read()
    parsed_owner_ids = _parse_json_list(
        owner_user_ids, "owner_user_ids", lambda x: isinstance(x, int)
    )
    parsed_skip_list = _parse_json_list(
        skip_indices, "skip_indices", lambda x: isinstance(x, int)
    )
    parsed_skip: set[int] | None = set(parsed_skip_list) if parsed_skip_list is not None else None
    parsed_cat = _parse_json_list(
        category_overrides, "category_overrides", lambda x: x is None or isinstance(x, int)
    )
    parsed_merch = _parse_json_list(
        merchant_overrides, "merchant_overrides", lambda x: x is None or isinstance(x, int)
    )
    parsed_names = _parse_json_list(
        new_merchant_names, "new_merchant_names", lambda x: x is None or isinstance(x, str)
    )
    parsed_flags = _parse_json_list(
        save_rule_flags, "save_rule_flags", lambda x: isinstance(x, bool)
    )
    parsed_amount_flags = _parse_json_list(
        save_rule_amount_flags, "save_rule_amount_flags", lambda x: isinstance(x, bool)
    )

    try:
        return commit_import(
            db,
            file_content=content,
            filename=file.filename or "unnamed",
            payment_method_id=payment_method_id,
            owner_user_ids=parsed_owner_ids,
            default_owner_user_id=default_owner_user_id,
            skip_indices=parsed_skip,
            category_overrides=parsed_cat,
            merchant_overrides=parsed_merch,
            new_merchant_names=parsed_names,
            save_rule_flags=parsed_flags,
            save_rule_amount_flags=parsed_amount_flags,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/checking/preview", response_model=CheckingImportPreview)
async def checking_preview_endpoint(
    file: UploadFile = File(...),
    payment_method_id: int = Form(...),
    db: Session = Depends(get_db),
) -> CheckingImportPreview:
    _block_if_plaid(db, payment_method_id)
    content = await file.read()
    try:
        return build_checking_preview(
            db,
            file_content=content,
            filename=file.filename or "unnamed",
            payment_method_id=payment_method_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/checking/commit", response_model=CheckingImportCommitResult)
async def checking_commit_endpoint(
    file: UploadFile = File(...),
    payment_method_id: int = Form(...),
    user_id: int | None = Form(None),
    skip_indices: str | None = Form(None),
    contract_conversions: str | None = Form(None),
    db: Session = Depends(get_db),
) -> CheckingImportCommitResult:
    _block_if_plaid(db, payment_method_id)
    content = await file.read()
    parsed_skip: set[int] | None = None
    if skip_indices:
        try:
            payload = json.loads(skip_indices)
            if not isinstance(payload, list) or not all(isinstance(x, int) for x in payload):
                raise ValueError("skip_indices must be a JSON list of integers")
            parsed_skip = set(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid skip_indices: {exc}")

    parsed_convs: dict[int, CheckingContractConversion] | None = None
    if contract_conversions:
        try:
            raw = json.loads(contract_conversions)
            if not isinstance(raw, list):
                raise ValueError("contract_conversions must be a JSON list")
            parsed_convs = {}
            for item in raw:
                conv = CheckingContractConversion.model_validate(item)
                parsed_convs[conv.index] = conv
        except (ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid contract_conversions: {exc}")

    try:
        return commit_checking_import(
            db,
            file_content=content,
            filename=file.filename or "unnamed",
            payment_method_id=payment_method_id,
            user_id=user_id,
            skip_indices=parsed_skip,
            contract_conversions=parsed_convs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------- Manual paste flow (checking) ----------

class CheckingPastePreviewRequest(BaseModel):
    payment_method_id: int
    text: str
    default_year: int
    date_format: str = "us"   # "us" (MM/DD) or "br" (DD/MM)
    decimal_mark: str = "us"  # "us" (1,234.56) or "br" (1.234,56)


class CheckingPastePreviewResponse(BaseModel):
    preview: CheckingImportPreview
    parse_errors: list[str]


@router.post("/checking/paste/preview", response_model=CheckingPastePreviewResponse)
async def checking_paste_preview_endpoint(
    body: CheckingPastePreviewRequest,
    db: Session = Depends(get_db),
) -> CheckingPastePreviewResponse:
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail=f"Payment method {body.payment_method_id} not found")
    _block_if_plaid(db, body.payment_method_id)
    try:
        parsed, errors = parse_paste(
            body.text,
            account_name=pm.name,
            default_year=body.default_year,
            date_format=body.date_format,
            decimal_mark=body.decimal_mark,
            rules=load_match_rules(db),
        )
        preview = build_checking_preview(
            db, payment_method_id=body.payment_method_id, pre_parsed=parsed
        )
        return CheckingPastePreviewResponse(preview=preview, parse_errors=errors)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------- Manual paste flow (credit card) ----------

class CCPastePreviewRequest(BaseModel):
    payment_method_id: int
    text: str


class CCPastePreviewResponse(BaseModel):
    preview: ImportPreview
    parse_errors: list[str]


@router.post("/paste/preview", response_model=CCPastePreviewResponse)
async def cc_paste_preview_endpoint(
    body: CCPastePreviewRequest,
    db: Session = Depends(get_db),
) -> CCPastePreviewResponse:
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail=f"Payment method {body.payment_method_id} not found")
    _block_if_plaid(db, body.payment_method_id)
    parsed, errors = parse_cc_paste(body.text)
    try:
        preview = build_preview(
            db,
            filename=f"manual_cc_paste_{pm.name}.txt",
            payment_method_id=body.payment_method_id,
            pre_parsed=parsed,
        )
        return CCPastePreviewResponse(preview=preview, parse_errors=errors)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class CCPasteCommitRequest(BaseModel):
    payment_method_id: int
    text: str
    default_owner_user_id: int = DEFAULT_OWNER_USER_ID
    owner_user_ids: list[int] | None = None
    skip_indices: list[int] | None = None
    category_overrides: list[int | None] | None = None
    merchant_overrides: list[int | None] | None = None
    new_merchant_names: list[str | None] | None = None
    save_rule_flags: list[bool] | None = None
    save_rule_amount_flags: list[bool] | None = None


@router.post("/paste/commit", response_model=ImportCommitResult)
async def cc_paste_commit_endpoint(
    body: CCPasteCommitRequest,
    db: Session = Depends(get_db),
) -> ImportCommitResult:
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail=f"Payment method {body.payment_method_id} not found")
    _block_if_plaid(db, body.payment_method_id)
    parsed, _errors = parse_cc_paste(body.text)
    try:
        return commit_import(
            db,
            filename=f"manual_cc_paste_{pm.name}.txt",
            payment_method_id=body.payment_method_id,
            owner_user_ids=body.owner_user_ids,
            default_owner_user_id=body.default_owner_user_id,
            skip_indices=set(body.skip_indices) if body.skip_indices else None,
            category_overrides=body.category_overrides,
            merchant_overrides=body.merchant_overrides,
            new_merchant_names=body.new_merchant_names,
            save_rule_flags=body.save_rule_flags,
            save_rule_amount_flags=body.save_rule_amount_flags,
            pre_parsed=parsed,
            source_override=ImportSource.MANUAL,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class CheckingPasteCommitRequest(BaseModel):
    payment_method_id: int
    text: str
    default_year: int
    date_format: str = "us"
    decimal_mark: str = "us"
    user_id: int | None = None
    skip_indices: list[int] | None = None
    contract_conversions: list[CheckingContractConversion] | None = None


@router.post("/checking/paste/commit", response_model=CheckingImportCommitResult)
async def checking_paste_commit_endpoint(
    body: CheckingPasteCommitRequest,
    db: Session = Depends(get_db),
) -> CheckingImportCommitResult:
    pm = db.get(PaymentMethod, body.payment_method_id)
    if pm is None:
        raise HTTPException(status_code=404, detail=f"Payment method {body.payment_method_id} not found")
    _block_if_plaid(db, body.payment_method_id)
    try:
        parsed, _errors = parse_paste(
            body.text,
            account_name=pm.name,
            default_year=body.default_year,
            date_format=body.date_format,
            decimal_mark=body.decimal_mark,
            rules=load_match_rules(db),
        )
        convs = (
            {c.index: c for c in body.contract_conversions}
            if body.contract_conversions
            else None
        )
        return commit_checking_import(
            db,
            payment_method_id=body.payment_method_id,
            user_id=body.user_id,
            skip_indices=set(body.skip_indices) if body.skip_indices else None,
            contract_conversions=convs,
            pre_parsed=parsed,
            filename=f"manual_paste_{parsed.period_start}_{parsed.period_end}.txt",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
