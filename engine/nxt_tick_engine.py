"""
대체거래소(NXT) 과열 필터 및 3대 트레일링 컷 비교 틱 이벤트 백테스트 엔진
- NXT 프리마켓(08:00~08:50) 과열 감지 및 09:00 설거지 음봉 방어
- 당일 시가(Open) 및 마이크로 고점 돌파 + 호가잔량 불균형(OBI) 진입
- 1초 지연 체결(1s Latency) 및 0.20% 세금/수수료 엄격 반영
- 3대 청산 규칙(고정 / 2틱 반락 트레일링 / 본전보존 계단식 트레일링) 동시 비교
"""

import argparse
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
from typing import Dict, List, Any
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import DATA_DIR

# 🆕 Phase A — 이 엔진은 지금까지 결과를 stdout 에만 찍고 저장하지 않았다.
# 화면에 찍힌 숫자는 다른 엔진의 결과와 나란히 놓을 수 없다. 런 스토어에 저장하면서
# 두 엔진(bar / tick)의 결과가 처음으로 같은 스키마 위에 놓인다.
from core.contracts import Trade
from core.runstore import (
    RunManifest,
    RunStore,
    current_git_sha,
    hash_params,
    make_run_id,
)

# 🆕 §3.6.1 — 거시 피처 null 정책. 값 자체는 아래 PARAMS 에 있고, 의미와 판정 로직은
# strategies/macro_filter.py 에 있다 (임계값 비교는 L3 의 일이다 — §2).
from strategies.macro_filter import ON_MISSING_REJECT
from engine.nxt_session import (
    NXT_COVERAGE_UNCONFIRMED,
    UNVERIFIED as NXT_UNVERIFIED,
    VENUE_UNVERIFIED,
    classify_premarket,
    require_verdict,
)

RAW_DIR = DATA_DIR / "raw_ticks"

STRATEGY_ID = "nxt_breakout"
STRATEGY_VERSION = "1.2.0"        # strategies/nxt_breakout/params/default.yaml 과 같은 값
FEATURE_SET_VERSION = "none"      # 아직 피처 스토어(L2)가 없다. Phase B 에서 "fs_v1"

# 청산 규칙 id. 결과의 exit_rule 컬럼에 들어가고, 이 컬럼으로 3대 컷을 나란히 비교한다.
# strategies/nxt_breakout/params/default.yaml 의 exits[].id 와 같은 이름을 쓴다.
EXIT_RULE_IDS = {
    "A_Fixed": "fixed",
    "B_2TickTrail": "tick_trail",
    "C_StepTrail": "step_trail",
}

# ---------------------------------------------------------------------------
# 전략 파라미터
#
# 함수 본문에 흩어져 있던 매직넘버를 한 곳으로 모았다. 값은 그대로다 — 거동은
# 바뀌지 않는다. 모은 이유는 두 가지다.
#   1) 이 값들이 RunManifest.param_hash 로 들어가 run_id 를 결정한다. 로직이 보는
#      숫자와 매니페스트에 적히는 숫자가 다르면 run_id 가 거짓말을 하게 된다.
#   2) Phase C 에서 params/default.yaml 로 그대로 들어낼 수 있는 형태다.
# ---------------------------------------------------------------------------
PARAMS: Dict[str, Any] = {
    "entry": {
        "spread_max_pct": 0.0025,       # 호가 스프레드 상한
        "buy_ratio_min": 0.75,          # 최근 N틱 시장가 매수 비중 하한
        "obi_min_ratio": 1.2,           # 매수1~3호가 잔량 / 매도1~3호가 잔량 하한
        "min_vol_15t": 30,              # 최근 N틱 최소 누적 거래량
        "recent_ticks": 15,             # 미시 수급 관측 틱 수
        "breakout_window_sec": 60,      # 마이크로 고점 추적 윈도우
        "session_start_sec": 32400,     # 09:00:00 정규장 개장
    },
    "nxt_overheat": {
        "window": [28800, 31800],       # 08:00:00 ~ 08:50:00 프리마켓 관측 구간
        "gain_threshold": 0.05,         # +5% 이상 상승 &
        "volume_threshold": 50000,      # 5만 주 이상 -> 정규장 돌파 매수 금지
    },
    # 거시 필터 — 아직 켜지 않았지만 **null 정책은 지금 확정해 둔다** (§3.6.1).
    # 정책 없이 enabled 를 켜면 커버리지 1.5% 인 유니버스가 조용히 사라지거나
    # 필터가 무력화된 채 켜져 있다고 착각하게 된다. 값은 여기 있어야
    # hash_params() 를 거쳐 런 매니페스트에 기록된다.
    # 의미와 선택지는 strategies/macro_filter.py 를 볼 것.
    "macro": {
        "enabled": False,
        "on_missing": ON_MISSING_REJECT,
        "mkt_cap_min_eok": 500,
        "mkt_cap_max_eok": 30000,
        "float_ratio_max_pct": 40.0,
        "ytd_tradamt_min_eok": 100,
    },
    "exits": {
        "fixed": {"take_profit_pct": 0.008, "stop_loss_pct": -0.005},
        "tick_trail": {"trail_ticks": 2, "stop_loss_pct": -0.005},
        "step_trail": {
            "arm_threshold_pct": 0.004,     # +0.4% 도달 시 손절선을 본전+1틱으로 상향
            "breakeven_offset_ticks": 1,
            "trail_ticks": 2,
            "stop_loss_pct": -0.005,
        },
    },
    "fee_rate": 0.0020,                 # 세금 + 수수료 (0.20%)
}


def to_seconds(t_str: str) -> int:
    """HHMMSS 문자열을 일 단위 누적 초(second)로 변환"""
    t = str(t_str).zfill(6)
    return int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])


def get_tick_size(price: float) -> int:
    """대한민국 주식 호가단위 (2023년 개편 표준)"""
    p = float(price)
    if p < 2000:
        return 1
    elif p < 5000:
        return 5
    elif p < 20000:
        return 10
    elif p < 50000:
        return 50
    elif p < 200000:
        return 100
    elif p < 500000:
        return 500
    else:
        return 1000


class NextradeTickEngine:
    def __init__(self, date_str: str, target_code: str, runs_root=None,
                 venue_resolution=VENUE_UNVERIFIED,
                 nxt_coverage=NXT_COVERAGE_UNCONFIRMED, allow_unverified_nxt=False):
        self.date_str = date_str
        self.code = target_code
        # raw-v1 원본에는 거래소가 없고 raw-v2 도 venue="unknown" 으로 보존한다.
        # 확인 경로가 생기기 전까지 기본값은 '미확인' 이다.
        self.venue_resolution = venue_resolution
        # 거래소를 구분할 수 있다는 것과 그 구간에 NXT 를 받고 있었다는 것은 별개다.
        # 현재 수집기는 6자리(KRX) 코드만 등록하므로 기본값은 '미확인' 이다.
        self.nxt_coverage = nxt_coverage
        self.allow_unverified_nxt = allow_unverified_nxt
        self.db_path = RAW_DIR / f"{date_str}_raw.db"

        if not self.db_path.exists():
            raise FileNotFoundError(f"데이터베이스 파일이 없습니다: {self.db_path}")

        # 🆕 Phase A — 결과 저장
        self.run_store = RunStore(runs_root)
        self.run_id: str = ""
        self.storage_key: str = ""

    def load_and_preprocess(self) -> List[Dict[str, Any]]:
        """체결과 10호가를 시간순으로 정합하여 틱 단위 이벤트 큐 생성"""
        print(f"🔍 [{self.code}] 원천 틱/호가 데이터 로딩 중...")
        conn = sqlite3.connect(self.db_path)
        trades = pd.read_sql_query(
            f"SELECT * FROM raw_trades WHERE code='{self.code}' ORDER BY rowid ASC",
            conn
        )
        quotes = pd.read_sql_query(
            f"SELECT * FROM raw_quotes WHERE code='{self.code}' ORDER BY rowid ASC",
            conn
        )
        conn.close()

        if trades.empty:
            print("⚠️ 체결 데이터가 없습니다.")
            return []

        print(f"  - 총 체결 틱: {len(trades):,}건 | 총 호가 변동: {len(quotes):,}건")

        # 호가 파싱
        quotes["ask_p1"] = quotes["offer_p"].apply(lambda x: float(x.split(",")[0]) if x else 0.0)
        quotes["bid_p1"] = quotes["bid_p"].apply(lambda x: float(x.split(",")[0]) if x else 0.0)
        
        # 1~3호가 총 잔량 계산 (호가 불균형 OBI 측정용)
        quotes["ask_v_top3"] = quotes["offer_v"].apply(
            lambda x: sum([int(v) for v in x.split(",")[:3]]) if x else 0
        )
        quotes["bid_v_top3"] = quotes["bid_v"].apply(
            lambda x: sum([int(v) for v in x.split(",")[:3]]) if x else 0
        )

        events = []
        q_idx = 0
        cur_q = {
            "ask_p1": 0.0, "bid_p1": 0.0,
            "ask_v_top3": 0, "bid_v_top3": 0
        }

        for _, t in trades.iterrows():
            t_time = t["t_time"]
            while q_idx < len(quotes) and quotes.iloc[q_idx]["q_time"] <= t_time:
                row_q = quotes.iloc[q_idx]
                cur_q = {
                    "ask_p1": row_q["ask_p1"],
                    "bid_p1": row_q["bid_p1"],
                    "ask_v_top3": row_q["ask_v_top3"],
                    "bid_v_top3": row_q["bid_v_top3"]
                }
                q_idx += 1

            price = float(t["price"])
            ask_p = cur_q["ask_p1"] if cur_q["ask_p1"] > 0 else price
            bid_p = cur_q["bid_p1"] if cur_q["bid_p1"] > 0 else price

            # 매수 판정 (호가 비교 결합)
            is_buy = 1 if (t["is_buy"] == 1 or (ask_p > 0 and price >= ask_p)) else 0

            events.append({
                "time": t_time,
                "sec": to_seconds(t_time),
                "price": price,
                "vol": int(t["vol"]),
                "is_buy": is_buy,
                "ask_p1": ask_p,
                "bid_p1": bid_p,
                "ask_v_top3": cur_q["ask_v_top3"],
                "bid_v_top3": cur_q["bid_v_top3"]
            })

        return events

    def run_strategy(self, latency_sec: int = 1, cooldown_sec: int = 10):
        events = self.load_and_preprocess()
        if not events:
            return None

        p_entry = PARAMS["entry"]
        p_nxt = PARAMS["nxt_overheat"]
        p_exit = PARAMS["exits"]

        # 🆕 Phase A — 런 식별자를 루프 전에 확정한다 (Trade 가 run_id 를 들고 있어야 한다).
        # 대상 종목도 파라미터에 포함한다. 종목이 바뀌면 다른 런이어야 하기 때문이다.
        run_params = dict(PARAMS)
        run_params["execution"] = {"latency_sec": latency_sec, "cooldown_sec": cooldown_sec}
        run_params["universe"] = [self.code]
        self.run_id = make_run_id(
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            param_hash=hash_params(run_params),
            feature_set_version=FEATURE_SET_VERSION,
            date_range=(self.date_str, self.date_str),
            git_sha=current_git_sha(),
        )

        # =======================================================
        # 1. NXT(08:00~08:50) 프리마켓 분석 및 과열 여부 판정
        # =======================================================
        # 시간대는 거래소를 확정하지 못한다. 08:00~08:50 안에는 KRX 장전
        # 시간외종가(08:30~08:40) 체결 등 NXT 가 아닌 이벤트가 섞일 수 있다.
        # 판정은 engine/nxt_session.py 가 하고, 미확인이면 계산하지 않는다.
        nxt_verdict = require_verdict(
            classify_premarket(
                events,
                window=tuple(p_nxt["window"]),
                gain_threshold=p_nxt["gain_threshold"],
                volume_threshold=p_nxt["volume_threshold"],
                venue_resolution=self.venue_resolution,
                nxt_coverage=self.nxt_coverage,
            ),
            allow_unverified=self.allow_unverified_nxt,
        )
        # None 은 '미확인' 이다. False(=확인된 미과열) 와 다르다.
        is_nxt_exhausted = nxt_verdict.exhausted
        if nxt_verdict.status == NXT_UNVERIFIED:
            print(f"⚠️ [NXT 미검증] {nxt_verdict.reason} — 과열 필터를 적용하지 않은 결과다")
        elif is_nxt_exhausted:
            print(f"🚨 [NXT 과열 감지] {nxt_verdict.reason} -> 정규장 돌파 매수 제한 발동!")

        # 09:00 정규장 시가 기준선 설정
        krx_trades = [e for e in events if e["sec"] >= p_entry["session_start_sec"]]  # 09:00:00 이후
        krx_open = krx_trades[0]["price"] if krx_trades else events[0]["price"]

        print(f"🎯 [시가 기준선] 당일 시가: {krx_open:,.0f}원 | 체결 지연 페널티: {latency_sec}초")

        # =======================================================
        # 2. 3대 청산 규칙 동시 시뮬레이션 구조
        # =======================================================
        FEE_RATE = PARAMS["fee_rate"]  # 세금 + 수수료

        # 각 청산 룰별 상태 관리
        # (excursions / entry_meta 는 Phase A 에서 추가된 결과 기록용 필드다.
        #  history 는 손대지 않는다 — 아래 성적표 출력이 history 를 그대로 찍기 때문.)
        rules = {
            "A_Fixed": {"name": "Rule A (고정익절 +0.8% / 손절 -0.5%)", "pos": 0, "entry_p": 0.0, "entry_t": "", "peak_p": 0.0, "history": [], "trough_p": 0.0, "entry_meta": {}, "excursions": []},
            "B_2TickTrail": {"name": "Rule B (2틱 반락 트레일링 컷)", "pos": 0, "entry_p": 0.0, "entry_t": "", "peak_p": 0.0, "history": [], "trough_p": 0.0, "entry_meta": {}, "excursions": []},
            "C_StepTrail": {"name": "Rule C (본전보존 계단식 트레일링 컷)", "pos": 0, "entry_p": 0.0, "entry_t": "", "peak_p": 0.0, "history": [], "trough_p": 0.0, "entry_meta": {}, "excursions": []}
        }

        pending_buy = False
        target_buy_sec = 0
        cooldown_until_sec = 0
        pending_meta: Dict[str, Any] = {}     # 진입 시그널 발생 시점의 피처 스냅샷

        rolling_60s_ticks = []
        recent_15_ticks = []

        for idx, tick in enumerate(events):
            cur_sec = tick["sec"]
            cur_p = tick["price"]
            cur_bid = tick["bid_p1"]

            # 마이크로 1분봉(60초) 고가 추적용 큐
            rolling_60s_ticks.append(tick)
            while rolling_60s_ticks and (cur_sec - rolling_60s_ticks[0]["sec"]) > p_entry["breakout_window_sec"]:
                rolling_60s_ticks.pop(0)

            recent_15_ticks.append(tick)
            if len(recent_15_ticks) > p_entry["recent_ticks"]:
                recent_15_ticks.pop(0)

            # ---------------------------------------------------
            # [1단계: 지연 체결 처리] (1~2초 렉 이후 실제 매도1호가로 체결)
            # ---------------------------------------------------
            if pending_buy and cur_sec >= target_buy_sec:
                fill_price = tick["ask_p1"]
                for r_key in rules:
                    if rules[r_key]["pos"] == 0:
                        rules[r_key]["pos"] = 1
                        rules[r_key]["entry_p"] = fill_price
                        rules[r_key]["entry_t"] = tick["time"]
                        rules[r_key]["peak_p"] = fill_price
                        rules[r_key]["trough_p"] = fill_price        # MAE 추적 시작점
                        rules[r_key]["entry_meta"] = dict(pending_meta)
                pending_buy = False

            # ---------------------------------------------------
            # [2단계: 3대 청산 규칙 동시 감시]
            # ---------------------------------------------------
            for r_key, r in rules.items():
                if r["pos"] == 1:
                    r["peak_p"] = max(r["peak_p"], cur_p)
                    r["trough_p"] = min(r["trough_p"], cur_p)   # 최대 역행폭(MAE) 추적
                    tick_size_peak = get_tick_size(r["peak_p"])
                    raw_pnl = (cur_bid - r["entry_p"]) / r["entry_p"]
                    net_pnl = raw_pnl - FEE_RATE

                    exit_msg = None

                    # --- Rule A: 고정 손익절 ---
                    if r_key == "A_Fixed":
                        if raw_pnl <= p_exit["fixed"]["stop_loss_pct"]:
                            exit_msg = "손절 (-0.5%)"
                        elif raw_pnl >= p_exit["fixed"]["take_profit_pct"]:
                            exit_msg = "익절 (+0.8%)"

                    # --- Rule B: 2틱 반락 트레일링 컷 ---
                    elif r_key == "B_2TickTrail":
                        if raw_pnl <= p_exit["tick_trail"]["stop_loss_pct"]:
                            exit_msg = "손절 (-0.5%)"
                        # 최고가 대비 2틱 밀리면 청산
                        elif cur_bid <= (r["peak_p"] - tick_size_peak * p_exit["tick_trail"]["trail_ticks"]):
                            exit_msg = f"2틱 반락 컷 (고점: {r['peak_p']:,.0f})"

                    # --- Rule C: 본전보존 계단식 트레일링 컷 ---
                    elif r_key == "C_StepTrail":
                        p_step = p_exit["step_trail"]
                        entry_tick_size = get_tick_size(r["entry_p"])
                        # 수수료 방어선 (진입가+1틱)
                        breakeven_p = r["entry_p"] + entry_tick_size * p_step["breakeven_offset_ticks"]

                        # +0.4% 이상 상승 시 손절선을 본전+1틱으로 상향
                        if r["peak_p"] >= r["entry_p"] * (1 + p_step["arm_threshold_pct"]):
                            if cur_bid <= breakeven_p:
                                exit_msg = "본전 방어 컷"
                            elif cur_bid <= (r["peak_p"] - tick_size_peak * p_step["trail_ticks"]):
                                exit_msg = "계단식 트레일 익절"
                        else:
                            if raw_pnl <= p_step["stop_loss_pct"]:
                                exit_msg = "원칙 손절 (-0.5%)"

                    # 청산 실행
                    if exit_msg:
                        r["history"].append({
                            "entry_t": r["entry_t"], "entry_p": r["entry_p"],
                            "exit_t": tick["time"], "exit_p": cur_bid,
                            "net_pnl_pct": round(net_pnl * 100, 3), "msg": exit_msg
                        })
                        # history 와 같은 순서로 쌓인다 (성적표 출력에는 영향 없음)
                        r["excursions"].append({
                            "peak_p": r["peak_p"],
                            "trough_p": r["trough_p"],
                            "entry_meta": r["entry_meta"],
                        })
                        r["pos"] = 0
                        cooldown_until_sec = cur_sec + cooldown_sec

            # ---------------------------------------------------
            # [3단계: 시가/고가 돌파 + OBI 호가 진입 감시]
            # ---------------------------------------------------
            all_idle = all(rules[k]["pos"] == 0 for k in rules)
            if all_idle and not pending_buy and cur_sec >= cooldown_until_sec:
                # NXT 과열 필터에 걸린 종목은 09:00 정규장 돌파 진입 금지!
                if is_nxt_exhausted is True and cur_sec >= p_entry["session_start_sec"]:
                    continue

                # 1) 호가 스프레드 검증 (1틱 초과 시 진입 금지)
                spread_pct = (tick["ask_p1"] - tick["bid_p1"]) / tick["bid_p1"] if tick["bid_p1"] > 0 else 1.0
                if spread_pct > p_entry["spread_max_pct"]:
                    continue

                # 2) 거시 기준선 돌파: 당일 시가(Open) 돌파 & 60초 마이크로 최고가 갱신
                micro_high_60s = max([t["price"] for t in rolling_60s_ticks[:-1]]) if len(rolling_60s_ticks) > 1 else cur_p
                is_breakout = (cur_p >= krx_open) and (cur_p >= micro_high_60s)

                # 3) 미시 틱 수급: 최근 15틱 중 75% 이상 시장가 매수
                tot_vol_15 = sum(t["vol"] for t in recent_15_ticks)
                buy_vol_15 = sum(t["vol"] for t in recent_15_ticks if t["is_buy"] == 1)
                buy_ratio = buy_vol_15 / tot_vol_15 if tot_vol_15 > 0 else 0.0

                # 4) 호가잔량 불균형 (OBI): 매수 1~3호가 잔량 > 매도 1~3호가 잔량 * 1.2배
                is_obi_bullish = tick["bid_v_top3"] > (tick["ask_v_top3"] * p_entry["obi_min_ratio"])

                # 모든 조건을 만족할 때만 1초 지연 예약 진입!
                if (is_breakout and buy_ratio >= p_entry["buy_ratio_min"]
                        and is_obi_bullish and tot_vol_15 >= p_entry["min_vol_15t"]):
                    pending_buy = True
                    target_buy_sec = cur_sec + latency_sec
                    # 🆕 Phase A — 진입 판단 근거를 그대로 남긴다.
                    # 나중에 "왜 이 거래가 졌는가"를 원천 DB 재조회 없이 답하기 위함.
                    # (ARCHITECTURE_V2.md §6.2 signal_meta)
                    pending_meta = {
                        "signal_time": tick["time"],
                        "signal_price": cur_p,
                        "spread_pct": round(spread_pct, 6),
                        "buy_ratio_15t": round(buy_ratio, 4),
                        "obi_top3": round(tick["bid_v_top3"] / tick["ask_v_top3"], 4) if tick["ask_v_top3"] else None,
                        "vol_15t": tot_vol_15,
                        "micro_high_60s": micro_high_60s,
                        "krx_open": krx_open,
                        "nxt_exhausted": is_nxt_exhausted,
                        "nxt_status": nxt_verdict.status,
                        "nxt_reason": nxt_verdict.reason,
                    }

        # =======================================================
        # 3. 3대 청산 규칙 성능 비교 리포트 출력
        # =======================================================
        print("\n" + "=" * 70)
        print(f"📊 [{self.code}] 3대 트레일링 컷 동시 시뮬레이션 성적표")
        print("=" * 70)

        summary_rows = []
        for r_key, r in rules.items():
            df_h = pd.DataFrame(r["history"])
            if df_h.empty:
                summary_rows.append({
                    "청산 룰": r["name"], "총 거래": 0, "승률": "0.0%", "평균 PnL": "0.0%", "누적 PnL": "0.0%"
                })
                continue

            wins = df_h[df_h["net_pnl_pct"] > 0]
            win_rate = len(wins) / len(df_h) * 100
            avg_pnl = df_h["net_pnl_pct"].mean()
            tot_pnl = df_h["net_pnl_pct"].sum()

            summary_rows.append({
                "청산 룰": r["name"],
                "총 거래": f"{len(df_h)}회",
                "승률": f"{win_rate:.1f}% ({len(wins)}승 {len(df_h)-len(wins)}패)",
                "평균 PnL": f"{avg_pnl:+.3f}%",
                "누적 PnL": f"{tot_pnl:+.3f}%"
            })

        print(pd.DataFrame(summary_rows).to_string(index=False))
        print("=" * 70)

        # 승자 룰의 상세 거래 내역 출력
        for r_key in ["B_2TickTrail", "C_StepTrail"]:
            df_best = pd.DataFrame(rules[r_key]["history"])
            if not df_best.empty:
                print(f"\n[🔍 {rules[r_key]['name']} 상세 거래 기록]")
                print(df_best.to_string(index=False))
                break

        # =======================================================
        # 4. 🆕 Phase A — 표준 Trade 레코드로 저장
        #    이 엔진이 결과를 저장하는 것은 이번이 처음이다. 3대 컷의 거래가
        #    exit_rule 컬럼으로 구분되어 한 런에 함께 들어간다.
        # =======================================================
        trades = self._to_trades(rules)
        manifest = RunManifest(
            run_id=self.run_id,
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            param_variant="default",
            param_hash=hash_params(run_params),
            feature_set_version=FEATURE_SET_VERSION,
            engine="tick",
            mode="backtest",
            date_range=(self.date_str, self.date_str),
            universe_size=1,
            git_sha=current_git_sha(),
            params=run_params,
            notes=f"종목 {self.code} · 3대 청산 컷 동시 비교",
        )
        self.storage_key = self.run_store.save(manifest, trades)
        run_path = self.run_store.run_dir(self.storage_key)
        print(f"\n💾 런 저장 완료: run_id={self.run_id} · {len(trades)}건 ➔ {run_path}")
        return self.storage_key

    def _to_trades(self, rules: Dict[str, Dict[str, Any]]) -> List[Trade]:
        """
        rules[*]["history"] -> 표준 Trade 레코드 (ARCHITECTURE_V2.md §6.2)

            entry_t / entry_p      -> entry_time / entry_price
            exit_t  / exit_p       -> exit_time  / exit_price   (매수1호가 기준 청산)
            net_pnl_pct            -> net_pnl_pct  (수수료 차감 후, 화면 성적표와 같은 값)
            msg                    -> exit_reason
            r_key                  -> exit_rule    (fixed / tick_trail / step_trail)

        gross_pnl_pct 는 체결가 기준 원본 손익, fee_pct 는 세금+수수료(%)다.
        mae/mfe 는 보유 중 추적한 최저·최고 체결가 기준 역행·순행폭이다
        (손익은 매수1호가 기준이라 소수점 아래에서 미세하게 다를 수 있다).
        """
        fee_pct = PARAMS["fee_rate"] * 100
        trade_date = datetime.strptime(self.date_str, "%Y%m%d").date()
        trades: List[Trade] = []

        for r_key, r in rules.items():
            exit_rule = EXIT_RULE_IDS[r_key]
            for i, h in enumerate(r["history"]):
                extra = r["excursions"][i] if i < len(r["excursions"]) else {}
                entry_p = float(h["entry_p"])
                peak_p = float(extra.get("peak_p", entry_p))
                trough_p = float(extra.get("trough_p", entry_p))

                trades.append(Trade(
                    run_id=self.run_id,
                    strategy_id=STRATEGY_ID,
                    strategy_version=STRATEGY_VERSION,
                    exit_rule=exit_rule,
                    code=self.code,
                    name="",                      # 틱 DB 에는 종목명이 없다 (Phase B: 일봉 매트릭스 연결)
                    date=trade_date,
                    entry_time=str(h["entry_t"]),
                    entry_price=entry_p,
                    exit_time=str(h["exit_t"]),
                    exit_price=float(h["exit_p"]),
                    qty=0,                        # 수량 미모델링 (Phase E: StrategyAccount)
                    gross_pnl_pct=round((float(h["exit_p"]) - entry_p) / entry_p * 100, 3),
                    fee_pct=fee_pct,
                    net_pnl_pct=float(h["net_pnl_pct"]),
                    mae_pct=round((trough_p - entry_p) / entry_p * 100, 3) if entry_p else 0.0,
                    mfe_pct=round((peak_p - entry_p) / entry_p * 100, 3) if entry_p else 0.0,
                    holding_sec=max(0, to_seconds(h["exit_t"]) - to_seconds(h["entry_t"])),
                    exit_reason=str(h["msg"]),
                    signal_meta={
                        "rule_name": r["name"],
                        "peak_price": peak_p,
                        "trough_price": trough_p,
                        **(extra.get("entry_meta") or {}),
                    },
                ))

        return trades


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NXT 프리마켓 과열방어 + 3대 트레일링 컷 틱 엔진")
    parser.add_argument("code", nargs="?", default="005930", help="대상 종목코드 (기본: 005930)")
    parser.add_argument(
        "--date", default="20260911",
        help="대상 날짜 YYYYMMDD (기본: 20260911). sampledata/raw_ticks/{date}_raw.db 를 찾는다 (§1.6)",
    )
    parser.add_argument("--latency-sec", type=int, default=1, help="지연 체결 초 (기본: 1)")
    parser.add_argument("--cooldown-sec", type=int, default=10, help="청산 후 재진입 대기 초 (기본: 10)")
    parser.add_argument(
        "--allow-unverified-nxt", action="store_true",
        help="거래소 미확인 상태에서도 실행한다. NXT 과열 필터는 적용되지 않으며 결과에 미검증으로 남는다",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_cli_args()
    engine = NextradeTickEngine(date_str=args.date, target_code=args.code,
                                allow_unverified_nxt=args.allow_unverified_nxt)
    engine.run_strategy(latency_sec=args.latency_sec, cooldown_sec=args.cooldown_sec)