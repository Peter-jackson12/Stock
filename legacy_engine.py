import os
import sqlite3
import sys
import time
import numpy as np
import pandas as pd

strategy = 'cross_Today'

# 0
# csv_path = 'C:\\Users\\user\\PycharmProjects\\data\\Daily\\long\\'
# sec_path = "C:\\Users\\user\\PycharmProjects\\data\\1sec\\old\\"

# 1
# csv_path = 'C:\\Users\\user\\PycharmProjects\\data\\Daily\\'
# sec_path = "C:\\Users\\user\\PycharmProjects\\data\\1sec\\new\\"

# 2
# csv_path = 'C:\\Users\\user\\PycharmProjects\\data\\Daily\\foward\\'
# sec_path = "C:\\Users\\user\\PycharmProjects\\data\\1sec\\foward\\"

# test
csv_path = '//DataCenter/Update/Daily/'
sec_path = "//DataCenter/주식호가캔들/1sec/"

total_data_list = os.listdir(sec_path)
total_data_list.sort(reverse=False)
total_data_list = total_data_list[:]


class SaveResult:

    def delete(self, part):
        csv_name = "{}_{}.csv".format(strategy, part)
        try:
            os.remove(csv_name)
        except:
            text = '[WinError 2] 지정된 파일을 찾을 수 없습니다: {}_testing.csv'.format(strategy)
            print(text)

    # @staticmethod
    def to_csv(self, trading_data, part):
        data = pd.DataFrame(trading_data)

        csv_name = "{}_{}.csv".format(strategy, part)
        data.to_csv(csv_name, encoding='euc-kr')
        pass


class BackTest:

    def __init__(self, part, split, test_date, test_code):

        self.sec_candle = None
        self.sec_data_list = None
        self.data = pd.DataFrame()
        self.csv_mkt = {}  # 시가총액
        self.csv_open = {}
        self.csv_close = {}
        self.csv_high = {}
        self.csv_low = {}
        self.csv_tradamt = {}  # 거래대금
        self.csv_float = {}  # 유통비율
        self.csv_shares = {}  # 주식 숫자
        self.start_index = -1
        self.end_index = -1
        self.today_index = -1
        self.set_time = 1300  # 1300 조건
        self.part = part
        self.split = split
        self.test_date = test_date
        self.test_code = test_code
        self.load_date_list = []

        # 백터스트 데이터 셋업
        self.stock = {}
        # 결과 저장 데이터 셋업
        self.trading = {}

    def initialize(self):
        SaveResult().delete(self.part)

        temp_date_list = []

        if type(self.test_date) == list:
            temp_date_list = self.test_date
        else:

            for file in total_data_list:

                if self.test_date is not None:

                    if type(self.test_date) == str and int(file[:8]) >= int(self.test_date):
                        temp_date_list.append(file)

                else:
                    if file + '.db' not in total_data_list and 'txt' not in file:
                        temp_date_list.append(file)

        length = int(len(temp_date_list) / self.split)
        load_date_list = temp_date_list[length * (self.part - 1):length * self.part] if self.part < self.split else temp_date_list[length * (self.part - 1):]

        self.load_date_list = load_date_list

        print(load_date_list)

    def Set_Record_Data(self):

        self.trading = {

            'pnl': [], 'mdd': [], 'mdu': [], 'last_high_elapsed': [],
            'today': [], 'name': [], 'starttime': [], 'trigger': [],
            't_open': [], 'entry_price': [], 'n': [],
            'exit_price': [], 'entry_time': [],
            'start_entry': [], 'entry_t': [], 'exit_time': [], 'time_spend': [], 'time_1': [],
            'tick_size': [], 'tick_rate': [], 'msg': [],
            'gap': [], 'topen_yhigh': [], 'topen_ylow': [], 'topen_yopen': [],
            'mkt_float': [], 'ytd_tradamt': [],
            'ytd_oh': [], 'ytd_oc': [], 'ytd_hc': [], 'ytd_lc': [], 'ytd_hl': [],

            'amt_5s': [], 'bamt_5s': [],
            'amt_10s': [], 'bamt_10s': [],
            'amt_30s': [], 'bamt_30s': [],
            'amt_60s': [], 'bamt_60s': [],

            'cbv_1': [], 't_max1buyratio': [], 'cross_vol': [], 'max1buyratio': [],

            'cvtotal': [], 'ctotal': [],
            'start_amt_60s': [], 'cum_amt': [],

            # ▼▼▼▼▼ 시작 캔들 관련 지표 추가 ▼▼▼▼▼
            'start_hl': [], 'start_oh': [], 'start_oc': [], 'start_hc': [], 'start_ol': [], 'start_lc': [],
            # ▲▲▲▲▲ 여기까지 추가 ▲▲▲▲▲

            'trigger_p_open': [], 'trigger_p_high': [], 'trigger_p_low': [], 'trigger_p_close': [],

            'max_bid_amt': [], 'max_bid_v': [],
            'sum_offer_amt': [], 'sum_offer_v': [],

            # === 변화량 ===
            'sum_bid_v_ch': [], 'sum_bid_amt_ch': [],
            'max_bid_v_ch': [], 'max_bid_amt_ch': [],
            'sum_offer_v_ch': [], 'sum_offer_amt_ch': [],

            'max_trigger': []

        }

        # windows 리스트를 사용해 관련 변수들을 한번에 추가
        windows = [5, 10, 30, 60]
        for w in windows:
            self.trading[f'cbv_{w}'] = []
            self.trading[f'max{w}buyratio'] = []
            self.trading[f't_max{w}buyratio'] = []

        # min/max_trigger 변수들을 한번에 추가
        trigger_windows = [1, 5, 10, 30, 60]
        for w in trigger_windows:
            self.trading[f'min{w}_trigger'] = []
            self.trading[f'max{w}_trigger'] = []

    def Data_load(self):

        start = time.time()
        self.initialize()

        start_date = self.load_date_list[0][:8]
        end_date = self.load_date_list[-1][:8]

        print('start_date', start_date, 'end date', end_date)

        self.Set_Record_Data()

        self.csv_mkt = pd.read_csv(csv_path + 'mkt.csv', encoding='CP949', low_memory=False)
        self.csv_open = pd.read_csv(csv_path + 'open.csv', encoding='CP949', low_memory=False)
        self.csv_high = pd.read_csv(csv_path + 'high.csv', encoding='CP949', low_memory=False)
        self.csv_low = pd.read_csv(csv_path + 'low.csv', encoding='CP949', low_memory=False)
        self.csv_tradamt = pd.read_csv(csv_path + 'tradamt.csv', encoding='CP949', low_memory=False)
        self.csv_close = pd.read_csv(csv_path + 'close.csv', encoding='CP949', low_memory=False)
        self.csv_float = pd.read_csv(csv_path + 'float.csv', encoding='CP949', low_memory=False)
        self.csv_shares = pd.read_csv(csv_path + 'shares.csv', encoding='CP949', low_memory=False)

        for i, date in enumerate(self.csv_open['Code']):
            if date == start_date:
                self.start_index = i
            if date == end_date:
                self.end_index = i
                break

        if self.start_index < 0 or self.end_index < 0:
            print('지정한 날짜를 csv 파일에서 찾을 수 없음')
            sys.exit()
        else:
            print('Complete Basic Data Load...... from <{}> to <{}>...'.format(self.csv_open['Code'][self.start_index], self.csv_open['Code'][self.end_index]))

        for i in range(self.start_index, self.end_index + 1):
            self.today_index = i
            today_date = self.csv_open['Code'][i]
            file_name = '{}_LOB.db'.format(today_date)
            path = sec_path + file_name

            if file_name in self.load_date_list:

                self.sec_candle = sqlite3.connect(path).cursor()
                self.sec_data_list = list(name[0] for name in self.sec_candle.execute("SELECT name FROM sqlite_master WHERE type='table';"))

                if self.test_code is not None:
                    self.Universe(self.test_code)
                    if len(self.stock) > 0:
                        self.Test_date_iteration(self.test_code)
                else:
                    sec_set = set(self.sec_data_list)
                    for code in self.csv_open.keys()[1:]:
                        if code[1:] in sec_set:
                            self.Universe(code)

                            if len(self.stock) > 0:
                                self.Test_date_iteration(code)

                end = time.time()
                print(self.csv_open['Code'][self.today_index], f"Data Iteration recall {end - start:.5f} sec")
            else:
                print('--------------{} DB파일 없음---------------'.format(path))

        SaveResult().to_csv(self.trading, self.part)

    def Universe(self, code):

        self.stock = {}

        name = self.csv_open[code][0]
        today = self.csv_open['Code'][self.today_index]

        if 'na' not in list(self.csv_high[code][max(self.today_index - 1, 0): self.today_index + 1]):

            shares = int(self.csv_shares[code][self.today_index - 1]) * float(self.csv_float[code][self.today_index - 1])
            mkt_float = int(float(self.csv_mkt[code][self.today_index - 1]) * float(self.csv_float[code][self.today_index - 1]) / 100)
            ytd_tradamt = round(float(self.csv_tradamt[code][self.today_index - 1]), 2)

            ytd_oh = round(100 * (int(self.csv_high[code][self.today_index - 1]) / int(self.csv_open[code][self.today_index - 1]) - 1), 3)
            ytd_hc = round(100 * (int(self.csv_close[code][self.today_index - 1]) / int(self.csv_high[code][self.today_index - 1]) - 1), 3)
            ytd_lc = round(100 * (int(self.csv_close[code][self.today_index - 1]) / int(self.csv_low[code][self.today_index - 1]) - 1), 3)
            ytd_oc = round(100 * (int(self.csv_close[code][self.today_index - 1]) / int(self.csv_open[code][self.today_index - 1]) - 1), 3)
            ytd_hl = round(100 * (int(self.csv_high[code][self.today_index - 1]) / int(self.csv_low[code][self.today_index - 1]) - 1), 3)

            # 진입 조건 설정(일봉 전략1)
            #

            if 550 < mkt_float < 14500 and ytd_tradamt > 40 and ytd_oh < 8:

                self.data = pd.DataFrame(self.sec_candle.execute("SELECT * FROM '{}'".format(code[1:])))
                correction = np.array(self.data[1][0]) / int(self.csv_open[code][self.today_index])
                t_open = int(self.csv_open[code][self.today_index]) * correction
                y_open = int(self.csv_open[code][self.today_index - 1]) * correction
                y_close = int(self.csv_close[code][self.today_index - 1]) * correction
                y_high = int(self.csv_high[code][self.today_index - 1]) * correction
                y_low = int(self.csv_low[code][self.today_index - 1]) * correction

                upper = self.calculate_upperlimit(y_close, today)

                gap = round(100 * (t_open / y_close - 1), 3)
                topen_yhigh = round(100 * (t_open / y_high - 1), 3)
                topen_ylow = round(100 * (t_open / y_low - 1), 3)
                topen_yopen = round(100 * (t_open / y_open - 1), 3)

                # 진입 조건 설정(일봉 전략2)
                #

                if True:

                    self.stock.update({code: {

                        'today': today, 'name': name, 'shares': shares,
                        't_open': t_open, 'y_high': y_high, 'y_low': y_low, 'y_open': y_open,
                        'y_close': y_close, 'upper': upper,
                        'gap': gap, 'topen_yhigh': topen_yhigh, 'topen_ylow': topen_ylow, 'topen_yopen': topen_yopen,
                        'mkt_float': mkt_float, 'ytd_tradamt': ytd_tradamt,
                        'ytd_oh': ytd_oh, 'ytd_oc': ytd_oc, 'ytd_hc': ytd_hc, 'ytd_lc': ytd_lc,
                        'ytd_hl': ytd_hl,

                        # 분봉
                        'candle_num': [], 'candle_time': [], 'candle_open': [], 'candle_high': [], 'candle_low': [], 'candle_close': [],
                        'candle_vol': [], 'candle_tick': [], 'candle_buy_vol': [], 'candle_sell_vol': [], 'candle_amt': [],

                        'high_time': [], 'high_index': [],

                        'time': [], 'open': [], 'high': [], 'low': [], 'close': [],
                        'vol': [], 'buy_vol': [], 'sell_vol': [],
                        'tick': [], 'buy_tick': [], 'sell_tick': [],
                        'offer_vol': [], 'bid_price': [], 'bid_vol': [],

                        'position': 0, 'state': 0, 'n': 0, 'entry_t': 0, 'exit_t': 0,
                        'entry_price': 0, 'qty': 0,
                        'exit_time': 0, 'exit_price': 0, 'time_spend': 0, 'time_1': 0,
                        'max_bid_amt': 0, 'sum_offer_v': 0, 'sum_offer_amt': 0,
                        'max_bid_v': 0, 'max_bid_v_index': 0, 'max_bid_p': 0,

                        'tmax_cbv1': 0,
                        'max_cross_buy_vol': 0,
                        'max1buyratio': 0, 't_max1buyratio': 0,

                        'entry_trigger_cbv_1': 0,
                        'entry_trigger_t_max1buyratio': 0,

                        # ▼▼▼▼▼ 시작 캔들 관련 지표 초기화 추가 ▼▼▼▼▼
                        'start_hl': 0, 'start_oh': 0, 'start_oc': 0, 'start_hc': 0, 'start_ol': 0, 'start_lc': 0,
                        # ▲▲▲▲▲ 여기까지 추가 ▲▲▲▲▲

                        'trigger_p_open': 0, 'trigger_p_high': 0, 'trigger_p_low': 0, 'trigger_p_close': 0,

                        'pnl': 0, 'tax': 0, 'net': 0, 'msg': 0, 'max_t': 0, 'min_t': 9999999999, 'mdd': 0, 'mdu': 0, 'last_high_elapsed': 0,
                        'max_trigger': 0
                    }})

                    # 루프를 사용하여 관련 변수들을 일괄 초기화
                    windows = [5, 10, 30, 60]
                    for w in windows:
                        self.stock[code][f'cbv_{w}'] = 0
                        self.stock[code][f'max_cbv{w}'] = 0
                        self.stock[code][f'tmax_cbv{w}'] = 0
                        self.stock[code][f'max{w}buyratio'] = 0
                        self.stock[code][f't_max{w}buyratio'] = 0
                        self.stock[code][f'entry_trigger_cbv_{w}'] = 0
                        self.stock[code][f'entry_trigger_max{w}buyratio'] = 0
                        self.stock[code][f'entry_trigger_t_max{w}buyratio'] = 0

                    # min/max_trigger 임시 변수 초기화
                    trigger_windows = [1, 5, 10, 30, 60]
                    for w in trigger_windows:
                        self.stock[code][f'min{w}_trigger'] = 0
                        self.stock[code][f'max{w}_trigger'] = 0

                    self.stock[code]['time'] = np.array(self.data[0])
                    self.stock[code]['open'] = np.array(self.data[1])
                    self.stock[code]['high'] = np.array(self.data[2])
                    self.stock[code]['low'] = np.array(self.data[3])
                    self.stock[code]['close'] = np.array(self.data[4])
                    self.stock[code]['vol'] = np.array(self.data[5])
                    self.stock[code]['buy_vol'] = np.array(self.data[6])
                    self.stock[code]['sell_vol'] = np.array(self.data[7])
                    self.stock[code]['tick'] = np.array(self.data[8])
                    self.stock[code]['buy_tick'] = np.array(self.data[9])
                    self.stock[code]['sell_tick'] = np.array(self.data[10])

    def Test_date_iteration(self, code):

        for t in range(len(self.stock[code]['time'])):
            # 분봉생성
            self.Control_Candle(code, t)

            # 청산전략 시간
            self.Control_Stop(code, t)

            # 진입전략
            self.Control_Strategy(code, t)

            # 결과저장
            self.Control_State(code)

            if self.stock[code]['position'] == 0 and t > self.set_time:
                break

    def Control_Candle(self, code, t):

        candle_time = int(self.stock[code]['time'][t][:4])

        # 캔들 생성
        if len(self.stock[code]['candle_time']) == 0 or self.stock[code]['candle_time'][-1] < candle_time:

            self.stock[code]['candle_time'].append(candle_time)
            self.stock[code]['candle_open'].append(self.stock[code]['open'][t])
            self.stock[code]['candle_high'].append(self.stock[code]['high'][t])
            self.stock[code]['candle_low'].append(self.stock[code]['low'][t])
            self.stock[code]['candle_close'].append(self.stock[code]['close'][t])
            self.stock[code]['candle_vol'].append(self.stock[code]['vol'][t])
            self.stock[code]['candle_buy_vol'].append(self.stock[code]['buy_vol'][t])
            self.stock[code]['candle_sell_vol'].append(self.stock[code]['sell_vol'][t])
            self.stock[code]['candle_tick'].append(self.stock[code]['tick'][t])
            self.stock[code]['candle_amt'].append(self.stock[code]['vol'][t] * self.stock[code]['close'][t])
            self.stock[code]['high_time'].append(self.stock[code]['time'][t])
            self.stock[code]['high_index'].append(t)


        # 캔들 업데이트

        elif self.stock[code]['candle_time'][-1] == candle_time:

            self.stock[code]['candle_close'][-1] = self.stock[code]['close'][t]
            self.stock[code]['candle_vol'][-1] += self.stock[code]['vol'][t]
            self.stock[code]['candle_buy_vol'][-1] += self.stock[code]['buy_vol'][t]
            self.stock[code]['candle_sell_vol'][-1] += self.stock[code]['sell_vol'][t]
            self.stock[code]['candle_tick'][-1] += self.stock[code]['tick'][t]
            self.stock[code]['candle_amt'][-1] += self.stock[code]['vol'][t] * self.stock[code]['close'][t]

            if self.stock[code]['candle_high'][-1] < self.stock[code]['high'][t]:
                self.stock[code]['candle_high'][-1] = self.stock[code]['high'][t]
                self.stock[code]['high_time'][-1] = self.stock[code]['time'][t]
                self.stock[code]['high_index'][-1] = t

            if self.stock[code]['candle_low'][-1] > self.stock[code]['low'][t]:
                self.stock[code]['candle_low'][-1] = self.stock[code]['low'][t]

    def Control_Stop(self, code, t):

        if self.stock[code]['position'] == 1 and self.stock[code]['state'] == 0 and t - self.stock[code]['entry_t'] > 0:

            time_spend = self.calculate_time_spread('second', self.stock[code]['time'][self.stock[code]['entry_t']], self.stock[code]['time'][t])

            self.stock[code]['max_t'] = self.stock[code]['high'][t] if self.stock[code]['high'][t] > self.stock[code]['max_t'] else self.stock[code]['max_t']
            self.stock[code]['min_t'] = self.stock[code]['low'][t] if self.stock[code]['low'][t] < self.stock[code]['min_t'] else self.stock[code]['min_t']

            exit_msg = None

            if t >= len(self.stock[code]['time']):
                exit_msg = 'time_2'
            elif int(self.stock[code]['time'][t]) > 152060:
                exit_msg = 'time_1'
            elif self.stock[code]['high'][t] >= self.stock[code]['upper']:
                exit_msg = 'upper'
            elif self.stock[code]['low'][t] < self.stock[code]['max_t'] * 0.94:
                exit_msg = 'los'
            elif len(self.stock[code]['candle_high']) > 1:
                lookback = min(len(self.stock[code]['candle_low']) - 1, 2)  # 최소 1개, 최대 2개 참조
                if self.stock[code]['low'][t] <= min(self.stock[code]['candle_low'][-(lookback + 1):-1]):
                    exit_msg = 'trail'

            if exit_msg:
                self.stock[code]['exit_t'] = t
                self.stock[code]['exit_time'] = self.stock[code]['time'][t]
                self.stock[code]['exit_price'] = min(self.stock[code]['low'][t:t + 2])
                self.stock[code]['pnl'] = round((self.stock[code]['exit_price'] - self.stock[code]['entry_price']) / self.stock[code]['entry_price'] * 100, 3) - 0.23
                self.stock[code]['mdd'] = round((self.stock[code]['min_t'] - self.stock[code]['entry_price']) / self.stock[code]['entry_price'] * 100, 3)
                self.stock[code]['mdu'] = round((self.stock[code]['max_t'] - self.stock[code]['entry_price']) / self.stock[code]['entry_price'] * 100, 3)

                self.stock[code]['time_spend'] = time_spend
                self.stock[code]['msg'] = exit_msg
                self.stock[code]['position'] = 0
                self.stock[code]['state'] = 1
                self.stock[code]['max_t'] = 0
                self.stock[code]['min_t'] = 99999999

    def Control_Strategy(self, code, t):

        stock = self.stock[code]

        # 미리 윈도우 리스트와 로컬 stock 변수 선언
        windows = [5, 10, 30, 60]
        stock['cbv_1'] = 0
        for w in windows:
            # t > w 인 경우에만 계산
            if t > w:
                # 해당 윈도우 동안의 초당 매수량
                stock[f'cbv_{w}'] = sum(stock['buy_vol'][t - w: t]) / w

                # 전체 최대값(max_cbv_{w}) 갱신
                if stock[f'cbv_{w}'] > stock[f'max_cbv{w}']:
                    stock[f'max_cbv{w}'] = stock[f'cbv_{w}']
                # 비율 계산
                if stock[f'max_cbv{w}'] != 0:
                    stock[f'max{w}buyratio'] = round(
                        stock[f'cbv_{w}'] / stock[f'max_cbv{w}'], 3
                    )

            # t > w 이고, 당일 고가가 시가 기준 이상일 때 별도 최대치 갱신
            if t > w and stock['high'][t] >= stock['open'][0]:
                # tmax_cbv{w} 갱신
                if stock[f'cbv_{w}'] > stock[f'tmax_cbv{w}']:
                    stock[f'tmax_cbv{w}'] = stock[f'cbv_{w}']
                # t_max{w}buyratio 계산
                if stock[f'tmax_cbv{w}'] != 0:
                    stock[f't_max{w}buyratio'] = round(
                        stock[f'cbv_{w}'] / stock[f'tmax_cbv{w}'], 3
                    )

        if t > 0 and stock['high'][t] >= stock['open'][0]:
            stock['cbv_1'] = stock['buy_vol'][t]

            if stock['cbv_1'] > stock['tmax_cbv1']:
                stock['tmax_cbv1'] = stock['cbv_1']
            if stock['tmax_cbv1'] != 0:
                stock['t_max1buyratio'] = round(stock['cbv_1'] / stock['tmax_cbv1'], 3)

        #   조건

        if 90100 <= int(stock['time'][t]) < 140000 and len(stock['candle_high']) > 1 and 5 < t <= self.set_time \
                and stock['position'] == 0 and stock['state'] == 0 and t + 1 < len(stock['time']):

            trigger = stock['open'][0]
            stock['trigger'] = trigger

            stock['starttime'] = stock['time'][0]
            stock['entry_t'] = t + 1
            stock['entry_time'] = stock['time'][t + 1]

            start_entry = self.calculate_time_spread('second', stock['starttime'], stock['entry_time'])
            stock['start_entry'] = start_entry
            stock['time_1'] = self.calculate_time_spread('second', stock['time'][t - 1], stock['time'][t])

            tick_size = self.calculate_ticksize(trigger, self.csv_open['Code'][self.today_index])
            stock['tick_size'] = tick_size
            stock['tick_rate'] = round(tick_size / trigger * 100, 3)

            # 틱 조건 and stock['tick_rate'] < 0.3

            if stock['upper'] > trigger \
                    and stock['high'][t] >= trigger > stock['high'][t - 1]\
                    and stock['tick_rate'] <= 0.18:

                time_spread = self.calculate_time_spread('second', stock['time'][0], stock['time'][t])

                # 시가 볼륨 처리
                if stock['gap'] == 0:
                    stock['sell_vol'][0] = stock['buy_vol'][0] / 2 + stock['sell_vol'][0]
                    stock['buy_vol'][0] = stock['buy_vol'][0] / 2

                elif stock['gap'] < 0:

                    stock['sell_vol'][0] = stock['buy_vol'][0] + stock['sell_vol'][0]
                    stock['buy_vol'][0] = 0

                #   vi볼륨 처리
                if self.calculate_time_spread('second', stock['time'][t - 1], stock['time'][t]) >= 120:

                    if stock['close'][t - 1] == stock['open'][t]:
                        stock['sell_vol'][t] = stock['buy_vol'][t] / 2 + stock['sell_vol'][t]
                        stock['buy_vol'][t] = stock['buy_vol'][t] / 2

                    elif stock['close'][t - 1] > stock['open'][t]:
                        stock['sell_vol'][t] = stock['buy_vol'][t] + stock['sell_vol'][t]
                        stock['buy_vol'][t] = 0

                stock['cross_vol'] = stock['vol'][t]
                stock['max_cross_buy_vol'] = max(stock['buy_vol'][1:t]) + 1  # 0으로 나누는거 방지용
                stock['max1buyratio'] = round(stock['cbv_1'] / stock['max_cross_buy_vol'], 3)

                stock = self.stock[code]

                for w in windows:
                    t_ws = max(0, t - w)
                    # 총 거래량, 매수 거래량, 평균 가격 계산
                    vol_ws = sum(stock['vol'][t_ws:t])
                    bvol_ws = sum(stock['buy_vol'][t_ws:t])
                    price_ws = np.mean(stock['close'][t_ws:t])

                    # 시간 스프레드 대비 정규화
                    norm_factor = min(w, time_spread) if time_spread > 0 else 1

                    # stock 딕셔너리에 amt_10s, bamt_10s 등으로 저장
                    stock[f'amt_{w}s'] = vol_ws * price_ws / 10000 / norm_factor
                    stock[f'bamt_{w}s'] = bvol_ws * price_ws / 10000 / norm_factor

                stock['cvtotal'] = sum(stock['vol'][:t]) / time_spread
                stock['ctotal'] = sum(stock['tick'][:t]) / time_spread

                # min/max_trigger 계산 로직
                trigger_windows = [1, 5, 10, 30, 60]
                for w in trigger_windows:
                    if t > w:  # 계산할 과거 데이터가 충분한지 확인
                        # 과거 w초 동안의 최저가와 최고가
                        min_price_w = min(stock['low'][t - w: t])
                        max_price_w = max(stock['high'][t - w: t])

                        # 0으로 나누는 것을 방지
                        if min_price_w > 0:
                            stock[f'min{w}_trigger'] = round((trigger / min_price_w - 1) * 100, 3)
                        if max_price_w > 0:
                            stock[f'max{w}_trigger'] = round((trigger / max_price_w - 1) * 100, 3)

                stock['max_trigger'] = round((trigger / max(stock['high'][:t]) - 1) * 100, 3)

                stock['start_amt_60s'] = stock['candle_amt'][0] / 10000 / 10000
                stock['cum_amt'] = sum(stock['candle_amt']) / 10000 / 10000

                # ▼▼▼▼▼ 시작 캔들(1분봉) 관련 지표 계산 ▼▼▼▼▼
                c_open0 = stock['candle_open'][0]
                c_high0 = stock['candle_high'][0]
                c_low0 = stock['candle_low'][0]
                c_close0 = stock['candle_close'][0]

                stock['start_hl'] = round(100 * (c_high0 / c_low0 - 1), 3)
                stock['start_oh'] = round(100 * (c_high0 / c_open0 - 1), 3)
                stock['start_oc'] = round(100 * (c_close0 / c_open0 - 1), 3)
                stock['start_hc'] = round(100 * (c_close0 / c_high0 - 1), 3)
                stock['start_ol'] = round(100 * (c_low0 / c_open0 - 1), 3)
                stock['start_lc'] = round(100 * (c_close0 / c_low0 - 1), 3)
                # ▲▲▲▲▲ 여기까지 계산 ▲▲▲▲▲

                # 이전 봉의 시가, 고가, 저가, 종가와 trigger 가격의 비율 계산
                stock['trigger_p_open'] = round(100 * (trigger / stock['candle_open'][-2] - 1), 3)
                stock['trigger_p_high'] = round(100 * (trigger / stock['candle_high'][-2] - 1), 3)
                stock['trigger_p_low'] = round(100 * (trigger / stock['candle_low'][-2] - 1), 3)
                stock['trigger_p_close'] = round(100 * (trigger / stock['candle_close'][-2] - 1), 3)

                # ───────────────────────────────────────────────
                # 1단계: high[t] 와 같은 값이 과거에 있었는가?
                # ───────────────────────────────────────────────
                target_price = max(stock['high'][:t])
                last_high_t = None

                search_array = stock['high'][:t - 1]
                indices = np.where(search_array == target_price)[0]

                if indices.size > 0:
                    last_high_t = indices[-1]

                # ───────────────────────────────────────────────
                # 거리 계산 & 결과 저장
                # ───────────────────────────────────────────────
                if last_high_t is None:
                    time_since_last_high = 9999
                else:
                    time_since_last_high = t - last_high_t

                stock['last_high_elapsed'] = time_since_last_high

                # 진입 조건 설정(초봉 전략)
                #                           \

                if stock['max10_trigger'] > 0\
                        and 2.3 < stock['ctotal'] < 31 \
                        and stock['t_max1buyratio'] > 0.1 \
                        and stock['max60_trigger'] > - 0.35 \
                        and stock['max60buyratio'] > 0.6\
                        and stock['cbv_5'] > 50 \
                        and stock['amt_10s'] > 700\
                        and start_entry <= self.set_time:

                    # === t 시점 ===
                    offer_p_list = list(self.data.iloc[t][11:21])
                    offer_vol_list = list(self.data.iloc[t][21:31])

                    stock['sum_offer_v'] = sum(offer_vol_list)
                    stock['sum_offer_amt'] = sum([vol * p for vol, p in zip(offer_vol_list, offer_p_list)]) / 10000

                    bid_p_list = list(self.data.iloc[t][31:41])
                    bid_vol_list = list(self.data.iloc[t][41:])
                    max_bid_v = max(bid_vol_list)
                    max_bid_v_index = bid_vol_list.index(max_bid_v)
                    max_bid_p = bid_p_list[max_bid_v_index]

                    stock['sum_bid_v'] = sum(bid_vol_list)
                    stock['sum_bid_amt'] = sum([vol * p for vol, p in zip(bid_vol_list, bid_p_list)]) / 10000
                    stock['max_bid_v'] = max_bid_v
                    stock['max_bid_v_index'] = max_bid_v_index
                    stock['max_bid_p'] = max_bid_p
                    stock['max_bid_amt'] = (max_bid_v * max_bid_p) / 10000

                    # === t-1 시점 ===
                    offer_vol_list_t1 = list(self.data.iloc[t - 1][21:31])
                    offer_p_list_t1 = list(self.data.iloc[t - 1][11:21])

                    stock['sum_offer_v_t1'] = sum(offer_vol_list_t1)

                    bid_p_list_t1 = list(self.data.iloc[t - 1][31:41])
                    bid_vol_list_t1 = list(self.data.iloc[t - 1][41:])
                    max_bid_v_t1 = max(bid_vol_list_t1)
                    max_bid_v_index_t1 = bid_vol_list_t1.index(max_bid_v_t1)
                    max_bid_p_t1 = bid_p_list_t1[max_bid_v_index_t1]

                    stock['sum_bid_v_t1'] = sum(bid_vol_list_t1)
                    stock['sum_bid_amt_t1'] = sum([vol * p for vol, p in zip(bid_vol_list_t1, bid_p_list_t1)]) / 10000
                    stock['max_bid_v_t1'] = max_bid_v_t1
                    stock['max_bid_p_t1'] = max_bid_p_t1
                    stock['max_bid_amt_t1'] = (max_bid_v_t1 * max_bid_p_t1) / 10000
                    stock['sum_offer_amt_t1'] = sum([vol * p for vol, p in zip(offer_vol_list_t1, offer_p_list_t1)]) / 10000

                    # === 변화량 계산 ===
                    stock['sum_bid_v_ch'] = stock['sum_bid_v'] - stock['sum_bid_v_t1']
                    stock['sum_bid_amt_ch'] = stock['sum_bid_amt'] - stock['sum_bid_amt_t1']
                    stock['max_bid_v_ch'] = stock['max_bid_v'] - stock['max_bid_v_t1']
                    stock['max_bid_amt_ch'] = stock['max_bid_amt'] - stock['max_bid_amt_t1']
                    stock['sum_offer_v_ch'] = stock['sum_offer_v'] - stock['sum_offer_v_t1']
                    stock['sum_offer_amt_ch'] = stock['sum_offer_amt'] - stock['sum_offer_amt_t1']

                    # 호가 조건
                    # and stock['sum_offer_v_ch'] < 6000 and max_bid_amt stock['sum_offer_amt'] > 22000

                    if stock['sum_offer_amt'] > 22000:

                        for w in windows:
                            stock[f'entry_trigger_cbv_{w}'] = stock[f'cbv_{w}']
                            stock[f'entry_trigger_max{w}buyratio'] = stock[f'max{w}buyratio']
                            stock[f'entry_trigger_t_max{w}buyratio'] = stock[f't_max{w}buyratio']
                        stock['entry_trigger_cbv_1'] = stock['cbv_1']
                        stock['entry_trigger_t_max1buyratio'] = stock['t_max1buyratio']
                        stock['position'] = 1
                        stock['n'] += 1
                        stock['entry_price'] = stock['high'][t + 1]

        self.stock[code] = stock

    def Control_State(self, code):

        if self.stock[code]['position'] == 0 and self.stock[code]['state'] == 1:
            self.trading['today'].append(self.stock[code]['today'])
            self.trading['name'].append(self.stock[code]['name'])
            self.trading['starttime'].append(self.stock[code]['starttime'])
            self.trading['t_open'].append(self.stock[code]['t_open'])
            self.trading['trigger'].append(self.stock[code]['trigger'])

            self.trading['tick_size'].append(self.stock[code]['tick_size'])
            self.trading['tick_rate'].append(self.stock[code]['tick_rate'])

            self.trading['pnl'].append(self.stock[code]['pnl'])
            self.trading['mdd'].append(self.stock[code]['mdd'])
            self.trading['mdu'].append(self.stock[code]['mdu'])
            self.trading['last_high_elapsed'].append(self.stock[code]['last_high_elapsed'])
            self.trading['msg'].append(self.stock[code]['msg'])

            self.trading['ytd_oh'].append(self.stock[code]['ytd_oh'])
            self.trading['ytd_oc'].append(self.stock[code]['ytd_oc'])
            self.trading['ytd_hc'].append(self.stock[code]['ytd_hc'])
            self.trading['ytd_lc'].append(self.stock[code]['ytd_lc'])

            self.trading['ytd_hl'].append(self.stock[code]['ytd_hl'])

            self.trading['n'].append(self.stock[code]['n'])

            self.trading['entry_time'].append(self.stock[code]['entry_time'])
            self.trading['start_entry'].append(self.stock[code]['start_entry'])

            self.trading['entry_t'].append(self.stock[code]['entry_t'])

            self.trading['entry_price'].append(self.stock[code]['entry_price'])
            self.trading['exit_time'].append(self.stock[code]['exit_time'])
            self.trading['exit_price'].append(self.stock[code]['exit_price'])
            self.trading['time_spend'].append(self.stock[code]['time_spend'])
            self.trading['time_1'].append(self.stock[code]['time_1'])

            self.trading['cross_vol'].append(self.stock[code]['cross_vol'])
            self.trading['max1buyratio'].append(self.stock[code]['max1buyratio'])

            self.trading['cbv_1'].append(self.stock[code]['entry_trigger_cbv_1'])
            self.trading['t_max1buyratio'].append(self.stock[code]['entry_trigger_t_max1buyratio'])

            windows = [5, 10, 30, 60]
            for w in windows:
                # amt_?s, bamt_?s 키로 trading에 추가
                self.trading[f'amt_{w}s'].append(self.stock[code][f'amt_{w}s'])
                self.trading[f'bamt_{w}s'].append(self.stock[code][f'bamt_{w}s'])
                self.trading[f'cbv_{w}'].append(self.stock[code][f'entry_trigger_cbv_{w}'])
                self.trading[f'max{w}buyratio'].append(self.stock[code][f'entry_trigger_max{w}buyratio'])
                self.trading[f't_max{w}buyratio'].append(self.stock[code][f'entry_trigger_t_max{w}buyratio'])

            self.trading['cvtotal'].append(self.stock[code]['cvtotal'])
            self.trading['ctotal'].append(self.stock[code]['ctotal'])

            # min/max_trigger 결과 저장
            trigger_windows = [1, 5, 10, 30, 60]
            for w in trigger_windows:
                self.trading[f'min{w}_trigger'].append(self.stock[code][f'min{w}_trigger'])
                self.trading[f'max{w}_trigger'].append(self.stock[code][f'max{w}_trigger'])

            self.trading['max_trigger'].append(self.stock[code]['max_trigger'])

            self.trading['start_amt_60s'].append(self.stock[code]['start_amt_60s'])
            self.trading['cum_amt'].append(self.stock[code]['cum_amt'])

            # ▼▼▼▼▼ 시작 캔들 관련 지표 저장 ▼▼▼▼▼
            self.trading['start_hl'].append(self.stock[code]['start_hl'])
            self.trading['start_oh'].append(self.stock[code]['start_oh'])
            self.trading['start_oc'].append(self.stock[code]['start_oc'])
            self.trading['start_hc'].append(self.stock[code]['start_hc'])
            self.trading['start_ol'].append(self.stock[code]['start_ol'])
            self.trading['start_lc'].append(self.stock[code]['start_lc'])
            # ▲▲▲▲▲ 여기까지 저장 ▲▲▲▲▲

            self.trading['trigger_p_open'].append(self.stock[code]['trigger_p_open'])
            self.trading['trigger_p_high'].append(self.stock[code]['trigger_p_high'])
            self.trading['trigger_p_low'].append(self.stock[code]['trigger_p_low'])
            self.trading['trigger_p_close'].append(self.stock[code]['trigger_p_close'])

            self.trading['max_bid_amt'].append(self.stock[code]['max_bid_amt'])
            self.trading['max_bid_v'].append(self.stock[code]['max_bid_v'])
            self.trading['sum_offer_amt'].append(self.stock[code]['sum_offer_amt'])
            self.trading['sum_offer_v'].append(self.stock[code]['sum_offer_v'])

            # === 변화량 데이터 ===
            self.trading['sum_bid_v_ch'].append(self.stock[code]['sum_bid_v_ch'])
            self.trading['sum_bid_amt_ch'].append(self.stock[code]['sum_bid_amt_ch'])

            self.trading['max_bid_v_ch'].append(self.stock[code]['max_bid_v_ch'])
            self.trading['max_bid_amt_ch'].append(self.stock[code]['max_bid_amt_ch'])
            self.trading['sum_offer_v_ch'].append(self.stock[code]['sum_offer_v_ch'])
            self.trading['sum_offer_amt_ch'].append(self.stock[code]['sum_offer_amt_ch'])

            self.trading['gap'].append(self.stock[code]['gap'])
            self.trading['topen_yhigh'].append(self.stock[code]['topen_yhigh'])
            self.trading['topen_ylow'].append(self.stock[code]['topen_ylow'])
            self.trading['topen_yopen'].append(self.stock[code]['topen_yopen'])

            self.trading['mkt_float'].append(self.stock[code]['mkt_float'])
            self.trading['ytd_tradamt'].append(self.stock[code]['ytd_tradamt'])
            self.stock[code]['state'] = 0

    @staticmethod
    def calculate_ticksize(price, back_test_date):

        if int(back_test_date) < 20230125:
            if price >= 500000:
                tick_size = 1000
            elif price >= 100000:
                tick_size = 500
            elif price >= 50000:
                tick_size = 100
            elif price >= 10000:
                tick_size = 50
            elif price >= 5000:
                tick_size = 10
            elif price >= 1000:
                tick_size = 5
            else:
                tick_size = 1
        else:
            if price >= 500000:
                tick_size = 1000
            elif price >= 200000:
                tick_size = 500
            elif price >= 50000:
                tick_size = 100
            elif price >= 20000:
                tick_size = 50
            elif price >= 5000:
                tick_size = 10
            elif price >= 2000:
                tick_size = 5
            else:
                tick_size = 1

        return tick_size

    def calculate_upperlimit(self, price, today):
        upper = price * 1.3

        tick_size = self.calculate_ticksize(upper, today)

        num = int(upper / tick_size)
        upper = num * tick_size
        return upper

    def handle_logging(self, message):
        # 간단히 콘솔에 출력하거나, 파일에 기록할 수 있습니다.
        print(f"[LOGGING] {message}")

    def calculate_time_spread(self, unit, start_time, end_time):

        if unit == 'second':
            interval_time = int((int(end_time[0:2]) - int(start_time[0:2])) * 3600
                                + (int(end_time[2:4]) - int(start_time[2:4])) * 60
                                + (int(end_time[4:6]) - int(start_time[4:6])))
            return interval_time

        elif unit == 'minute':
            interval_time = int((int(end_time[0:2]) - int(start_time[0:2])) * 60
                                + int(end_time[2:4]) - int(start_time[2:4]))
            return interval_time

        else:
            self.handle_logging('WARNING! calculate_time_spread ERROR: Invalid unit provided.')
            return None