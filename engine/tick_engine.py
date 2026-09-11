"""
초경량 틱 단위 이벤트 드리븐 백테스트 엔진 (Tick Event-Driven Backtest Engine)
- raw_ticks DB에서 직접 체결(trades)과 10호가(quotes)를 시간순으로 동기화
- 틱 발생 순간의 실제 매도1호가(offer_p1) 매수, 매수1호가(bid_p1) 매도로 실전 슬리피지 100% 반영
"""

from pathlib import Path
import sqlite3
import sys
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import DATA_DIR

RAW_DIR = DATA_DIR / "raw_ticks"


class TickBacktestEngine:

    def __init__(self, date_str: str, target_code: str):
        self.date_str = date_str
        self.code = target_code
        self.db_path = RAW_DIR / f"{date_str}_raw.db"

        if not self.db_path.exists():
            raise FileNotFoundError(f"원본 틱 파일이 없습니다: {self.db_path}")

    def load_and_align_data(self) -> list[dict]:
        """체결과 호가를 시간순으로 정렬하여 틱마다 당시의 1호가를 매핑"""
        print(f"🔍 [{self.code}] 틱 및 호가 데이터 로딩 중...")
        conn = sqlite3.connect(self.db_path)

        # 1. 체결 데이터 로드
        trades = pd.read_sql_query(
            f"SELECT * FROM raw_trades WHERE code='{self.code}' ORDER BY rowid ASC",
            conn,
        )
        # 2. 호가 데이터 로드
        quotes = pd.read_sql_query(
            f"SELECT * FROM raw_quotes WHERE code='{self.code}' ORDER BY rowid ASC",
            conn,
        )
        conn.close()

        if trades.empty:
            print("⚠️ 체결 데이터가 없습니다.")
            return []

        print(
            f"  - 체결 틱 수: {len(trades):,}건 | 호가 변동 수: {len(quotes):,}건"
        )

        # 호가에서 매도1호가, 매수1호가 추출
        quotes["ask_p1"] = quotes["offer_p"].apply(
            lambda x: float(x.split(",")[0]) if x else 0.0
        )
        quotes["bid_p1"] = quotes["bid_p"].apply(
            lambda x: float(x.split(",")[0]) if x else 0.0
        )

        # 체결 틱마다 그 순간 가장 최근 호가 1호가(매도1, 매수1) 매핑
        events = []
        q_idx = 0
        cur_ask_p1 = 0.0
        cur_bid_p1 = 0.0

        for _, t_row in trades.iterrows():
            t_time = t_row["t_time"]

            # 호가 시간 동기화 (체결 시간 이하의 최신 호가 탐색)
            while q_idx < len(quotes) and quotes.iloc[q_idx]["q_time"] <= t_time:
                cur_ask_p1 = quotes.iloc[q_idx]["ask_p1"]
                cur_bid_p1 = quotes.iloc[q_idx]["bid_p1"]
                q_idx += 1

            events.append(
                {
                    "time": t_time,
                    "price": t_row["price"],
                    "vol": t_row["vol"],
                    "is_buy": t_row["is_buy"],
                    # 실전 체결 호가 (매수할 땐 ask_p1, 매도할 땐 bid_p1)
                    "ask_p1": cur_ask_p1 if cur_ask_p1 > 0 else t_row["price"],
                    "bid_p1": cur_bid_p1 if cur_bid_p1 > 0 else t_row["price"],
                }
            )

        return events

    def run_strategy(self, window_ticks: int = 15, buy_ratio_th: float = 0.8):
        """
        초단타 틱 전략 시뮬레이션
        - 진입 조건: 최근 N개 틱 중 '시장가 매수 체결 비중'이 80% 이상 & 거래량 급증 시
        - 진입 가격: 당시 실제 매도 1호가(ask_p1)
        - 청산 조건: 손절 -0.5% 또는 익절 +0.8% 도달 시 매수 1호가(bid_p1)로 시장가 청산
        """
        events = self.load_and_align_data()
        if not events:
            return

        print("\n⚡ [틱 단위 이벤트 백테스팅 가동 시작]")
        trades_history = []
        recent_ticks = []

        position = 0  # 0: 무포지션, 1: 보유
        entry_price = 0.0
        entry_time = ""

        # 실전 거래비용 (증권사 수수료 + 증권거래세 약 0.20%)
        fee_rate = 0.20 / 100

        for idx, tick in enumerate(events):
            recent_ticks.append(tick)
            if len(recent_ticks) > window_ticks:
                recent_ticks.pop(0)

            # -----------------------------------------------
            # 1. 청산 로직 (포지션 보유 시 매 틱마다 감시)
            # -----------------------------------------------
            if position == 1:
                cur_bid = tick["bid_p1"]  # 내가 던져서 체결될 매수1호가
                raw_pnl = (cur_bid - entry_price) / entry_price
                net_pnl = raw_pnl - fee_rate

                # 청산 규칙 (손절 -0.5% 이하, 익절 +0.8% 이상, 혹은 100틱 이상 보유 시 타임컷)
                exit_msg = None
                if raw_pnl <= -0.005:
                    exit_msg = "손절 (Stop Loss)"
                elif raw_pnl >= 0.008:
                    exit_msg = "익절 (Take Profit)"
                elif idx % 100 == 0:  # 너무 오래 끌면 청산
                    exit_msg = "시간 청산 (Time Exit)"

                if exit_msg:
                    trades_history.append(
                        {
                            "entry_time": entry_time,
                            "entry_price": entry_price,
                            "exit_time": tick["time"],
                            "exit_price": cur_bid,
                            "net_pnl_pct": round(net_pnl * 100, 3),
                            "msg": exit_msg,
                        }
                    )
                    position = 0
                    entry_price = 0.0
                    continue

            # -----------------------------------------------
            # 2. 진입 로직 (무포지션일 때 틱 수급 감시)
            # -----------------------------------------------
            if position == 0 and len(recent_ticks) == window_ticks:
                tot_vol = sum(t["vol"] for t in recent_ticks)
                buy_vol = sum(t["vol"] for t in recent_ticks if t["is_buy"] == 1)
                buy_ratio = buy_vol / tot_vol if tot_vol > 0 else 0

                # 최근 N틱 중 80% 이상이 시장가 매수 틱으로 쏠렸을 때
                if buy_ratio >= buy_ratio_th and tot_vol >= 50:
                    # 매도 1호가로 즉시 진입 (가장 가혹하고 현실적인 체결가)
                    entry_price = tick["ask_p1"]
                    entry_time = tick["time"]
                    position = 1

        # -----------------------------------------------
        # 결과 요약 리포트 출력
        # -----------------------------------------------
        print("=" * 60)
        print(f"📊 [{self.code}] 틱 백테스트 결과 리포트")
        print("=" * 60)
        df_res = pd.DataFrame(trades_history)
        if df_res.empty:
            print("⚠️ 조건에 일치하는 매매가 발생하지 않았습니다.")
            return

        wins = df_res[df_res["net_pnl_pct"] > 0]
        win_rate = len(wins) / len(df_res) * 100
        avg_pnl = df_res["net_pnl_pct"].mean()
        tot_pnl = df_res["net_pnl_pct"].sum()

        print(f"• 총 거래 횟수: {len(df_res)}회")
        print(f"• 승률 (Win Rate): {win_rate:.2f}% ({len(wins)}승 {len(df_res)-len(wins)}패)")
        print(f"• 평균 손익률: {avg_pnl:.3f}%")
        print(f"• 누적 손익률: {tot_pnl:.3f}%")
        print("\n[상세 거래 내역 (최근 10건)]")
        print(df_res.tail(10).to_string(index=False))


if __name__ == "__main__":
    # 오늘 오전에 거래가 가장 활발했던 종목(예: 삼성전자 005930 또는 0015N0) 테스트
    target_stock = sys.argv[1] if len(sys.argv) > 1 else "005930"
    engine = TickBacktestEngine(date_str="20260911", target_code=target_stock)
    engine.run_strategy()