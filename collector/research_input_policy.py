"""알려진 진단 raw의 연구 사용만 거부한다. 일반 scope의 적격성 인증은 아니다."""

FID_READ_DIAGNOSTIC_SCOPE = "kiwoom_universe_fid_read_diagnostic"


def research_exclusion_reason(manifest):
    """파일명/외부 sidecar가 아닌 실제 raw manifest의 scope를 판정한다."""
    if isinstance(manifest, dict) and manifest.get("feed_scope") == FID_READ_DIAGNOSTIC_SCOPE:
        return "diagnostic feed_scope is excluded from research: " + FID_READ_DIAGNOSTIC_SCOPE
    return None


def require_research_input(manifest):
    reason = research_exclusion_reason(manifest)
    if reason is not None:
        raise ValueError(reason)
