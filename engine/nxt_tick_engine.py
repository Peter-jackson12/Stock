"""
대체거래소(NXT) 과열 필터 및 3대 트레일링 컷 비교 틱 이벤트 백테스트 엔진
- NXT 프리마켓(08:00~08:50) 과열 감지 및 09:00 설거지 음봉 방어
- 당일 시가(Open) 및 마이크로 고점 돌파 + 호가잔량 불균형(OBI) 진입
- 1초 지연 체결(1s Latency) 및 0.20% 세금/수수료 엄격 반영
- 3대 청산 규칙(고정 / 2틱 반락 트레일링 / 본전보존 계단식 트레일링) 동시 비교
"""

from pathlib import Path
import sqlite3
import sys
from typing import Dict, List, Any
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import DATA_DIR

RAW_DIR = DATA_DIR / "raw_ticks"


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
    def __init__(self, date_str: str, target_code: str):
        self.date_str = date_str
        self.code = target_code
        self.db_path = RAW_DIR / f"{date_str}_raw.db"

        if not self.db_path.exists():
            raise FileNotFoundError(f"데이터베이스 파일이 없습니다: {self.db_path}")

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
            return

        # =======================================================
        # 1. NXT(08:00~08:50) 프리마켓 분석 및 과열 여부 판정
        # =======================================================
        nxt_trades = [e for e in events if 28800 <= e["sec"] < 31800]  # 08:00 ~ 08:50
        is_nxt_exhausted = False
        nxt_open = nxt_trades[0]["price"] if nxt_trades else 0.0

        if nxt_trades:
            nxt_close = nxt_trades[-1]["price"]
            nxt_vol = sum(e["vol"] for e in nxt_trades)
            nxt_gain = (nxt_close - nxt_open) / nxt_open if nxt_open > 0 else 0.0
            
            # NXT에서 이미 +5% 이상 폭등하고 거래량이 폭발한 경우 -> 9시 정규장 매수 금지!
            if nxt_gain >= 0.05 and nxt_vol >= 50000:
                is_nxt_exhausted = True
                print(f"🚨 [NXT 과열 감지] 프리마켓 급등(+{nxt_gain*100:.2f}%)으로 09:00 개장 직후 상투 위험 -> 정규장 매수 제한 발동!")

        # 09:00 정규장 시가 기준선 설정
        krx_trades = [e for e in events if e["sec"] >= 32400]  # 09:00:00 이후
        krx_open = krx_trades[0]["price"] if krx_trades else events[0]["price"]

        print(f"🎯 [시가 기준선] 당일 시가: {krx_open:,.0f}원 | 체결 지연 페널티: {latency_sec}초")

        # =======================================================
        # 2. 3대 청산 규칙 동시 시뮬레이션 구조
        # =======================================================
        FEE_RATE = 0.20 / 100  # 세금 + 수수료

        # 각 청산 룰별 상태 관리
        rules = {
            "A_Fixed": {"name": "Rule A (고정익절 +0.8% / 손절 -0.5%)", "pos": 0, "entry_p": 0.0, "entry_t": "", "peak_p": 0.0, "history": []},
            "B_2TickTrail": {"name": "Rule B (2틱 반락 트레일링 컷)", "pos": 0, "entry_p": 0.0, "entry_t": "", "peak_p": 0.0, "history": []},
            "C_StepTrail": {"name": "Rule C (본전보존 계단식 트레일링 컷)", "pos": 0, "entry_p": 0.0, "entry_t": "", "peak_p": 0.0, "history": []}
        }

        pending_buy = False
        target_buy_sec = 0
        cooldown_until_sec = 0

        rolling_60s_ticks = []
        recent_15_ticks = []

        for idx, tick in enumerate(events):
            cur_sec = tick["sec"]
            cur_p = tick["price"]
            cur_bid = tick["bid_p1"]

            # 마이크로 1분봉(60초) 고가 추적용 큐
            rolling_60s_ticks.append(tick)
            while rolling_60s_ticks and (cur_sec - rolling_60s_ticks[0]["sec"]) > 60:
                rolling_60s_ticks.pop(0)

            recent_15_ticks.append(tick)
            if len(recent_15_ticks) > 15:
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
                pending_buy = False

            # ---------------------------------------------------
            # [2단계: 3대 청산 규칙 동시 감시]
            # ---------------------------------------------------
            for r_key, r in rules.items():
                if r["pos"] == 1:
                    r["peak_p"] = max(r["peak_p"], cur_p)
                    tick_size_peak = get_tick_size(r["peak_p"])
                    raw_pnl = (cur_bid - r["entry_p"]) / r["entry_p"]
                    net_pnl = raw_pnl - FEE_RATE

                    exit_msg = None

                    # --- Rule A: 고정 손익절 ---
                    if r_key == "A_Fixed":
                        if raw_pnl <= -0.005:
                            exit_msg = "손절 (-0.5%)"
                        elif raw_pnl >= 0.008:
                            exit_msg = "익절 (+0.8%)"

                    # --- Rule B: 2틱 반락 트레일링 컷 ---
                    elif r_key == "B_2TickTrail":
                        if raw_pnl <= -0.005:
                            exit_msg = "손절 (-0.5%)"
                        # 최고가 대비 2틱 밀리면 청산
                        elif cur_bid <= (r["peak_p"] - tick_size_peak * 2):
                            exit_msg = f"2틱 반락 컷 (고점: {r['peak_p']:,.0f})"

                    # --- Rule C: 본전보존 계단식 트레일링 컷 ---
                    elif r_key == "C_StepTrail":
                        entry_tick_size = get_tick_size(r["entry_p"])
                        breakeven_p = r["entry_p"] + entry_tick_size  # 수수료 방어선 (진입가+1틱)
                        
                        # +0.4% 이상 상승 시 손절선을 본전+1틱으로 상향
                        if r["peak_p"] >= r["entry_p"] * 1.004:
                            if cur_bid <= breakeven_p:
                                exit_msg = "본전 방어 컷"
                            elif cur_bid <= (r["peak_p"] - tick_size_peak * 2):
                                exit_msg = "계단식 트레일 익절"
                        else:
                            if raw_pnl <= -0.005:
                                exit_msg = "원칙 손절 (-0.5%)"

                    # 청산 실행
                    if exit_msg:
                        r["history"].append({
                            "entry_t": r["entry_t"], "entry_p": r["entry_p"],
                            "exit_t": tick["time"], "exit_p": cur_bid,
                            "net_pnl_pct": round(net_pnl * 100, 3), "msg": exit_msg
                        })
                        r["pos"] = 0
                        cooldown_until_sec = cur_sec + cooldown_sec

            # ---------------------------------------------------
            # [3단계: 시가/고가 돌파 + OBI 호가 진입 감시]
            # ---------------------------------------------------
            all_idle = all(rules[k]["pos"] == 0 for k in rules)
            if all_idle and not pending_buy and cur_sec >= cooldown_until_sec:
                # NXT 과열 필터에 걸린 종목은 09:00 정규장 돌파 진입 금지!
                if is_nxt_exhausted and cur_sec >= 32400:
                    continue

                # 1) 호가 스프레드 검증 (1틱 초과 시 진입 금지)
                spread_pct = (tick["ask_p1"] - tick["bid_p1"]) / tick["bid_p1"] if tick["bid_p1"] > 0 else 1.0
                if spread_pct > 0.0025:
                    continue

                # 2) 거시 기준선 돌파: 당일 시가(Open) 돌파 & 60초 마이크로 최고가 갱신
                micro_high_60s = max([t["price"] for t in rolling_60s_ticks[:-1]]) if len(rolling_60s_ticks) > 1 else cur_p
                is_breakout = (cur_p >= krx_open) and (cur_p >= micro_high_60s)

                # 3) 미시 틱 수급: 최근 15틱 중 75% 이상 시장가 매수
                tot_vol_15 = sum(t["vol"] for t in recent_15_ticks)
                buy_vol_15 = sum(t["vol"] for t in recent_15_ticks if t["is_buy"] == 1)
                buy_ratio = buy_vol_15 / tot_vol_15 if tot_vol_15 > 0 else 0.0

                # 4) 호가잔량 불균형 (OBI): 매수 1~3호가 잔량 > 매도 1~3호가 잔량 * 1.2배
                is_obi_bullish = tick["bid_v_top3"] > (tick["ask_v_top3"] * 1.2)

                # 모든 조건을 만족할 때만 1초 지연 예약 진입!
                if is_breakout and buy_ratio >= 0.75 and is_obi_bullish and tot_vol_15 >= 30:
                    pending_buy = True
                    target_buy_sec = cur_sec + latency_sec

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


if __name__ == "__main__":
    target_stock = sys.argv[1] if len(sys.argv) > 1 else "005930"
    engine = NextradeTickEngine(date_str="20260911", target_code=target_stock)
    engine.run_strategy(latency_sec=1, cooldown_sec=10)