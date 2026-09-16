"""Local default; external binding requires OIDC plus an explicit operator identity."""
import math
import time


def authorized_operator(claims, operators, *, now=None):
    now = time.time() if now is None else now
    expiry = claims.get("exp")
    if type(expiry) not in (float, int) or not math.isfinite(expiry) or expiry <= now:
        return False
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not isinstance(issuer, str) or not issuer or not isinstance(subject, str) or not subject:
        return False
    return any(isinstance(row, dict) and row.get("issuer") == issuer and row.get("subject") == subject
               for row in operators)


def require_access(*, show_logout=True):
    import streamlit as st
    try:
        settings = st.secrets.to_dict()
    except FileNotFoundError:
        settings = {}
    policy = settings.get("stock_access", {})
    local = st.get_option("server.address") in ("127.0.0.1", "localhost", "::1")
    if local and not policy.get("require_auth", False):
        return
    auth = settings.get("auth", {})
    operators = policy.get("operators", [])
    required = ("redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url")
    if (not isinstance(operators, list) or not operators or
            not all(isinstance(auth.get(key), str) and auth[key] for key in required)):
        st.error("외부 접속에는 로그인 제공자와 허용 운영자 설정이 필요합니다. 현재 운영 기능은 차단되어 있습니다.")
        st.stop()
    if not auth["redirect_uri"].startswith("https://") or not auth["server_metadata_url"].startswith("https://"):
        st.error("원격 인증에는 HTTPS 설정이 필요합니다.")
        st.stop()
    if not st.user.is_logged_in:
        st.button("운영자 로그인", on_click=st.login)
        st.stop()
    claims = st.user.to_dict()
    if not authorized_operator(claims, operators):
        st.error("허용된 운영자가 아니거나 인증이 만료되었습니다.")
        st.button("로그아웃", on_click=st.logout)
        st.stop()
    if show_logout:
        st.sidebar.button("로그아웃", on_click=st.logout, key="operator_logout")
