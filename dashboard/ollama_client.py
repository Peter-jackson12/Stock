from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pandas as pd
import streamlit as st

OLLAMA_URL = "http://localhost:11434/api/chat"
DEFAULT_MODEL = "llama3.2:3b"


def _safe_table(df: pd.DataFrame | None, max_rows: int = 20) -> str:
    if df is None or df.empty:
        return "데이터 없음"
    view = df.head(max_rows).copy()
    for col in view.columns:
        if pd.api.types.is_datetime64_any_dtype(view[col]):
            view[col] = view[col].astype(str)
    return view.to_string(index=False)


def ask_ollama(
    question: str,
    page_name: str,
    context: str,
    history: list[dict[str, str]] | None = None,
    model: str = DEFAULT_MODEL,
    timeout: int = 90,
) -> str:
    """로컬 Ollama /api/chat 엔드포인트에 페이지 컨텍스트와 질문을 전달합니다."""
    system_prompt = f"""
당신은 주식 백테스트 결과를 해석하는 데이터 분석 보조 AI입니다.
현재 사용자가 보고 있는 페이지는 '{page_name}'입니다.
아래에 제공된 대시보드 데이터와 집계 결과만을 근거로 답하세요.
데이터에 없는 사실, 미래 주가, 실제 투자 수익을 추측하지 마세요.
투자 권유가 아니라 백테스트 결과 해석과 분석 보조에 집중하세요.
가능하면 숫자를 근거로 간결하고 이해하기 쉽게 설명하세요.
질문에 필요한 데이터가 컨텍스트에 없다면 '현재 화면 데이터만으로는 판단하기 어렵다'고 명확히 말하세요.

[현재 페이지 데이터]
{context}
""".strip()

    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history[-8:])
    messages.append({"role": "user", "content": question})

    payload = json.dumps({"model": model, "messages": messages, "stream": False}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        OLLAMA_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result: dict[str, Any] = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ConnectionError(
            "Ollama에 연결할 수 없습니다. Ollama가 실행 중인지 확인하고 `ollama run llama3.2:3b`를 한 번 실행해 주세요."
        ) from exc
    except TimeoutError as exc:
        raise TimeoutError("Ollama 응답 시간이 초과되었습니다. 더 작은 모델을 사용하거나 다시 질문해 주세요.") from exc

    return str(result.get("message", {}).get("content", "응답을 받지 못했습니다."))


def render_ai_chat(
    page_name: str,
    context: str,
    suggested_questions: list[str] | None = None,
) -> None:
    """각 페이지 하단에 접을 수 있는 Ollama 채팅 UI를 렌더링합니다."""
    key_base = page_name.lower().replace(" ", "_")
    history_key = f"ollama_history_{key_base}"
    model_key = f"ollama_model_{key_base}"

    if history_key not in st.session_state:
        st.session_state[history_key] = []
    if model_key not in st.session_state:
        st.session_state[model_key] = DEFAULT_MODEL

    st.divider()
    with st.expander("🤖 AI에게 이 페이지 분석 질문하기", expanded=False):
        top_left, top_right = st.columns([3, 1])
        with top_left:
            st.caption("현재 페이지의 필터·KPI·집계 결과를 Ollama에 함께 전달합니다.")
        with top_right:
            st.text_input("Ollama 모델", key=model_key, help="예: llama3.2:3b, qwen3:8b")

        if suggested_questions:
            st.markdown("**예시 질문**")
            qcols = st.columns(min(3, len(suggested_questions)))
            for idx, suggestion in enumerate(suggested_questions):
                if qcols[idx % len(qcols)].button(suggestion, key=f"{key_base}_suggest_{idx}", use_container_width=True):
                    st.session_state[f"pending_question_{key_base}"] = suggestion

        for message in st.session_state[history_key]:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        pending_key = f"pending_question_{key_base}"
        typed = st.chat_input("이 페이지 결과에 대해 질문하세요", key=f"chat_input_{key_base}")
        question = st.session_state.pop(pending_key, None) or typed

        if question:
            st.session_state[history_key].append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)

            with st.chat_message("assistant"):
                with st.spinner("Ollama가 현재 페이지를 분석하고 있습니다..."):
                    try:
                        history_for_model = [
                            m for m in st.session_state[history_key][:-1]
                            if m.get("role") in {"user", "assistant"}
                        ]
                        answer = ask_ollama(
                            question=question,
                            page_name=page_name,
                            context=context,
                            history=history_for_model,
                            model=st.session_state[model_key],
                        )
                    except Exception as exc:  # UI에서 연결 오류를 친절하게 표시
                        answer = f"⚠️ {exc}"
                    st.markdown(answer)
            st.session_state[history_key].append({"role": "assistant", "content": answer})

        if st.session_state[history_key]:
            if st.button("대화 내용 지우기", key=f"clear_{key_base}"):
                st.session_state[history_key] = []
                st.rerun()


def table_to_context(df: pd.DataFrame | None, max_rows: int = 20) -> str:
    return _safe_table(df, max_rows=max_rows)
