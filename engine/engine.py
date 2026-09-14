import os
import sqlite3
import traceback
from datetime import datetime
import numpy as np
import pandas as pd
from engine.config import (  # ⭐️ engine. 추가
    STRATEGY_NAME,
    STRATEGY_VERSION,
    SET_TIME,
    RESULT_DIR,
    ENCODING,
    FEE_PCT,
    EXIT_RULE_ID,
    FEATURE_SET_VERSION,
)
from engine.utils import (
    calculate_ticksize,
    calculate_upperlimit,
    calculate_time_spread,
)
from engine.data_loader import DataLoader
from engine.risk_manager import check_exit_signals
from engine.strategy import calculate_window_metrics, check_entry_conditions

# 🆕 Phase A — 표준 결과 계약(L5). 기존 CSV 출력은 그대로 두고 병행 저장한다.
from core.contracts import Trade
from core.runstore import (
    RunManifest,
    RunStore,
    current_git_sha,
    hash_params,
    make_run_id,
)

# ====================================================================
# 🛠️ [DEBUG / TEST MODE] 단일 종목 필터
# 전체 종목 백테스팅은 시간이 오래 걸려 샘플 구간에서 기아(000270)만 돌린다.
# 전체 종목을 돌리려면 None 으로 바꾼다. 이 값은 RunManifest.params 에도
# 기록되므로, 나중에 런을 다시 열었을 때 "무엇을 돌린 결과인지" 알 수 있다.
# ====================================================================
DEBUG_ONLY_CODE: str | None = '000270'


class BackTestEngine:
    def __init__(self, part: int = 1, split: int = 12, runs_root=None):
        self.part = part
        self.split = split
        self.strategy = STRATEGY_NAME
        self.loader = DataLoader()

        # 일봉 데이터 미리 로드
        self.daily_data = self.loader.load_daily_csvs()
        self.trading = {}

        # 🆕 Phase A — 런 스토어 출력용 상태
        self.trades: list[Trade] = []      # 표준 Trade 레코드 (CSV 와 같은 거래를 담는다)
        self.run_id: str = ""
        self.run_params: dict = {}
        self.date_range: tuple[str, str] = ("", "")
        self.run_store = RunStore(runs_root)
        self.storage_key: str = ""

    def _init_trading_dict(self):
        """결과 저장용 디셔너리 구조 초기화"""
        return {
            'today': [], 'name': [], 'starttime': [], 'trigger': [], 't_open': [],
            'entry_price': [], 'exit_price': [], 'entry_time': [], 'exit_time': [],
            'pnl': [], 'mdd': [], 'mdu': [], 'last_high_elapsed': [], 'msg': [],
            'mkt_float': [], 'ytd_tradamt': [], 'cbv_1': [], 'ctotal': [], 'cum_amt': []
        }

    def run(self):
        """백테스트 가동 및 결과 저장 메인 루프"""
        print(f"🚀 [Part {self.part}/{self.split}] 백테스트 엔진 가동 시작")
        self.trading = self._init_trading_dict()
        self.trades = []

        if 'open' not in self.daily_data or self.daily_data['open'].empty:
            print("⚠️ 일봉 데이터(open.csv 등)를 읽을 수 없습니다.")
            return

        csv_open = self.daily_data['open']
        
        # 8자리 숫자 날짜만 추출
        date_list = [str(d) for d in csv_open['Code'][1:] if str(d).isdigit() and len(str(d)) == 8]
        
        length = int(len(date_list) / self.split)
        if self.part < self.split:
            load_dates = date_list[length * (self.part - 1): length * self.part]
        else:
            load_dates = date_list[length * (self.part - 1):]

        print(f"📅 담당 백테스팅 날짜 범위: {load_dates[0] if load_dates else '없음'} ~ {load_dates[-1] if load_dates else '없음'}")

        # 🆕 Phase A — 런 식별자 확정.
        # Trade 레코드가 run_id 를 들고 있어야 하므로 루프 '전에' 계산한다.
        self._prepare_run_identity(load_dates)

        processed_stocks = 0
        for today_date in load_dates:
            today_str = str(today_date)
            conn = self.loader.get_lob_db_connection(today_str)
            if conn is None:
                continue

            cursor = conn.cursor()
            sec_tables = set(name[0] for name in cursor.execute("SELECT name FROM sqlite_master WHERE type='table';"))

            # 🛠️ [DEBUG / TEST MODE] 단일 종목 필터 — 상단 DEBUG_ONLY_CODE 참고
            for code_col in csv_open.keys()[1:]:
                code = code_col[1:] if code_col.startswith('A') else code_col

                # 📌 [테스트용] 지정 종목이 아니면 즉시 스킵
                if DEBUG_ONLY_CODE and code != DEBUG_ONLY_CODE:
                    continue
                
                if code in sec_tables:
                    processed_stocks += 1
                    self._process_stock(conn, code_col, code, today_str)

            conn.close()

        print(f"📊 탐색한 총 종목 수: {processed_stocks}개")

        # 결과 CSV 저장 (레거시 경로 — 런 스토어 검증이 끝날 때까지 유지한다)
        result_df = pd.DataFrame(self.trading)
        output_file = RESULT_DIR / f"{self.strategy}_{self.part}.csv"
        result_df.to_csv(output_file, encoding=ENCODING, index=False)
        print(f"✅ [Part {self.part}] 백테스팅 완료! 저장 건수: {len(result_df)}건 ➔ 파일: {output_file}")

        # 🆕 Phase A — 표준 Trade 레코드를 런 스토어에도 저장 (CSV 와 병행)
        self._save_run(result_df, processed_stocks)

    # ------------------------------------------------------------------
    # 🆕 Phase A — 런 스토어 연동
    # ------------------------------------------------------------------

    def _prepare_run_identity(self, load_dates: list[str]) -> None:
        """
        run_id = hash(전략 버전 + 파라미터 + 피처셋 + 날짜범위 + git sha)

        같은 입력이면 같은 run_id 가 나와야 한다. 파트별로 날짜 범위가 다르므로
        병렬 실행되는 12개 파트는 서로 다른 run_id 를 갖는다.
        """
        self.date_range = (load_dates[0], load_dates[-1]) if load_dates else ("", "")

        # 지금 이 엔진의 거동을 결정하는 값 전부. 하나라도 바뀌면 run_id 가 바뀌어야 한다.
        # (Phase C 에서 strategies/*/params/*.yaml 로 이관된다)
        self.run_params = {
            "set_time": SET_TIME,
            "fee_pct": FEE_PCT,
            "exit_rule": EXIT_RULE_ID,
            "universe_filter": DEBUG_ONLY_CODE or "all",
            "partition": {"part": self.part, "split": self.split},
        }
        self.run_id = make_run_id(
            strategy_id=self.strategy,
            strategy_version=STRATEGY_VERSION,
            param_hash=hash_params(self.run_params),
            feature_set_version=FEATURE_SET_VERSION,
            date_range=self.date_range,
            git_sha=current_git_sha(),
        )

    def _save_run(self, result_df: pd.DataFrame, processed_stocks: int) -> None:
        """표준 Trade 레코드 + 매니페스트 기록, 그리고 레거시 CSV 와의 즉석 대조."""
        manifest = RunManifest(
            run_id=self.run_id,
            strategy_id=self.strategy,
            strategy_version=STRATEGY_VERSION,
            param_variant="default",
            param_hash=hash_params(self.run_params),
            feature_set_version=FEATURE_SET_VERSION,
            engine="bar",                      # 1초봉 해상도 엔진
            mode="backtest",
            date_range=self.date_range,
            universe_size=processed_stocks,
            git_sha=current_git_sha(),
            params=self.run_params,
            notes=f"legacy CSV 병행 출력: {RESULT_DIR / f'{self.strategy}_{self.part}.csv'}",
        )
        self.storage_key = self.run_store.save(manifest, self.trades)
        run_path = self.run_store.run_dir(self.storage_key)
        print(f"💾 [Part {self.part}] 런 저장 완료: run_id={self.run_id} ➔ {run_path}")

        # 레거시 CSV 와 새 parquet 이 같은 거래를 담고 있는지 즉석 대조.
        # 어긋나면 매핑이 잘못된 것이므로 CSV 를 지우면 안 된다는 신호다.
        csv_count, trade_count = len(result_df), len(self.trades)
        csv_pnl = float(pd.to_numeric(result_df['pnl'], errors='coerce').fillna(0).sum()) if csv_count else 0.0
        trade_pnl = float(sum(t.net_pnl_pct for t in self.trades))
        if csv_count == trade_count and abs(csv_pnl - trade_pnl) < 1e-9:
            print(f"   ✅ CSV/parquet 대조 일치 — {trade_count}건, 누적 PnL {trade_pnl:+.3f}%")
        else:
            print(
                f"   ❌ CSV/parquet 불일치! CSV {csv_count}건({csv_pnl:+.3f}%) vs "
                f"parquet {trade_count}건({trade_pnl:+.3f}%) — 매핑을 점검하세요"
            )

    def _to_trade(self, *, stock, code, stock_name, today_str, entry_time_str, mae_pct, mfe_pct) -> Trade:
        """
        레거시 20컬럼 CSV 한 줄 -> 표준 Trade 레코드 (ARCHITECTURE_V2.md §6.2)

            today                     -> date
            name                      -> name
            entry/exit_price·time     -> 동일 이름
            pnl                       -> net_pnl_pct   (수수료 차감 후. 값 그대로)
            mdd                       -> mae_pct       (최대 역행폭)
            mdu                       -> mfe_pct       (최대 순행폭)
            msg                       -> exit_reason
            starttime·trigger·t_open  -> signal_meta
            cbv_1·ctotal              -> signal_meta   (진입 시점 피처 스냅샷)
            mkt_float·ytd_tradamt·cum_amt -> signal_meta.placeholders

        ⚠️ mkt_float=1000 / ytd_tradamt=100 / cum_amt=100 은 계산된 값이 아니라
           코드에 박힌 상수다(ARCHITECTURE_V2.md §1.5 의 '끊어진 연결선').
           Trade 의 정식 필드로 올리면 가짜 숫자가 결과 계약에 섞여 들어가므로,
           placeholders 로 격리해 둔다. Phase B 에서 거시 피처가 연결되면
           실제 값으로 대체한다.
        """
        entry_price = float(stock['entry_price'])
        exit_price = float(stock['exit_price'])
        exit_time_str = str(stock['exit_time'])

        # net 은 CSV 와 완전히 같은 값을 쓴다. gross 는 수수료 차감 전 원본.
        gross_pnl_pct = round((exit_price - entry_price) / entry_price * 100, 3)
        holding_sec = max(0, calculate_time_spread('second', entry_time_str, exit_time_str))

        return Trade(
            run_id=self.run_id,
            strategy_id=self.strategy,
            strategy_version=STRATEGY_VERSION,
            exit_rule=EXIT_RULE_ID,
            code=code,
            name=stock_name,
            date=datetime.strptime(today_str, "%Y%m%d").date(),
            entry_time=entry_time_str,
            entry_price=entry_price,
            exit_time=exit_time_str,
            exit_price=exit_price,
            qty=0,                      # 이 엔진은 수량을 모델링하지 않는다 (Phase E: StrategyAccount)
            gross_pnl_pct=gross_pnl_pct,
            fee_pct=FEE_PCT,
            net_pnl_pct=stock['pnl'],
            mae_pct=mae_pct,
            mfe_pct=mfe_pct,
            holding_sec=holding_sec,
            exit_reason=str(stock['msg']),
            signal_meta={
                "session_start_time": str(stock['time'][0]),
                "trigger": stock.get('trigger', 0),
                "day_open": float(stock['open'][0]),
                "upper_limit": float(stock.get('upper', 0.0)),
                "last_high_elapsed": 0,
                "cbv_1": stock.get('cbv_1', 0),
                "ctotal": stock.get('ctotal', 0),
                # 아직 실제 값이 연결되지 않은 자리 (Phase B)
                "placeholders": {"mkt_float": 1000, "ytd_tradamt": 100, "cum_amt": 100},
            },
        )

    def _process_stock(self, conn, code_col, code, today_str):
        try:
            stock_name = str(self.daily_data['open'][code_col].iloc[0])
            raw_data = pd.DataFrame(conn.cursor().execute(f"SELECT * FROM '{code}'").fetchall())
            if raw_data.empty:
                return

            # 수치 데이터 타입 강제 변환 (SQLite 문자열 타입 방지)
            times = np.array(raw_data[0]).astype(str)
            opens = np.array(raw_data[1]).astype(float)
            highs = np.array(raw_data[2]).astype(float)
            lows = np.array(raw_data[3]).astype(float)
            closes = np.array(raw_data[4]).astype(float)
            vols = np.array(raw_data[5]).astype(float)
            buy_vols = np.array(raw_data[6]).astype(float)
            sell_vols = np.array(raw_data[7]).astype(float)
            ticks = np.array(raw_data[8]).astype(float)

            stock = {
                'name': stock_name,
                'time': times,
                'open': opens,
                'high': highs,
                'low': lows,
                'close': closes,
                'vol': vols,
                'buy_vol': buy_vols,
                'sell_vol': sell_vols,
                'tick': ticks,
                'candle_high': [], 'candle_low': [], 'candle_open': [],
                'position': 0, 'state': 0, 'entry_t': 0, 'entry_price': 0.0,
                'max_t': 0.0, 'min_t': 999999999.0, 'upper': opens[0] * 1.3,
                'tick_rate': 0.1, 'max_cbv5': 0.0, 'max_cbv10': 0.0, 'max_cbv30': 0.0, 'max_cbv60': 0.0,
                'tmax_cbv5': 0.0, 'tmax_cbv10': 0.0, 'tmax_cbv30': 0.0, 'tmax_cbv60': 0.0, 'tmax_cbv1': 0.0
            }

            for t in range(len(stock['time'])):
                time_str = str(stock['time'][t])
                
                # 캔들 데이터 업데이트 (candle_close 는 어디서도 읽지 않아 제거함 — §1.6)
                stock['candle_high'].append(stock['high'][t])
                stock['candle_low'].append(stock['low'][t])
                stock['candle_open'].append(stock['open'][t])

                # 윈도우 수치 연산
                calculate_window_metrics(stock, t)

                # 1. 청산 조건 체킹 (포지션 보유 시)
                if stock['position'] == 1 and stock['state'] == 0:
                    exit_msg = check_exit_signals(stock, t, time_str)
                    if exit_msg:
                        stock['exit_price'] = stock['low'][t]
                        stock['exit_time'] = time_str
                        stock['pnl'] = round((stock['exit_price'] - stock['entry_price']) / stock['entry_price'] * 100, 3) - 0.23
                        stock['msg'] = exit_msg
                        stock['position'] = 0
                        stock['state'] = 1
                        
                        # 파생값은 한 번만 계산해서 CSV 와 Trade 양쪽에 같은 값을 넣는다
                        # (두 번 계산하면 언젠가 반드시 어긋난다)
                        entry_time_str = str(stock['time'][stock['entry_t']])
                        mae_pct = round((stock['min_t'] - stock['entry_price'])/stock['entry_price']*100, 2)
                        mfe_pct = round((stock['max_t'] - stock['entry_price'])/stock['entry_price']*100, 2)

                        # 거래 기록 저장 (레거시 20컬럼 CSV)
                        self.trading['today'].append(today_str)
                        self.trading['name'].append(stock_name)
                        self.trading['starttime'].append(stock['time'][0])
                        self.trading['trigger'].append(stock.get('trigger', 0))
                        self.trading['t_open'].append(stock['open'][0])
                        self.trading['entry_price'].append(stock['entry_price'])
                        self.trading['exit_price'].append(stock['exit_price'])
                        self.trading['entry_time'].append(stock['time'][stock['entry_t']])
                        self.trading['exit_time'].append(stock['exit_time'])
                        self.trading['pnl'].append(stock['pnl'])
                        self.trading['mdd'].append(mae_pct)
                        self.trading['mdu'].append(mfe_pct)
                        self.trading['last_high_elapsed'].append(0)
                        self.trading['msg'].append(stock['msg'])
                        self.trading['mkt_float'].append(1000)
                        self.trading['ytd_tradamt'].append(100)
                        self.trading['cbv_1'].append(stock.get('cbv_1', 0))
                        self.trading['ctotal'].append(stock.get('ctotal', 0))
                        self.trading['cum_amt'].append(100)

                        # 🆕 Phase A — 같은 거래를 표준 Trade 계약으로도 기록
                        self.trades.append(self._to_trade(
                            stock=stock, code=code, stock_name=stock_name, today_str=today_str,
                            entry_time_str=entry_time_str, mae_pct=mae_pct, mfe_pct=mfe_pct,
                        ))

                        print(f"★ [매도 완료] 종목: {stock['name']}({code}), PnL: {stock['pnl']}%, 사유: {exit_msg}")

                # 2. 진입 조건 체킹
                if check_entry_conditions(stock, t, SET_TIME):
                    stock['position'] = 1
                    stock['entry_t'] = t + 1 if t + 1 < len(stock['time']) else t
                    stock['entry_price'] = stock['high'][stock['entry_t']]
                    print(f"★ [매수 진입] 종목: {stock['name']}({code}), 시간: {time_str}, 진입가: {stock['entry_price']}")

        except Exception as e:
            # 에러 원인 출력 (숨기지 않음!)
            print(f"❌ 종목 [{stock['name']}({code})] 처리 중 에러 발생: {e}")
            traceback.print_exc()