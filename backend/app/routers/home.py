"""Home dashboard stats endpoint.

`GET /api/home/stats` powers the launcher tiles' second line on `/`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.services.home import HomeStat, compute_home_stats

router = APIRouter(prefix="/api/home", tags=["home"])


class HomeStatOut(BaseModel):
    value: str
    delta_pct: float | None = None
    severity: str = "neutral"

    @classmethod
    def from_obj(cls, s: HomeStat) -> "HomeStatOut":
        return cls(value=s.value, delta_pct=s.delta_pct, severity=s.severity)


class HomeStatsOut(BaseModel):
    year: int
    month: int
    monthly: HomeStatOut
    annual: HomeStatOut
    savings: HomeStatOut
    debts: HomeStatOut
    assets: HomeStatOut
    warnings: HomeStatOut
    transactions: HomeStatOut
    income: HomeStatOut
    imports_: HomeStatOut
    rates: HomeStatOut
    rules: HomeStatOut


@router.get("/stats", response_model=HomeStatsOut)
def home_stats_endpoint(db: Session = Depends(get_db)) -> HomeStatsOut:
    s = compute_home_stats(db)
    return HomeStatsOut(
        year=s.year,
        month=s.month,
        monthly=HomeStatOut.from_obj(s.monthly),
        annual=HomeStatOut.from_obj(s.annual),
        savings=HomeStatOut.from_obj(s.savings),
        debts=HomeStatOut.from_obj(s.debts),
        assets=HomeStatOut.from_obj(s.assets),
        warnings=HomeStatOut.from_obj(s.warnings),
        transactions=HomeStatOut.from_obj(s.transactions),
        income=HomeStatOut.from_obj(s.income),
        imports_=HomeStatOut.from_obj(s.imports_),
        rates=HomeStatOut.from_obj(s.rates),
        rules=HomeStatOut.from_obj(s.rules),
    )
