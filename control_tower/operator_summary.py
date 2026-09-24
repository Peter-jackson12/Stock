"""운영 화면용 안전한 상태 요약. 저장 근거를 현재 생존/품질 판정으로 승격하지 않는다."""
from __future__ import annotations


_LABELS = {
    "recent": "최근",
    "stale": "오래됨",
    "clock_ahead": "시각 확인",
    "no_heartbeat": "없음",
    "unavailable": "없음",
}


def _label(status) -> str:
    return _LABELS.get(status, "미확인")


def summarize_operator_state(observation: dict, raw: dict) -> dict:
    """Bounded reader 결과를 표시용으로 축약한다.

    이 함수는 프로세스·원본 DB·시장 상태를 조회하지 않는다. recent/closed 같은 저장 근거도
    현재 collector 생존이나 데이터 품질 인증으로 바꾸지 않는다.
    """
    heartbeat_status = observation.get("status", "unavailable")
    raw_status = raw.get("status", "unavailable")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    producer_state = snapshot.get("state")
    if not isinstance(producer_state, str):
        producer_state = None

    if producer_state in {"failed", "interrupted"}:
        tone = "error"
        headline = "오류/중단 생산자 기록이 있습니다"
        guidance = "현재 실행 여부를 추정하지 말고 아래 오류·세션 근거를 먼저 확인하세요."
    elif "clock_ahead" in {heartbeat_status, raw_status}:
        tone = "warning"
        headline = "저장 근거의 시각 확인이 필요합니다"
        guidance = "PC 시각과 근거 시각을 확인하기 전에는 상태를 현재 사실로 해석하지 마세요."
    elif "stale" in {heartbeat_status, raw_status}:
        tone = "warning"
        headline = "저장된 근거가 오래되었습니다"
        guidance = "현재 수집기가 실행 중인지 중지됐는지는 미확인입니다. 자동 재시작하지 않습니다."
    elif "recent" in {heartbeat_status, raw_status}:
        tone = "info"
        headline = "최근 저장 근거가 있습니다"
        guidance = "최근 기록은 프로세스 생존·피드 정상·데이터 품질 인증이 아닙니다."
    else:
        tone = "info"
        headline = "현재 읽을 수 있는 수집 근거가 부족합니다"
        guidance = "수집 중지로 단정하지 말고 필요하면 아래 세션·수집 경로에서 별도 확인하세요."

    return {
        "raw_evidence": _label(raw_status),
        "heartbeat_evidence": _label(heartbeat_status),
        "collector_now": "미확인",
        "producer_state": producer_state,
        "tone": tone,
        "headline": headline,
        "guidance": guidance,
        "execution_approved": False,
        "data_quality": "미확인",
    }
