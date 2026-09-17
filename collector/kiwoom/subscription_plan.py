"""구독 계획 — 어떤 코드를 어느 모드로 구독하는지, 그 목록이 어디서 왔는지 함께 들고 다닌다.

운영 수집기는 종목코드를 여섯 자리 숫자로만 받는다. 그 검증을 전역으로 풀면 정규장 경로까지
접미사를 받게 되므로, **명시적 NXT 모드에서만** 접미사를 허용한다. 기본 모드의 규칙은
지금과 같다.

이 모듈이 굳이 목록의 출처와 확인 시각을 필수로 받는 이유가 있다. 개발가이드상 거래소는
종목코드 접미사로 갈리지만, **어떤 종목이 NXT 거래 대상인지**는 조회로 받는 여섯 자리
목록에 들어 있지 않다. 그 목록을 NXT 대상 목록으로 간주하면 근거 없는 확장이 된다.
그래서 당분간은 사람이 확인해 넣은 소수 종목으로 시작하고, 그 사실을 기록에 남긴다.

여기서 두 가지를 반드시 구분한다.

    사용자가 입력했다          — 이 목록이 어디서 왔는지에 대한 사실
    NXT 거래 대상임을 확인했다  — 공식 수단으로 확인했는가에 대한 별개의 사실

전자가 참이라고 후자가 참이 되지 않는다. 기본값은 미확인이다.

그리고 접미사는 **라우팅 근거일 뿐이다.** 구독 코드와 콜백 코드를 원문 그대로 기록하되,
접미사가 붙었다는 사실만으로 개별 체결의 거래소(venue)나 NXT 결손 가드의 수신 완전성
(coverage)을 확정하지 않는다. 그 둘은 별도 증명이 필요하다.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

#: 기존 정규장 수집. 여섯 자리 코드만 받는다.
MODE_KRX_REGULAR = "krx_regular"
#: NXT 수집. `_NX` 접미사를 요구한다.
MODE_NXT = "nxt"
MODES = (MODE_KRX_REGULAR, MODE_NXT)

SUFFIX_NXT = "NX"
SUFFIX_UNIFIED = "AL"

#: 소수 종목으로 시작한다는 원칙을 강제한다. 전 종목 확장은 별도 검증 뒤의 일이다.
MAX_CODES = 10


@dataclass(frozen=True)
class SymbolListSource:
    """목록이 어디서 왔고 언제 확인됐는지. 둘 다 비워 둘 수 없다."""
    origin: str
    verified_at: str
    #: 공식 수단으로 NXT 거래 대상임을 확인했는가. 사용자가 입력했다는 사실과 별개다.
    nxt_eligibility_confirmed: bool = False
    note: str = ""

    def __post_init__(self):
        if not str(self.origin).strip():
            raise ValueError("목록 출처가 필요하다 (예: 사용자 입력, 공시 화면)")
        if not str(self.verified_at).strip():
            raise ValueError("목록 확인 시각이 필요하다")


@dataclass(frozen=True)
class SubscriptionPlan:
    mode: str
    codes: tuple
    source: SymbolListSource
    market_profile: str
    #: 접미사만으로 올리지 않는 두 값. 기록에 항상 함께 남긴다.
    venue_resolution: str = "unverified"
    nxt_coverage: str = "unconfirmed"

    def describe(self):
        payload = dict(mode=self.mode, codes=list(self.codes),
                       market_profile=self.market_profile,
                       source=asdict(self.source),
                       venue_resolution=self.venue_resolution,
                       nxt_coverage=self.nxt_coverage,
                       note="접미사는 라우팅 근거이며 개별 체결 venue 와 NXT coverage 는 미확인이다")
        return payload


def split_code(code):
    """(여섯 자리, 접미사 또는 None). 형식이 어긋나면 ValueError."""
    if not isinstance(code, str) or not code.strip():
        raise ValueError("종목코드는 비어 있지 않은 문자열이어야 한다")
    base, _, suffix = code.strip().partition("_")
    if len(base) != 6 or not base.isdigit():
        raise ValueError(f"종목코드 앞부분은 여섯 자리 숫자여야 한다: {code!r}")
    if not suffix:
        return base, None
    upper = suffix.upper()
    if upper not in (SUFFIX_NXT, SUFFIX_UNIFIED):
        raise ValueError(f"알 수 없는 시장 접미사: {code!r} (허용: _NX, _AL)")
    return base, upper


def build_plan(mode, codes, *, source, market_profile):
    """구독 계획을 검증해 만든다. 규칙을 어기면 ValueError.

    기본 모드에서는 접미사를 받지 않는다 — 검증을 전역으로 풀지 않기 위해서다.
    """
    if mode not in MODES:
        raise ValueError(f"지원하지 않는 구독 모드: {mode!r} (가능: {', '.join(MODES)})")
    if not isinstance(source, SymbolListSource):
        raise ValueError("목록 출처와 확인 시각이 필요하다")
    if isinstance(codes, str):
        codes = [c for c in codes.split(",") if c.strip()]
    codes = [str(c).strip() for c in codes]
    if not 1 <= len(codes) <= MAX_CODES:
        raise ValueError(f"종목은 1~{MAX_CODES}개여야 한다 (받은 개수: {len(codes)})")

    seen = set()
    normalized = []
    for code in codes:
        base, suffix = split_code(code)
        if mode == MODE_KRX_REGULAR and suffix is not None:
            raise ValueError(
                f"정규장 모드는 접미사를 받지 않는다: {code!r}. NXT 수집은 모드를 명시해야 한다")
        if mode == MODE_NXT:
            if suffix is None:
                raise ValueError(f"NXT 모드는 _NX 접미사가 필요하다: {code!r}")
            if suffix == SUFFIX_UNIFIED:
                raise ValueError(
                    f"통합(_AL) 코드는 체결 거래소를 단정할 수 없어 운영 구독에 쓰지 않는다: {code!r}. "
                    "별도 비교 검증에만 사용한다")
        key = (base, suffix)
        if key in seen:
            raise ValueError(f"중복된 종목코드: {code!r}")
        seen.add(key)
        normalized.append(f"{base}_{suffix}" if suffix else base)

    return SubscriptionPlan(mode=mode, codes=tuple(normalized), source=source,
                            market_profile=market_profile)


def record_callback(plan, *, subscription_code, callback_code, real_type):
    """구독 코드 원문과 콜백 코드를 그대로 남긴다. 접미사로 venue 를 확정하지 않는다."""
    return dict(subscription_code=subscription_code, callback_code=callback_code,
                real_type=real_type, market_profile=plan.market_profile, mode=plan.mode,
                suffix_matches=(subscription_code == callback_code),
                venue="unknown", venue_resolution=plan.venue_resolution,
                nxt_coverage=plan.nxt_coverage)
