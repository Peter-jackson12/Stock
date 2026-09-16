from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from dashboard.access import authorized_operator


@pytest.mark.parametrize("claims", [dict(iss="issuer", sub="user", exp=0),
    dict(iss="other", sub="user", exp=200), dict(iss="issuer", sub="wrong", exp=200),
    dict(iss="issuer", sub="user"), dict(iss="issuer", sub="user", exp=float("inf"))])
def test_invalid_operator_claims_are_denied(claims):
    assert not authorized_operator(claims, [{"issuer": "issuer", "subject": "user"}], now=100)


def test_exact_issuer_subject_and_expiry_required():
    assert authorized_operator(dict(iss="issuer", sub="user", exp=200),
                               [{"issuer": "issuer", "subject": "user"}], now=100)


def test_external_binding_stops_before_data_access(monkeypatch):
    import streamlit as st
    original = st.get_option
    monkeypatch.setattr(st, "get_option", lambda key: "0.0.0.0" if key == "server.address" else original(key))
    app = AppTest.from_string("from dashboard.access import require_access\nrequire_access()\nimport streamlit as st\nst.success('private data')").run()
    assert not app.exception and app.error and not app.success


@pytest.mark.parametrize("page", ["app.py", "pages/01_overview.py", "pages/02_performance.py",
                                 "pages/03_strategy_analysis.py", "pages/04_insight.py"])
def test_each_entrypoint_denies_unauthenticated_external_access(page, monkeypatch):
    import streamlit as st
    original = st.get_option
    monkeypatch.setattr(st, "get_option", lambda key: "0.0.0.0" if key == "server.address" else original(key))
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "dashboard" / page)).run()
    assert not app.exception and app.error
    assert not app.radio and not app.dataframe


def test_authenticated_fragment_does_not_duplicate_sidebar_logout(monkeypatch):
    import streamlit as st
    class Secrets:
        def to_dict(self):
            return dict(stock_access=dict(require_auth=True, operators=[dict(issuer="issuer", subject="user")]),
                        auth=dict(redirect_uri="https://example.test/oauth2callback", cookie_secret="fixture",
                            client_id="fixture", client_secret="fixture", server_metadata_url="https://example.test/metadata"))
    class User:
        is_logged_in = True
        def to_dict(self):
            import time
            return dict(iss="issuer", sub="user", exp=time.time() + 60)
    monkeypatch.setattr(st, "secrets", Secrets())
    monkeypatch.setattr(st, "user", User())
    app = AppTest.from_string("from dashboard.access import require_access\nrequire_access()\nrequire_access(show_logout=False)\nimport streamlit as st\nst.success('allowed')").run()
    assert not app.exception and app.success
    assert len([b for b in app.button if b.label == "로그아웃"]) == 1
