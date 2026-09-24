"""문서 경로/절·필수 의존 연결·원문 보존·인계 크기를 검사한다. 운영 데이터/네트워크 접근 없음.

현재 문서에서 쓰는 inline 링크와 ATX heading/명시적 HTML id를 검사한다.
범용 Markdown 검증기, 외부 URL 검사기, 코드/문서 의미 동치 검증기는 아니다.
동결 archive 본문은 당시 상대 경로를 유지하므로 탐색 검사 대신 blob 동일성을 검사한다.
필수 연결은 읽기 경로의 삭제를 잡는다. 에이전트가 실제로 읽었는지나 내용의 정확성은 인증하지 않는다.
"""
import hashlib
from pathlib import Path
import posixpath
import re
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAVIGATION_DOCS = (
    "README.md", "AGENTS.md", "HANDOFF.md", "BACKTEST_TODO.md",
    "TICK_RESEARCH_RUNBOOK.md", "docs/PIPELINE_MAP.md", "docs/archive/README.md",
    "CONTROL_TOWER.md", "docs/COLLECTION_RUNBOOK.md",
    "docs/COLLECTOR_TELEMETRY.md", "docs/COLLECTOR_TEARDOWN.md",
)
# 상세 시간표나 문구 대신 담당 문서/절과 실제 구현의 의존 연결을 고정한다.
REQUIRED_DEPENDENCIES = {
    "AGENTS.md": {
        "docs/COLLECTION_RUNBOOK.md#collection-decision",
        "docs/COLLECTION_RUNBOOK.md#collection-live-boundary",
    },
    "HANDOFF.md": {
        "docs/COLLECTION_RUNBOOK.md#collection-decision",
        "docs/COLLECTION_RUNBOOK.md#collection-live-boundary",
    },
    "README.md": {
        "docs/COLLECTION_RUNBOOK.md#collection-decision",
        "docs/COLLECTION_RUNBOOK.md#collection-coverage",
        "CONTROL_TOWER.md",
    },
    "CONTROL_TOWER.md": {"docs/COLLECTION_RUNBOOK.md"},
    "docs/COLLECTION_RUNBOOK.md": {
        "collector/kiwoom/market_sessions.py",
        "collector/kiwoom/session_monitor.py",
        "collector/kiwoom/subscription_plan.py",
        "collector/kiwoom/kiwoom_universe_logger.py",
        "collector/kiwoom/live_capture.py",
        "HANDOFF.md", "docs/PIPELINE_MAP.md#entrypoints",
        "docs/COLLECTOR_TELEMETRY.md", "docs/COLLECTOR_TEARDOWN.md",
        "TICK_RESEARCH_RUNBOOK.md",
    },
}
ARCHIVES = (
    ("docs/archive/HANDOFF_20260920_PRE_STRUCTURE_REVIEW.md",
     "fdb32bd15727d7122a9009c5f8923cac1f3a6ee6"),
    ("docs/archive/BACKTEST_TODO_20260920_PRE_STRUCTURE_REVIEW.md",
     "d7fe2cdadd9ea349a940b91be59093ef112b59c8"),
)
LINK = re.compile(r"(?<!!)\[[^\]\n]+\]\(([^\s)]+)\)")


def outside_fences(text):
    """실행 예시/인용 코드 안의 가짜 링크와 제목을 검사하지 않는다."""
    result, fence = [], None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            continue
        if fence is None:
            result.append(line)
    return "\n".join(result)


def anchors(text):
    text = outside_fences(text)
    found = set(re.findall(r'<a\s+(?:id|name)=["\']([^"\']+)["\']', text))
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.MULTILINE):
        title = re.sub(r"<[^>]+>", "", title).lower()
        slug = re.sub(r"[^\w\-\s]", "", title).replace(" ", "-")
        candidate, duplicate = slug, 0
        while candidate in found:
            duplicate += 1
            candidate = f"{slug}-{duplicate}"
        found.add(candidate)
    return found


def local_link_targets(relative, text):
    """문서 위치에 상대적인 내부 링크를 저장소 경로+절로 정규화한다. 파일/URL을 열지 않는다."""
    targets = set()
    for destination in LINK.findall(outside_fences(text)):
        url = urlsplit(destination.strip("<>"))
        if url.scheme or url.netloc:
            continue
        path = (posixpath.normpath(posixpath.join(posixpath.dirname(relative), unquote(url.path)))
                if url.path else relative)
        targets.add(path + ("#" + unquote(url.fragment) if url.fragment else ""))
    return targets


def missing_dependencies(relative, text, required):
    return set(required) - local_link_targets(relative, text)


def link_errors(root, relative):
    """고정된 저장소 내부 파일/절 대상만 읽는다."""
    root = root.resolve()
    source = root / relative
    errors = []
    for destination in LINK.findall(outside_fences(source.read_text(encoding="utf-8"))):
        url = urlsplit(destination.strip("<>"))
        if url.scheme or url.netloc:
            continue
        target = ((source.parent / unquote(url.path)).resolve() if url.path else source.resolve())
        if not target.is_relative_to(root):
            errors.append(f"{relative}: outside repository: {destination}")
        elif not target.exists():
            errors.append(f"{relative}: missing file: {destination}")
        elif url.fragment and target.suffix == ".md":
            fragment = unquote(url.fragment)
            if fragment not in anchors(target.read_text(encoding="utf-8")):
                errors.append(f"{relative}: missing anchor: {destination}")
    return errors


@pytest.mark.parametrize("relative", NAVIGATION_DOCS)
def test_active_document_links(relative):
    assert not (errors := link_errors(ROOT, relative)), "\n".join(errors)


@pytest.mark.parametrize("relative", REQUIRED_DEPENDENCIES)
def test_operational_decision_dependencies_remain_reachable(relative):
    missing = missing_dependencies(relative, (ROOT / relative).read_text(encoding="utf-8"),
                                   REQUIRED_DEPENDENCIES[relative])
    assert not missing, f"{relative}: 필수 의존 연결 누락: {sorted(missing)}"


@pytest.mark.parametrize("relative,expected_blob", ARCHIVES)
def test_frozen_document_blob_preserved(relative, expected_blob):
    # .gitattributes fixes Markdown to LF in both the Git blob and checkout.
    data = (ROOT / relative).read_bytes()
    header = f"blob {len(data)}\0".encode("ascii")
    assert hashlib.sha1(header + data).hexdigest() == expected_blob


def test_handoff_remains_current_and_bounded():
    data = (ROOT / "HANDOFF.md").read_bytes()
    assert len(data) <= 12 * 1024, "중요 조건을 지우지 말고 과거 이력을 보존본으로 분리하세요"
    text = data.decode("utf-8")
    assert text.startswith("# 현재 인계")
    assert "# 이전 인계" not in text
    assert "docs/archive/README.md" in text


def test_fenced_examples_are_not_navigation():
    text = "# 제목\n```text\n[예시](missing.md)\n# 가짜\n```\n[실제](real.md)\n"
    visible = outside_fences(text)
    assert LINK.findall(visible) == ["real.md"]
    assert "가짜" not in anchors(text)


def test_korean_duplicate_and_explicit_anchors():
    text = '# 같은 제목\n## 같은 제목\n<a id="entrypoints"></a>\n'
    assert anchors(text) == {"같은-제목", "같은-제목-1", "entrypoints"}


def test_missing_file_and_anchor_are_reported(tmp_path):
    (tmp_path / "index.md").write_text(
        "[정상](target.md#제목)\n[절 누락](target.md#없는-절)\n"
        "[파일 누락](missing.md)\n[외부](https://example.invalid/no-network)\n",
        encoding="utf-8")
    (tmp_path / "target.md").write_text("# 제목\n", encoding="utf-8")
    errors = link_errors(tmp_path, "index.md")
    assert len(errors) == 2
    assert any("missing anchor" in error for error in errors)
    assert any("missing file" in error for error in errors)


@pytest.mark.parametrize("replacement", [
    "담당 문서: `target.md#decision`\n",
    "```md\n[담당](target.md#decision)\n```\n",
    "[외부 사본](https://example.invalid/target.md#decision)\n",
])
def test_removed_required_edge_is_detected_even_when_target_exists(tmp_path, replacement):
    (tmp_path / "target.md").write_text('<a id="decision"></a>\n', encoding="utf-8")
    required = {"target.md#decision"}
    assert not missing_dependencies("index.md", "[담당](target.md#decision)\n", required)
    (tmp_path / "index.md").write_text(replacement, encoding="utf-8")
    # 기존 링크 검사만으로는 삭제된 연결을 탐지하지 못한다.
    assert not link_errors(tmp_path, "index.md")
    assert missing_dependencies("index.md", replacement, required) == required


def test_required_edges_normalize_relative_paths_and_fragments():
    text = "[코드](../collector/kiwoom/./market_sessions.py)\n[내부](#%EA%B2%B0%EC%A0%95)\n"
    assert local_link_targets("docs/runbook.md", text) == {
        "collector/kiwoom/market_sessions.py", "docs/runbook.md#결정",
    }
