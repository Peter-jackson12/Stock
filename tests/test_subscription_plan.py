"""tests/test_subscription_plan.py — 구독 계획: 모드별 접미사 허용과 목록 근거

운영 수집기는 종목코드를 여섯 자리로만 받는다. 그 검증을 전역으로 풀면 정규장 경로까지
접미사를 받게 되므로, 명시적 NXT 모드에서만 허용한다.

그리고 어떤 종목이 NXT 거래 대상인지는 조회로 받는 여섯 자리 목록에 들어 있지 않다.
사용자가 입력했다는 사실과 NXT 거래 대상임을 확인했다는 사실은 다르며, 후자의 기본값은
미확인이다.

실행:
    uv run pytest tests/test_subscription_plan.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector.kiwoom.subscription_plan import (  # noqa: E402
    MAX_CODES,
    plan_from_cli,
    MODE_KRX_REGULAR,
    MODE_NXT,
    SubscriptionPlan,
    SymbolListSource,
    build_plan,
    record_callback,
    split_code,
)

SOURCE = SymbolListSource(origin="사용자 입력", verified_at="2026-09-17T16:30:00+09:00")


def _nxt(codes="005930_NX", source=SOURCE):
    return build_plan(MODE_NXT, codes, source=source, market_profile="nxt_aftermarket")


def _krx(codes="005930"):
    return build_plan(MODE_KRX_REGULAR, codes, source=SOURCE, market_profile="krx_regular")


def _rejects(call, needle=None):
    try:
        call()
    except ValueError as exc:
        if needle:
            assert needle in str(exc), f"메시지에 {needle!r} 없음: {exc}"
        return
    raise AssertionError("거부해야 할 입력을 통과시켰다")


# ── 기존 정규장 호환성 ──────────────────────────────────────────────────────

def test_정규장_모드는_지금과_같은_규칙이다():
    plan = _krx("005930,000660")
    assert plan.codes == ("005930", "000660")


def test_정규장_모드는_접미사를_받지_않는다():
    # 검증을 전역으로 푸는 것이 아니다. NXT 는 모드를 명시해야 한다.
    for code in ("005930_NX", "005930_AL"):
        _rejects(lambda c=code: _krx(c), "접미사를 받지 않는다")


# ── NXT 모드 ────────────────────────────────────────────────────────────────

def test_NXT_모드에서만_접미사가_허용된다():
    plan = _nxt("005930_NX,000660_NX")
    assert plan.codes == ("005930_NX", "000660_NX")
    assert plan.mode == MODE_NXT


def test_NXT_모드는_맨_코드를_거부한다():
    _rejects(lambda: _nxt("005930"), "_NX 접미사가 필요하다")


def test_통합코드는_운영_구독에_쓰지_않는다():
    # 체결 거래소를 단정할 수 없어 비교 검증 전용이다.
    _rejects(lambda: _nxt("005930_AL"), "비교 검증에만")


def test_잘못된_접미사를_거부한다():
    for bad in ("005930_XX", "005930_", "005930_NXT", "5930_NX", "abcdef_NX"):
        _rejects(lambda c=bad: _nxt(c))


def test_소문자_접미사는_받아들이고_정규화한다():
    assert _nxt("005930_nx").codes == ("005930_NX",)


# ── 중복 ────────────────────────────────────────────────────────────────────

def test_중복_코드를_거부한다():
    _rejects(lambda: _nxt("005930_NX,005930_NX"), "중복")
    _rejects(lambda: _krx("005930,005930"), "중복")


def test_같은_종목의_다른_접미사는_중복이_아니다():
    # 서로 다른 시장이므로 별개 구독이다. 다만 통합코드는 위에서 이미 막힌다.
    assert split_code("005930_NX") == ("005930", "NX")
    assert split_code("005930") == ("005930", None)


# ── 목록 근거 ───────────────────────────────────────────────────────────────

def test_목록_출처와_확인_시각이_없으면_거부한다():
    _rejects(lambda: SymbolListSource(origin="", verified_at="2026-09-17"), "출처")
    _rejects(lambda: SymbolListSource(origin="사용자 입력", verified_at="  "), "확인 시각")
    _rejects(lambda: build_plan(MODE_NXT, "005930_NX", source=None,
                                market_profile="nxt_aftermarket"), "출처")


def test_사용자_입력과_NXT_대상_확인은_다른_사실이다():
    # 사용자가 넣었다는 것이 NXT 거래 대상임을 확인했다는 뜻이 되면 안 된다.
    assert SOURCE.origin == "사용자 입력"
    assert SOURCE.nxt_eligibility_confirmed is False        # 기본값은 미확인
    confirmed = SymbolListSource(origin="사용자 입력", verified_at="2026-09-17T16:30:00+09:00",
                                 nxt_eligibility_confirmed=True, note="공시 화면 대조")
    assert confirmed.nxt_eligibility_confirmed is True
    # 계획 기록에 두 사실이 따로 남는다.
    payload = _nxt(source=confirmed).describe()
    assert payload["source"]["origin"] == "사용자 입력"
    assert payload["source"]["nxt_eligibility_confirmed"] is True


def test_소수_종목_원칙을_강제한다():
    many = ",".join(f"{i:06d}_NX" for i in range(MAX_CODES + 1))
    _rejects(lambda: _nxt(many), f"1~{MAX_CODES}")
    _rejects(lambda: _nxt([]), f"1~{MAX_CODES}")


# ── 접미사로 venue/coverage 를 올리지 않는다 ────────────────────────────────

def test_계획은_venue와_coverage를_미확인으로_들고_다닌다():
    payload = _nxt().describe()
    assert payload["venue_resolution"] == "unverified"
    assert payload["nxt_coverage"] == "unconfirmed"


def test_콜백_기록은_원문을_남기되_venue를_확정하지_않는다():
    plan = _nxt()
    row = record_callback(plan, subscription_code="005930_NX",
                          callback_code="005930_NX", real_type="주식체결")
    assert row["subscription_code"] == "005930_NX"      # 구독 코드 원문
    assert row["callback_code"] == "005930_NX"          # 콜백 코드 원문
    assert row["market_profile"] == "nxt_aftermarket"
    assert row["suffix_matches"] is True
    assert row["venue"] == "unknown"                     # 접미사가 일치해도 확정하지 않는다
    assert row["venue_resolution"] == "unverified"
    assert row["nxt_coverage"] == "unconfirmed"


def test_콜백_코드가_다르면_그_사실이_남는다():
    row = record_callback(_nxt(), subscription_code="005930_NX",
                          callback_code="005930", real_type="주식체결")
    assert row["suffix_matches"] is False
    assert row["venue"] == "unknown"


def test_알_수_없는_모드를_거부한다():
    _rejects(lambda: build_plan("야간", "005930", source=SOURCE, market_profile="krx_regular"),
             "지원하지 않는 구독 모드")


# ── 명령줄 입력 ─────────────────────────────────────────────────────────────

CLI_OK = dict(nxt_codes="005930_NX", list_origin="사용자 입력",
              list_verified_at="2026-09-17T16:30:00+09:00",
              market_profile="nxt_aftermarket", duration_seconds=60)


def test_명령줄에서_계획을_만든다():
    plan = plan_from_cli(**CLI_OK)
    assert plan.codes == ("005930_NX",)
    assert plan.source.origin == "사용자 입력"
    assert plan.market_profile == "nxt_aftermarket"


def test_목록_근거_없이는_명령줄에서도_거부한다():
    for missing in ("list_origin", "list_verified_at"):
        kwargs = dict(CLI_OK); kwargs[missing] = ""
        _rejects(lambda k=kwargs: plan_from_cli(**k), "목록 근거가 필요하다")


def test_확인_여부는_직접_밝힐_때만_참이다():
    assert plan_from_cli(**CLI_OK).source.nxt_eligibility_confirmed is False
    confirmed = plan_from_cli(**CLI_OK, nxt_eligibility_confirmed=True)
    assert confirmed.source.nxt_eligibility_confirmed is True


def test_명령줄도_제한_시간_범위를_먼저_확인한다():
    for bad in (0, 301, 3600, "60"):
        kwargs = dict(CLI_OK); kwargs["duration_seconds"] = bad
        _rejects(lambda k=kwargs: plan_from_cli(**k), "1~300초")


def test_명령줄에서도_접미사와_중복_규칙이_같다():
    for bad in ("005930", "005930_AL", "005930_NX,005930_NX"):
        kwargs = dict(CLI_OK); kwargs["nxt_codes"] = bad
        _rejects(lambda k=kwargs: plan_from_cli(**k))


def test_코드가_비면_거부한다():
    kwargs = dict(CLI_OK); kwargs["nxt_codes"] = "  "
    _rejects(lambda: plan_from_cli(**kwargs), "--nxt-codes 가 필요하다")
