"""Categorization rules CRUD.

Drives /rules — list, add, edit priority/merchant/category, delete. The
categorizer reloads its rule cache on each import, so changes here take
effect on the next preview.

Also exposes a one-shot 'recategorize Variable' endpoint that
re-runs the categorizer across transactions currently sitting in the
fallback Variable category and applies any matching rule.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import CategorizationRule, Category, Merchant, Transaction
from app.services.categorizer import DEFAULT_CATEGORY_NAME, Categorizer

router = APIRouter(prefix="/api/rules", tags=["categorization-rules"])


class RuleOut(BaseModel):
    id: int
    keyword: str
    priority: int
    merchant_id: int
    merchant_name: str
    category_id: int
    category_name: str
    category_type: str
    category_icon: str | None = None

    @classmethod
    def from_db(cls, r: CategorizationRule) -> "RuleOut":
        return cls(
            id=r.id,
            keyword=r.keyword,
            priority=r.priority,
            merchant_id=r.merchant_id,
            merchant_name=r.merchant.name,
            category_id=r.category_id,
            category_name=r.category.name,
            category_type=r.category.type.value,
            category_icon=r.category.icon,
        )


class RuleCreate(BaseModel):
    keyword: str = Field(min_length=1, max_length=100)
    merchant_id: int
    category_id: int
    priority: int = Field(default=100, ge=1, le=999)


class RulePatch(BaseModel):
    keyword: str | None = Field(default=None, min_length=1, max_length=100)
    merchant_id: int | None = None
    category_id: int | None = None
    priority: int | None = Field(default=None, ge=1, le=999)


def _validate_fks(session: Session, *, merchant_id: int | None, category_id: int | None):
    if merchant_id is not None and session.get(Merchant, merchant_id) is None:
        raise HTTPException(404, f"Merchant {merchant_id} not found")
    if category_id is not None and session.get(Category, category_id) is None:
        raise HTTPException(404, f"Category {category_id} not found")


@router.get("", response_model=list[RuleOut])
def list_rules(db: Session = Depends(get_db)) -> list[RuleOut]:
    rules = db.scalars(
        select(CategorizationRule).order_by(CategorizationRule.priority, CategorizationRule.keyword)
    ).all()
    return [RuleOut.from_db(r) for r in rules]


@router.post("", response_model=RuleOut, status_code=status.HTTP_201_CREATED)
def create_rule(body: RuleCreate, db: Session = Depends(get_db)) -> RuleOut:
    _validate_fks(db, merchant_id=body.merchant_id, category_id=body.category_id)
    keyword = body.keyword.strip().lower()
    if not keyword:
        raise HTTPException(400, "keyword must not be empty")
    dup = db.scalar(select(CategorizationRule).filter_by(keyword=keyword))
    if dup is not None:
        raise HTTPException(409, f"Rule for keyword '{keyword}' already exists (id {dup.id})")
    rule = CategorizationRule(
        keyword=keyword,
        merchant_id=body.merchant_id,
        category_id=body.category_id,
        priority=body.priority,
    )
    db.add(rule)
    db.commit()
    db.refresh(rule)
    return RuleOut.from_db(rule)


@router.patch("/{rule_id}", response_model=RuleOut)
def patch_rule(rule_id: int, body: RulePatch, db: Session = Depends(get_db)) -> RuleOut:
    rule = db.get(CategorizationRule, rule_id)
    if rule is None:
        raise HTTPException(404, f"Rule {rule_id} not found")
    _validate_fks(db, merchant_id=body.merchant_id, category_id=body.category_id)
    if body.keyword is not None:
        new_kw = body.keyword.strip().lower()
        if new_kw != rule.keyword:
            dup = db.scalar(select(CategorizationRule).filter_by(keyword=new_kw))
            if dup is not None:
                raise HTTPException(409, f"Rule for keyword '{new_kw}' already exists (id {dup.id})")
            rule.keyword = new_kw
    if body.merchant_id is not None:
        rule.merchant_id = body.merchant_id
    if body.category_id is not None:
        rule.category_id = body.category_id
    if body.priority is not None:
        rule.priority = body.priority
    db.commit()
    db.refresh(rule)
    return RuleOut.from_db(rule)


class RecategorizeResult(BaseModel):
    examined: int
    recategorized: int
    new_merchants: int
    sample: list[dict]


@router.post("/recategorize-variable", response_model=RecategorizeResult)
def recategorize_variable(
    dry_run: bool = False,
    db: Session = Depends(get_db),
) -> RecategorizeResult:
    """Re-run the categorizer across all transactions in the fallback
    'Variable' category. Any row whose description now matches a rule gets
    its merchant + category updated. Pass dry_run=true to preview without
    persisting."""
    default_cat = db.scalar(select(Category).filter_by(name=DEFAULT_CATEGORY_NAME))
    if default_cat is None:
        raise HTTPException(500, f"Default category '{DEFAULT_CATEGORY_NAME}' missing")

    txns = db.scalars(
        select(Transaction).where(Transaction.category_id == default_cat.id)
    ).all()
    categorizer = Categorizer(db)

    recategorized = 0
    new_merchants = 0
    sample: list[dict] = []
    for t in txns:
        if not t.description:
            continue
        match = categorizer.classify(t.description)
        # If the categorizer returns the default category, the rule didn't
        # match (or the matched rule itself points to Variable — unlikely).
        if match.category_id == default_cat.id:
            continue
        if match.merchant_id is None:
            merchant = categorizer.get_or_create_merchant(match.merchant_name, match.category_id)
            new_merchants += 1
            new_merchant_id = merchant.id
        else:
            new_merchant_id = match.merchant_id
        if not dry_run:
            t.merchant_id = new_merchant_id
            t.category_id = match.category_id
        recategorized += 1
        if len(sample) < 20:
            sample.append({
                "transaction_id": t.id,
                "date": str(t.transaction_date),
                "description": (t.description or "")[:80],
                "new_merchant": match.merchant_name,
                "new_category": match.category_name,
                "matched_keyword": match.keyword,
            })

    if not dry_run:
        db.commit()
    else:
        db.rollback()

    return RecategorizeResult(
        examined=len(txns),
        recategorized=recategorized,
        new_merchants=new_merchants,
        sample=sample,
    )


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_rule(rule_id: int, db: Session = Depends(get_db)) -> Response:
    rule = db.get(CategorizationRule, rule_id)
    if rule is None:
        raise HTTPException(404, f"Rule {rule_id} not found")
    db.delete(rule)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
