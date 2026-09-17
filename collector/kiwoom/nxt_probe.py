"""OCX 없는 소규모 구독 검증 계획/원문 관측 계약. 운영 수집기에 연결하지 않는다."""
from datetime import datetime, timedelta, timezone
import re

from engine.nxt_session import venue_from_code

KST = timezone(timedelta(hours=9))


def prepare_plan(base_code, *, server, start_at, duration_seconds=60):
    """한 종목의 세 라우팅 코드를 순차 검증할 계획만 만든다. 로그인하지 않는다."""
    if not isinstance(base_code, str) or not re.fullmatch(r"[0-9]{6}", base_code):
        raise ValueError("six ASCII digits required")
    if server not in ("mock", "live"):
        raise ValueError("explicit server required")
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= 300:
        raise ValueError("duration must be 1..300 seconds")
    start = datetime.fromisoformat(start_at)
    if start.utcoffset() != timedelta(hours=9):
        raise ValueError("explicit KST start required")
    return dict(schema="ocx_nxt_probe_plan_1", base_code=base_code, server=server,
                start_at=start.isoformat(), duration_seconds=duration_seconds,
                subscription_codes=[base_code, base_code + "_NX", base_code + "_AL"],
                execution="offline_plan_only", subscription_method="SetRealReg_unverified",
                nxt_coverage="unconfirmed", event_venue="unknown",
                guide="C:/OpenAPI/koa_devguide.xml:280-299,2432-2442",
                production_cutoff_compatible=(start + timedelta(seconds=duration_seconds)
                    <= start.replace(hour=15, minute=35, second=0, microsecond=0)))


def observation(*, subscription_code, callback_code, real_type, fids,
                received_at_utc, received_ns, session_id, server):
    """수신 원문과 라우팅 근거를 분리한다. 접미사만으로 개별 체결 거래소를 인증하지 않는다."""
    if server not in ("mock", "live") or not session_id:
        raise ValueError("observed server and session required")
    if not all(isinstance(v, str) for v in (subscription_code, callback_code, real_type)):
        raise ValueError("verbatim callback text required")
    if not isinstance(fids, dict) or any(not isinstance(k, str) or
            (v is not None and not isinstance(v, str)) for k, v in fids.items()):
        raise ValueError("raw FID text required")
    at = datetime.fromisoformat(received_at_utc.replace("Z", "+00:00"))
    if at.utcoffset() != timedelta(0) or type(received_ns) is not int or received_ns < 0:
        raise ValueError("UTC and monotonic receipt clock required")
    local = at.astimezone(KST)
    second = local.hour * 3600 + local.minute * 60 + local.second
    period = ("aftermarket" if 56400 <= second < 72000 else
              "regular" if 32400 <= second < 55800 else
              "strategy_premarket_window" if 28800 <= second < 31800 else "outside_probe_windows")
    return dict(schema="ocx_nxt_probe_observation_1", session_id=session_id, server=server,
                subscription_code_raw=subscription_code, callback_code_raw=callback_code,
                real_type_raw=real_type, fids=dict(fids), received_at_utc=received_at_utc,
                received_ns=received_ns, receipt_market_date=local.date().isoformat(),
                receipt_market_period=period, routing_scope_hint=venue_from_code(callback_code),
                venue="unknown", venue_evidence="code_suffix_only_unverified_for_SetRealReg",
                nxt_coverage="unconfirmed")
