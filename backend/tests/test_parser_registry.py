"""The parsers package is a registry, so a tree may ship a subset of it.

Nothing imports a parser module by name: each module declares a `SPEC` and the
registry discovers it. These tests pin the two properties that makes that safe
— precedence is explicit rather than import-order-dependent, and a parser that
is not in the tree resolves to None instead of raising.
"""
from __future__ import annotations

from app.models import PaymentMethod, PaymentMethodType
from app.services import import_hints
from app.services.parsers import registry
from app.services.parsers.detect import detect, detect_checking


def test_discovery_finds_parsers_without_naming_them():
    assert registry.card_specs(), "no card parser registered"
    assert registry.checking_specs(), "no checking parser registered"
    assert all(callable(spec.parse) for spec in registry.card_specs())


def test_precedence_is_the_declared_order_not_the_import_order():
    for specs in (registry.card_specs(), registry.checking_specs()):
        orders = [spec.order for spec in specs]
        assert orders == sorted(orders)


def test_checking_patterns_win_over_card_patterns():
    """A statement that both flows could claim belongs to the checking flow:
    `detect` defers to `detect_checking` before trying any card spec."""
    name = "nubank_conta_2026_05.csv"
    assert detect_checking(name) is not None
    assert detect(name) is None


def test_a_filename_no_parser_claims_resolves_to_none():
    for name in ("meridian_bank_statement.pdf", "aurora_visa.csv", "statement.csv"):
        assert detect(name) is None
        assert detect_checking(name) is None


def test_an_absent_parser_module_is_simply_not_registered(monkeypatch):
    """The public tree ships a subset of the parser modules. Simulate one being
    absent: the keyword that used to select it must resolve to None, the rest of
    the registry must keep working, and nothing may raise."""
    claimed = detect("citi_may.csv")
    assert claimed is not None

    survivors = [entry for entry in registry._entries if entry.module != "citi"]
    monkeypatch.setattr(registry, "_entries", survivors)

    assert detect("citi_may.csv") is None
    assert registry.card_specs(), "removing one module emptied the registry"
    assert "citi" not in registry.registered_modules()


def test_an_account_whose_parser_is_absent_gets_the_neutral_hint(monkeypatch):
    card = PaymentMethod(name="Citi Card", type=PaymentMethodType.CREDIT_CARD)
    assert import_hints.hint_for_payment_method(card).text != import_hints.NO_PARSER_HINT

    survivors = [entry for entry in registry._entries if entry.module != "citi"]
    monkeypatch.setattr(registry, "_entries", survivors)

    hint = import_hints.hint_for_payment_method(card)
    assert hint.text == import_hints.NO_PARSER_HINT
    assert hint.paste_capable is False
