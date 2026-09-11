"""
현실적 1~2초 지연(Latency) 반영 틱 이벤트 백테스트 엔진
- 1~2초 통신 렉을 가혹하게 반영하여 실전 슬리피지 검증
- 청산 후 5초 쿨다운(Cooldown) 적용으로 톱니바퀴 연쇄 손실 방지
"""

from pathlib import Path
import sqlite3
import sys
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
from engine.config import DATA_DIR

RAW_DIR = DATA_DIR / "raw_ticks"


def to_seconds(t_str: str) -> int:
    """HHMMSS 시간 문자열을 초(Second) 정수로 변환"""
    t = str(t_str).zfill(6)
    return int(t[:2]) * 3600 + int(t[2:4]) * 60 + int(t[4:6])


class TickBacktestEngine:

    def __init__(self, date_str: str, target_code: str):
        self.date_str = date_str
        self.code = target_code
        self.db_path = RAW_DIR / f"{date_str}_raw.db"

        if not self.db_path.exists():
            raise FileNotFoundError(f"원본 틱 파일이 없습니다: {self.db_path}")

    def load_and_align_data(self) -> list[dict]:
        """체결과 호가를 시간순으로 정렬하여 당시 1호가 매핑"""
        print(f"🔍 [{self.code}] 틱 및 호가 데이터 로딩 중...")
        conn = sqlite3.connect(self.db_path)
        trades = pd.read_sql_query(
            f"SELECT * FROM raw_trades WHERE code='{self.code}' ORDER BY rowid ASC",
            conn,
        )
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

        quotes["ask_p1"] = quotes["offer_p"].apply(
            lambda x: float(x.split(",")[0]) if x else 0.0
        )
        quotes["bid_p1"] = quotes["bid_p"].apply(
            lambda x: float(x.split(",")[0]) if x else 0.0
        )

        events = []
        q_idx = 0
        cur_ask_p1 = 0.0
        cur_bid_p1 = 0.0

        for _, t_row in trades.iterrows():
            t_time = t_row["t_time"]
            while q_idx < len(quotes) and quotes.iloc[q_idx]["q_time"] <= t_time:
                cur_ask_p1 = quotes.iloc[q_idx]["ask_p1"]
                cur_bid_p1 = quotes.iloc[q_idx]["bid_p1"]
                q_idx += 1

            events.append(
                {
                    "time": t_time,
                    "sec": to_seconds(t_time),
                    "price": t_row["price"],
                    "vol": t_row["vol"],
                    "is_buy": t_row["is_buy"],
                    "ask_p1": cur_ask_p1 if cur_ask_p1 > 0 else t_row["price"],
                    "bid_p1": cur_bid_p1 if cur_bid_p1 > 0 else t_row["price"],
                }
            )

        return events

    def run_strategy(
        self,
        window_ticks: int = 15,
        buy_ratio_th: float = 0.75,
        latency_sec: int = 1,
        cooldown_sec: int = 5,
    ):
        """
        :param latency_sec: 주문 후 체결까지 걸리는 가혹한 지연 시간 (1초 또는 2초)
        :param cooldown_sec: 청산 후 재진입 금지 시간 (5초)
        """
        events = self.load_and_align_data()
        if not events:
            return

        print(
            f"\n⚡ [틱 이벤트 백테스팅 가동] 지연체결: {latency_sec}초 페널티 | 재진입쿨다운: {cooldown_sec}초"
        )

        trades_history = []
        recent_ticks = []

        position = 0
        entry_price = 0.0
        entry_time = ""

        pending_buy = False
        target_buy_sec = 0
        cooldown_until_sec = 0

        fee_rate = 0.20 / 100  # 수수료 + 세금 0.20%

        for tick in events:
            cur_sec = tick["sec"]

            # -----------------------------------------------
            # 1. 지연 체결 처리 (시그널 후 latency_sec 초가 지난 시점에 체결!)
            # -----------------------------------------------
            if pending_buy and cur_sec >= target_buy_sec:
                # 1~2초 뒤의 실제 매도 1호가로 체결
                entry_price = tick["ask_p1"]
                entry_time = tick["time"]
                position = 1
                pending_buy = False

            # -----------------------------------------------
            # 2. 청산 감시 (포지션 보유 시 매 틱 감시)
            # -----------------------------------------------
            if position == 1:
                cur_bid = tick["bid_p1"]
                raw_pnl = (cur_bid - entry_price) / entry_price
                net_pnl = raw_pnl - fee_rate

                exit_msg = None
                if raw_pnl <= -0.005:  # 손절 -0.5%
                    exit_msg = "손절 (Stop Loss)"
                elif raw_pnl >= 0.008:  # 익절 +0.8%
                    exit_msg = "익절 (Take Profit)"

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
                    # 청산 후 5초간 쿨다운 (뇌동매매 방지)
                    cooldown_until_sec = cur_sec + cooldown_sec
                    continue

            # -----------------------------------------------
            # 3. 진입 조건 감시 (최근 N틱 중 75% 이상 시장가 매수 쏠림)
            # -----------------------------------------------
            recent_ticks.append(tick)
            if len(recent_ticks) > window_ticks:
                recent_ticks.pop(0)

            if (
                position == 0
                and not pending_buy
                and cur_sec >= cooldown_until_sec
            ):
                if len(recent_ticks) == window_ticks:
                    tot_vol = sum(t["vol"] for t in recent_ticks)
                    buy_vol = sum(
                        t["vol"] for t in recent_ticks if t["is_buy"] == 1
                    )
                    buy_ratio = buy_vol / tot_vol if tot_vol > 0 else 0

                    if buy_ratio >= buy_ratio_th and tot_vol >= 50:
                        # ⭐️ 즉시 체결하지 않고, 1~2초 뒤에 체결되도록 예약!
                        pending_buy = True
                        target_buy_sec = cur_sec + latency_sec

        # -----------------------------------------------
        # 결과 요약 리포트
        # -----------------------------------------------
        print("=" * 60)
        print(
            f"📊 [{self.code}] 틱 백테스트 결과 (지연: {latency_sec}초 / 쿨다운: {cooldown_sec}초)"
        )
        print("=" * 60)
        df_res = pd.DataFrame(trades_history)
        if df_res.empty:
            print(
                "⚠️ 지연 체결 페널티를 통과한 매매가 없습니다. (수급이 단발성이었음)"
            )
            return

        wins = df_res[df_res["net_pnl_pct"] > 0]
        win_rate = len(wins) / len(df_res) * 100
        avg_pnl = df_res["net_pnl_pct"].mean()
        tot_pnl = df_res["net_pnl_pct"].sum()

        print(f"• 총 거래 횟수: {len(df_res)}회")
        print(
            f"• 승률 (Win Rate): {win_rate:.2f}% ({len(wins)}승 {len(df_res)-len(wins)}패)"
        )
        print(f"• 평균 손익률: {avg_pnl:.3f}%")
        print(f"• 누적 손익률: {tot_pnl:.3f}%")
        print("\n[상세 거래 내역]")
        print(df_res.to_string(index=False))


if __name__ == "__main__":
    target_stock = sys.argv[1] if len(sys.argv) > 1 else "005930"
    engine = TickBacktestEngine(date_str="20260911", target_code=target_stock)
    # 1초 지연 체결 테스트 (원하시면 latency_sec=2 로 변경 가능)
    engine.run_strategy(
        window_ticks=15, buy_ratio_th=0.75, latency_sec=1, cooldown_sec=5
    )