"""Phone-vs-desktop UI selection.

Note for Playwright later: its default user-agent has no "Mobi", so browser
tests must set document.cookie = "fin_ui=phone" to see the phone screens.
"""

MOBI_UA = {"user-agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobi Safari"}


def test_phone_ua_gets_phone_template(client):
    r = client.get("/", headers=MOBI_UA)
    assert r.status_code == 200
    assert 'class="phone-ui' in r.text
    assert "ptabs" in r.text


def test_desktop_ua_gets_desktop_template(client):
    r = client.get("/")
    assert r.status_code == 200
    assert 'class="phone-ui' not in r.text


def test_cookie_overrides_user_agent_both_ways(client):
    client.cookies.set("fin_ui", "desktop")
    assert 'class="phone-ui' not in client.get("/", headers=MOBI_UA).text

    client.cookies.set("fin_ui", "phone")
    assert 'class="phone-ui' in client.get("/").text
    del client.cookies["fin_ui"]


def test_connections_has_a_phone_template(client):
    r = client.get("/connections", headers=MOBI_UA)
    assert r.status_code == 200
    assert 'class="phone-ui' in r.text
    # The review flow ships without the save-rule toggles on the phone
    # (owner decision, 2026-08-14) but must keep the full commit-body contract.
    assert "save_rule_flags" in r.text
    assert "$ only" not in r.text


def test_connections_labels_every_item_with_its_payment_methods(client):
    """Both providers, both UIs: the connector/institution name alone does not
    identify a connection (two Plaid Items at one bank; every Pluggy item
    reports the same connector)."""
    for headers in ({}, MOBI_UA):
        body = client.get("/connections", headers=headers).text
        assert body.count("item.mapped_payment_methods.join(' + ')") == 2, headers
        assert "no accounts mapped" in body


def test_transactions_renders_incrementally_on_both_uis(client):
    """The API returns the whole filtered set; the page must window it instead
    of laying out every row (711 rows and growing)."""
    for headers in ({}, MOBI_UA):
        body = client.get("/transactions", headers=headers).text
        assert "const ROW_PAGE = 100;" in body, headers
        assert 'x-for="t in visibleRows()"' in body, headers
        assert "Load more (${remainingRows()} remaining)" in body, headers


def test_both_uis_know_which_facts_a_provider_owns(client):
    """One writer per fact, on the screen as well as in the API. Each of the
    three guarded forms must read the linkage the list endpoints expose, or the
    page offers an edit that can only 409. Every assertion here is a form that
    would otherwise look editable."""
    checks = {
        # /savings keys on the free-text account_name, so linkage is resolved
        # by exact payment-method name.
        "/savings": ["accountProvider(", "?.provider"],
        # /debts resolves the card behind each balance row.
        "/debts": ["cardProvider(", "providerCards()"],
        # /transactions locks the provider-owned fields and drops them from the
        # PATCH body, which is what keeps category editable.
        "/transactions": ["editing.provider", "pmLabel(pm)", "!!pm.provider"],
    }
    for path, needles in checks.items():
        for headers in ({}, MOBI_UA):
            body = client.get(path, headers=headers).text
            for needle in needles:
                assert needle in body, (path, headers, needle)


def test_monthly_report_has_a_phone_template(client):
    r = client.get("/reports/monthly", headers=MOBI_UA)
    assert r.status_code == 200
    assert 'class="phone-ui' in r.text
    assert "phoneMonthly" in r.text


def test_every_converted_page_serves_phone_on_mobi(client):
    pages = [
        "/", "/receivables", "/connections", "/reports/monthly",
        "/transactions", "/debts", "/savings",
        "/assets", "/warnings", "/reports/annual",
    ]
    for path in pages:
        r = client.get(path, headers=MOBI_UA)
        assert r.status_code == 200, path
        assert 'class="phone-ui' in r.text, f"{path} did not serve the phone template"


def test_unconverted_page_serves_desktop_even_on_phone(client):
    # imports, guide, and rules are desktop-only by decision (2026-08-14).
    for path in ("/imports", "/guide"):
        r = client.get(path, headers=MOBI_UA)
        assert r.status_code == 200, path
        assert 'class="phone-ui' not in r.text, path


def test_ui_switch_sets_and_clears_cookie(client):
    r = client.post("/ui", data={"choice": "phone"}, follow_redirects=False)
    assert r.status_code == 303
    assert "fin_ui=phone" in r.headers.get("set-cookie", "")

    r = client.post("/ui", data={"choice": "auto"}, follow_redirects=False)
    set_cookie = r.headers.get("set-cookie", "")
    assert "fin_ui" in set_cookie and 'Max-Age=0' in set_cookie
    del client.cookies["fin_ui"]


def test_manifest_is_public_and_carries_the_proxy_prefix(client):
    from fastapi.testclient import TestClient

    from app.main import app

    anonymous = TestClient(app)
    r = anonymous.get("/manifest.webmanifest", headers={"x-forwarded-prefix": "/finances/"})
    assert r.status_code == 200
    body = r.json()
    assert body["start_url"] == "/finances/"
    assert body["icons"][0]["src"].startswith("/finances/static/")

    direct = anonymous.get("/manifest.webmanifest").json()
    assert direct["start_url"] == "/"


def test_portal_link_is_deployment_config_not_code(client):
    """A deployment's portal URL once leaked into the tree by being hardcoded
    in both headers (2026-08-15). It now comes from PORTAL_URL: unset (the
    test environment) renders no icon on either UI; set, both UIs render it."""
    from app.main import templates

    for headers in ({}, MOBI_UA):
        text = client.get("/", headers=headers).text
        assert 'title="Casa portal"' not in text

    templates.env.globals["portal_url"] = "https://portal.example.test"
    try:
        for headers in ({}, MOBI_UA):
            text = client.get("/", headers=headers).text
            assert 'href="https://portal.example.test"' in text
    finally:
        templates.env.globals["portal_url"] = ""


def test_ui_strings_are_english_only():
    """English-only UI rule. Portuguese leaked in three times
    (home placeholder, warnings loaders, installment labels), each time on a
    screen a reviewer sees. The pre-publication sweep of 2026-08-15 found six
    more the list did not cover (section comments, a card-statement noun, a
    colour comment, and the portal link title), so they are listed here too.
    This walks both UIs' templates."""
    import pathlib
    import re

    forbidden = (
        "Carregando",
        "Parcela",
        "parcela",
        "Sem alertas",
        "Próximo",
        "VISÃO",
        "LANÇAMENTOS",
        "cinza",
        "fatura",
        "Casa",
        "Dinheiro",
        "dinheiro",
    )
    offenders = []
    for path in sorted(pathlib.Path("app/templates").rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for word in forbidden:
            if re.search(rf"\b{word}\b", text):
                offenders.append(f"{path}: {word!r}")
    assert not offenders, "Portuguese in the UI: " + "; ".join(offenders)


def test_warnings_demotes_the_pushed_feeds_on_both_uis():
    """Phase D2 (2026-08-19): expiring contracts and the spend goals are the
    page; the overdraft forecast and the statement alerts sit behind one
    collapsed toggle, because they now arrive as calendar reminders. Both feeds
    must still be REQUESTED (they feed the push and the Home summary), so this
    checks placement, not absence — and it checks both UIs, since the horizon
    label bug of 2026-08-15 was exactly a change applied to one twin only."""
    import pathlib

    for name in ("warnings.html", "phone/warnings.html"):
        text = (pathlib.Path("app/templates") / name).read_text(encoding="utf-8")

        assert "secondary: false" in text, f"{name}: the section must start collapsed"
        assert 'x-show="secondary" x-cloak' in text, f"{name}: no disclosure wrapper"

        # Still fetched: the services feed the push and the Home summary.
        for endpoint in ("/api/warnings/overdraft", "/api/warnings/statements"):
            assert endpoint in text, f"{name} stopped requesting {endpoint}"

        toggle = text.index('@click="secondary = !secondary"')
        assert text.index("expiring || []") < toggle, (
            f"{name}: contracts / installments must come before the collapsed section"
        )
        assert text.index("spendGoals || []") < toggle, (
            f"{name}: the spend-goal cards must come before the collapsed section"
        )
        assert text.index("overdrafts || []") > toggle, (
            f"{name}: the overdraft forecast must be inside the collapsed section"
        )
        assert text.index("statementAlerts || []") > toggle, (
            f"{name}: the statement alerts must be inside the collapsed section"
        )


def test_savings_history_is_collapsed_on_desktop_and_absent_on_phone():
    """Demo review, 2026-08-19: the desktop history listing (per-date
    snapshots under the filters) starts collapsed behind the same disclosure
    idiom as the warnings page, showing the snapshot count in its header. The
    phone twin drops history entirely, no toggle (owner decision): only the
    add-snapshot sheet remains there."""
    import pathlib

    desktop = (pathlib.Path("app/templates") / "savings.html").read_text(encoding="utf-8")
    assert "historyOpen: false" in desktop, "desktop history must start collapsed"
    assert 'x-show="historyOpen" x-cloak' in desktop, "no disclosure wrapper on the listing"
    toggle = desktop.index('@click="historyOpen = !historyOpen"')
    assert desktop.index("(history?.snapshots || []).length") < toggle + len(
        '@click="historyOpen = !historyOpen"'
    ) + 400, "the toggle header must show the snapshot count"
    assert desktop.index("groupedHistory()") > toggle, (
        "the per-date listing must be inside the collapsed section"
    )

    phone = (pathlib.Path("app/templates/phone") / "savings.html").read_text(encoding="utf-8")
    assert "groupedHistory" not in phone, "phone must not carry the history listing at all"
    assert "reloadHistory" not in phone, "phone must not fetch history data it never shows"
    assert "historyOpen" not in phone, "phone has no toggle to remove: history is just gone"


def test_warnings_horizon_labels_match_the_requests_on_both_uis():
    """Each warnings panel states its own horizon in its heading. Desktop was
    corrected to 90 days and the phone twin was missed (2026-08-15), leaving
    it claiming 60 while listing an item 76 days out. A stated horizon must be
    one the page actually asks the API for."""
    import pathlib
    import re

    for name in ("warnings.html", "phone/warnings.html"):
        text = (pathlib.Path("app/templates") / name).read_text(encoding="utf-8")
        requested = set(re.findall(r"horizon_days=(\d+)", text))
        assert requested, f"{name} requests no horizon at all"
        stated = set(re.findall(r"next (\d+) days", text))
        assert stated <= requested, (
            f"{name} states horizons {stated - requested} it never requests "
            f"(it asks for {sorted(requested)})"
        )
