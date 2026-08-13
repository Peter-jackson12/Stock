import os
import sqlite3
import traceback
import numpy as np
import pandas as pd
from config import STRATEGY_NAME, SET_TIME, RESULT_DIR, ENCODING
from utils import calculate_ticksize, calculate_upperlimit, calculate_time_spread
from data_loader import DataLoader
from risk_manager import check_exit_signals
from strategy import calculate_window_metrics, check_entry_conditions

class BackTestEngine:
    def __init__(self, part: int = 1, split: int = 12):
        self.part = part
        self.split = split
        self.strategy = STRATEGY_NAME
        self.loader = DataLoader()
        
        # 일봉 데이터 미리 로드
        self.daily_data = self.loader.load_daily_csvs()
        self.trading = {}

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

        processed_stocks = 0
        for today_date in load_dates:
            today_str = str(today_date)
            conn = self.loader.get_lob_db_connection(today_str)
            if conn is None:
                continue

            cursor = conn.cursor()
            sec_tables = set(name[0] for name in cursor.execute("SELECT name FROM sqlite_master WHERE type='table';"))

            # 종목 탐색
            for code_col in csv_open.keys()[1:]:
                code = code_col[1:] if code_col.startswith('A') else code_col
                if code in sec_tables:
                    processed_stocks += 1
                    self._process_stock(conn, code_col, code, today_str)

            conn.close()

        print(f"📊 탐색한 총 종목 수: {processed_stocks}개")

        # 결과 CSV 저장
        result_df = pd.DataFrame(self.trading)
        output_file = RESULT_DIR / f"{self.strategy}_{self.part}.csv"
        result_df.to_csv(output_file, encoding=ENCODING, index=False)
        print(f"✅ [Part {self.part}] 백테스팅 완료! 저장 건수: {len(result_df)}건 ➔ 파일: {output_file}")

    def _process_stock(self, conn, code_col, code, today_str):
        """개별 종목 백테스팅 연산 수행"""
        try:
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
                'time': times,
                'open': opens,
                'high': highs,
                'low': lows,
                'close': closes,
                'vol': vols,
                'buy_vol': buy_vols,
                'sell_vol': sell_vols,
                'tick': ticks,
                'candle_high': [], 'candle_low': [], 'candle_open': [], 'candle_close': [],
                'position': 0, 'state': 0, 'entry_t': 0, 'entry_price': 0.0,
                'max_t': 0.0, 'min_t': 999999999.0, 'upper': opens[0] * 1.3,
                'tick_rate': 0.1, 'max_cbv5': 0.0, 'max_cbv10': 0.0, 'max_cbv30': 0.0, 'max_cbv60': 0.0,
                'tmax_cbv5': 0.0, 'tmax_cbv10': 0.0, 'tmax_cbv30': 0.0, 'tmax_cbv60': 0.0, 'tmax_cbv1': 0.0
            }

            for t in range(len(stock['time'])):
                time_str = str(stock['time'][t])
                
                # 캔들 데이터 업데이트
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
                        
                        # 거래 기록 저장
                        self.trading['today'].append(today_str)
                        self.trading['name'].append(code)
                        self.trading['starttime'].append(stock['time'][0])
                        self.trading['trigger'].append(stock.get('trigger', 0))
                        self.trading['t_open'].append(stock['open'][0])
                        self.trading['entry_price'].append(stock['entry_price'])
                        self.trading['exit_price'].append(stock['exit_price'])
                        self.trading['entry_time'].append(stock['time'][stock['entry_t']])
                        self.trading['exit_time'].append(stock['exit_time'])
                        self.trading['pnl'].append(stock['pnl'])
                        self.trading['mdd'].append(round((stock['min_t'] - stock['entry_price'])/stock['entry_price']*100, 2))
                        self.trading['mdu'].append(round((stock['max_t'] - stock['entry_price'])/stock['entry_price']*100, 2))
                        self.trading['last_high_elapsed'].append(0)
                        self.trading['msg'].append(stock['msg'])
                        self.trading['mkt_float'].append(1000)
                        self.trading['ytd_tradamt'].append(100)
                        self.trading['cbv_1'].append(stock.get('cbv_1', 0))
                        self.trading['ctotal'].append(stock.get('ctotal', 0))
                        self.trading['cum_amt'].append(100)
                        print(f"★ [매도 완료] 종목: {code}, PnL: {stock['pnl']}%, 사유: {exit_msg}")

                # 2. 진입 조건 체킹
                if check_entry_conditions(stock, t, SET_TIME):
                    stock['position'] = 1
                    stock['entry_t'] = t + 1 if t + 1 < len(stock['time']) else t
                    stock['entry_price'] = stock['high'][stock['entry_t']]
                    print(f"★ [매수 진입] 종목: {code}, 시간: {time_str}, 진입가: {stock['entry_price']}")

        except Exception as e:
            # 에러 원인 출력 (숨기지 않음!)
            print(f"❌ 종목 [{code}] 처리 중 에러 발생: {e}")
            traceback.print_exc()