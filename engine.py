import os
import sqlite3
import sys
import time
import numpy as np
import pandas as pd

strategy = 'CrosPhigh'

# 0
# csv_path = 'C:\\Users\\user\\PycharmProjects\\data\\Daily\\long\\'
# sec_path = "C:\\Users\\user\\PycharmProjects\\data\\1sec\\old\\"

# 1
# csv_path = 'C:\\Users\\user\\PycharmProjects\\data\\Daily\\'
# sec_path = "C:\\Users\\user\\PycharmProjects\\data\\1sec\\new\\"

# 2
csv_path = 'C:\\Users\\user\\PycharmProjects\\data\\Daily\\foward\\'
sec_path = "C:\\Users\\user\\PycharmProjects\\data\\1sec\\foward\\"

# test
# csv_path = '//DataCenter/Update/Daily/'
# sec_path = "//DataCenter/주식호가캔들/1sec/"

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
        self.set_time = 6000  # 조건 6000
        self.part = part
        self.split = split
        self.test_date = test_date
        self.test_code = test_code
        self.load_date_list = []
        self.windows_all = [1, 5, 10, 30]

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
                    #                    if type(self.test_date) == str and int(file[:8]) > int(self.test_date):

                    if type(self.test_date) == str and int(file[:8]) > int(self.test_date):
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
            'ytd_oh': [], 'ytd_oc': [], 'ytd_hc': [], 'ytd_lc': [], 'ytd_hl': [], 'inc3': [],

            'max_cbv': [], 'max_tick': [],
            'cvtotal': [], 'cbvtotal': [], 'ctotal': [], 'cbtotal': [],

            'p_amt': [], 'p_bamt': [], 'p_tick': [], 'cum_amt': [],
            'p_vol': [], 'p_bvol': [], 'p_max': [], 'p_ratio': [],
            # ▼▼▼▼▼ pp_ (3봉 집계) 변수 추가 ▼▼▼▼▼
            'pp_amt': [], 'pp_bamt': [], 'pp_tick': [], 'pp_vol': [], 'pp_bvol': [],
            # ▼▼▼▼▼ p12_ (5봉 집계) 변수 추가 ▼▼▼▼▼
            'p12_amt': [], 'p12_bamt': [], 'p12_tick': [], 'p12_vol': [], 'p12_bvol': [],
            'p18_amt': [], 'p18_bamt': [], 'p18_tick': [], 'p18_vol': [], 'p18_bvol': [],

            # ▼▼▼▼▼ p1_ (현재 봉) 관련 추가 ▼▼▼▼▼
            'p1_amt': [], 'p1_bamt': [], 'p1_tick': [], 'p1_vol': [], 'p1_bvol': [],
            'p1_hl': [], 'p1_oh': [], 'p1_oc': [], 'p1_hc': [], 'p1_ol': [], 'p1_lc': [],
            # ▲▲▲▲▲ p1_ (현재 봉) 관련 추가 ▲▲▲▲▲

            # ▼▼▼▼▼ 이전 캔들 관련 지표 추가 ▼▼▼▼▼
            't_hl': [], 't_oh': [], 't_oc': [], 't_hc': [], 't_ol': [], 't_lc': [],
            'p_hl': [], 'p_oh': [], 'p_oc': [], 'p_hc': [], 'p_ol': [], 'p_lc': [],
            'pp_hl': [], 'pp_oh': [], 'pp_oc': [], 'pp_hc': [], 'pp_ol': [], 'pp_lc': [],
            'p12_hl': [], 'p12_oh': [], 'p12_oc': [], 'p12_hc': [], 'p12_ol': [], 'p12_lc': [],
            'p18_hl': [], 'p18_oh': [], 'p18_oc': [], 'p18_hc': [], 'p18_ol': [], 'p18_lc': [],

            # ▼▼▼▼▼ 누적 지표 추가 ▼▼▼▼▼
            'c_hl': [], 'c_oh': [], 'c_oc': [], 'c_hc': [], 'c_ol': [], 'c_lc': [],

            # 'trigger_p_open': [], 'trigger_p_low': [], 'trigger_p_close': [], 'trigger_p_high': [],
            'sum_bid_amt': [], 'sum_bid_v': [], 'max_bid_amt': [], 'max_bid_v': [], 'max_bid_trigger': [],
            'sum_offer_amt': [], 'sum_offer_v': [], 'max_offer_amt': [], 'max_offer_v': [], 'max_offer_trigger': [],

            # === 변화량 ===
            'sum_bid_v_ch': [], 'sum_bid_amt_ch': [],
            'max_bid_v_ch': [], 'max_bid_amt_ch': [],
            'sum_offer_v_ch': [], 'sum_offer_amt_ch': [],

            'open_trigger': [], 'high_trigger': [], 'low_trigger': [], 'close_trigger': [],
            'yopen_trigger': [], 'yhigh_trigger': [], 'ylow_trigger': [], 'yclose_trigger': [],

            'max_trigger': [], 'min_trigger': [],
            # ▼▼▼▼▼ t-1 시점 (직전 1초) 데이터 추가 ▼▼▼▼▼
            'p1s_vol': [], 'p1s_cbv_1': [], 'p1s_amt_1s': [], 'p1s_bamt_1s': [], 'p1s_tick_1s': [],

        }

        # min/max_trigger 변수들을 한번에 추가
        for w in self.windows_all:
            self.trading[f'vol_{w}'] = []
            self.trading[f'cbv_{w}'] = []
            self.trading[f'min{w}_trigger'] = []
            self.trading[f'max{w}_trigger'] = []
            self.trading[f'amt_{w}s'] = []
            self.trading[f'bamt_{w}s'] = []
            self.trading[f'tick_{w}s'] = []
            self.trading[f'buy_tick_{w}s'] = []

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

        if 'na' not in list(self.csv_high[code][max(self.today_index - 4, 0): self.today_index + 1]):

            shares = int(self.csv_shares[code][self.today_index - 1]) * float(self.csv_float[code][self.today_index - 1])
            mkt_float = int(float(self.csv_mkt[code][self.today_index - 1]) * float(self.csv_float[code][self.today_index - 1]) / 100)
            ytd_tradamt = round(float(self.csv_tradamt[code][self.today_index - 1]), 2)
            ytd_oh = round(100 * (int(self.csv_high[code][self.today_index - 1]) / int(self.csv_open[code][self.today_index - 1]) - 1), 3)
            ytd_hc = round(100 * (int(self.csv_close[code][self.today_index - 1]) / int(self.csv_high[code][self.today_index - 1]) - 1), 3)
            ytd_lc = round(100 * (int(self.csv_close[code][self.today_index - 1]) / int(self.csv_low[code][self.today_index - 1]) - 1), 3)
            ytd_oc = round(100 * (int(self.csv_close[code][self.today_index - 1]) / int(self.csv_open[code][self.today_index - 1]) - 1), 3)
            ytd_hl = round(100 * (int(self.csv_high[code][self.today_index - 1]) / int(self.csv_low[code][self.today_index - 1]) - 1), 3)
            inc3 = round((int(self.csv_close[code][self.today_index - 1]) / int(self.csv_close[code][self.today_index - 4]) - 1) * 100, 3)

            # 스크리닝용 미래데이터

            tdy_tradamt = round(float(self.csv_tradamt[code][self.today_index]), 2)
            tdy_hl = round(100 * (int(self.csv_high[code][self.today_index]) / int(self.csv_low[code][self.today_index]) - 1), 3)
            tdy_lc = round(100 * (int(self.csv_close[code][self.today_index]) / int(self.csv_low[code][self.today_index]) - 1), 3)
            tdy_oh = round(100 * (int(self.csv_high[code][self.today_index]) / int(self.csv_open[code][self.today_index]) - 1), 3)

            # 진입 조건 설정(일봉 전략1)
            # 850 < mkt_float

            if mkt_float < 10000 and tdy_tradamt > 1 and ytd_tradamt > 16:

                self.data = pd.DataFrame(self.sec_candle.execute("SELECT * FROM '{}'".format(code[1:])))
                correction = np.array(self.data[1][0]) / int(self.csv_open[code][self.today_index])
                t_open = int(self.csv_open[code][self.today_index]) * correction
                # t_high = int(self.csv_high[code][self.today_index]) * correction
                # t_low = int(self.csv_low[code][self.today_index]) * correction
                # t_close = int(self.csv_close[code][self.today_index]) * correction

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
                #ytd_oh < 9 andgap >= 0

                if True:

                    self.stock.update({code: {

                        'today': today, 'name': name, 'shares': shares,
                        't_open': t_open, 'y_high': y_high, 'y_low': y_low, 'y_open': y_open,
                        'y_close': y_close, 'upper': upper,
                        'gap': gap, 'topen_yhigh': topen_yhigh, 'topen_ylow': topen_ylow, 'topen_yopen': topen_yopen,
                        'mkt_float': mkt_float, 'ytd_tradamt': ytd_tradamt,
                        'ytd_oh': ytd_oh, 'ytd_oc': ytd_oc, 'ytd_hc': ytd_hc, 'ytd_lc': ytd_lc,
                        'ytd_hl': ytd_hl, 'inc3': inc3,

                        # 분봉
                        'candle_num': [], 'candle_time': [], 'candle_open': [], 'candle_high': [], 'candle_low': [], 'candle_close': [],
                        'candle_vol': [], 'candle_tick': [], 'candle_buy_vol': [], 'candle_sell_vol': [], 'candle_amt': [], 'candle_bamt': [],

                        'high_time': [], 'high_index': [],

                        'time': [], 'open': [], 'high': [], 'low': [], 'close': [],
                        'vol': [], 'buy_vol': [], 'sell_vol': [],
                        'tick': [], 'buy_tick': [], 'sell_tick': [],
                        'offer_vol': [], 'bid_price': [], 'bid_vol': [],

                        'position': 0, 'state': 0, 'n': 0, 'entry_t': 0, 'exit_t': 0,
                        'entry_price': 0, 'qty': 0,
                        'exit_time': 0, 'exit_price': 0, 'time_spend': 0, 'time_1': 0,
                        'sum_bid_amt': 0, 'sum_bid_v': 0, 'max_bid_amt': 0, 'max_bid_v': 0, 'max_bid_trigger': 0,
                        'sum_offer_v': 0, 'sum_offer_amt': 0, 'max_offer_amt': 0, 'max_offer_v': 0, 'max_offer_trigger': 0,
                        'max_offer_v_index': 0, 'max_offer_p': 0, 'max_bid_v_index': 0, 'max_bid_p': 0,

                        'max_cbv': 0, 'max_tick': 0,
                        # ▼▼▼▼▼ t-1 시점 (직전 1초) 데이터 초기화 추가 ▼▼▼▼▼
                        'p1s_vol': 0, 'p1s_cbv_1': 0, 'p1s_amt_1s': 0, 'p1s_bamt_1s': 0, 'p1s_tick_1s': 0,

                        # ▼▼▼▼▼ p1_ (현재 봉) 관련 추가 ▼▼▼▼▼
                        'p1_amt': 0, 'p1_bamt': 0, 'p1_tick': 0, 'p1_vol': 0, 'p1_bvol': 0,
                        'p1_hl': 0, 'p1_oh': 0, 'p1_oc': 0, 'p1_hc': 0, 'p1_ol': 0, 'p1_lc': 0,
                        # ▲▲▲▲▲ p1_ (현재 봉) 관련 추가 ▲▲▲▲▲

                        # ▼▼▼▼▼ 이전 캔들 관련 지표 초기화 추가 ▼▼▼▼▼
                        'p_hl': 0, 'p_oh': 0, 'p_oc': 0, 'p_hc': 0, 'p_ol': 0, 'p_lc': 0,
                        'pp_hl': 0, 'pp_oh': 0, 'pp_oc': 0, 'pp_hc': 0, 'pp_ol': 0, 'pp_lc': 0,
                        'p12_hl': 0, 'p12_oh': 0, 'p12_oc': 0, 'p12_hc': 0, 'p12_ol': 0, 'p12_lc': 0,
                        'p18_hl': 0, 'p18_oh': 0, 'p18_oc': 0, 'p18_hc': 0, 'p18_ol': 0, 'p18_lc': 0,

                        # ▼▼▼▼▼ 누적 지표 초기화 추가 ▼▼▼▼▼
                        'c_hl': 0, 'c_oh': 0, 'c_oc': 0, 'c_hc': 0, 'c_ol': 0, 'c_lc': 0,

                        # 'trigger_p_open': 0, 'trigger_p_low': 0, 'trigger_p_close': 0,'trigger_p_high': 0,
                        'pnl': 0, 'tax': 0, 'net': 0, 'msg': 0, 'max_t': 0, 'min_t': 9999999999, 'mdd': 0, 'mdu': 0, 'last_high_elapsed': 0,
                        'loss_cut_price': 0,
                        # 'maxhl_entry': 0,
                        'open_trigger': 0, 'high_trigger': 0, 'low_trigger': 0, 'close_trigger': 0,
                        'yopen_trigger': 0, 'yhigh_trigger': 0, 'ylow_trigger': 0, 'yclose_trigger': 0,

                        # 'max_low_trigger': 0, 'max_close_trigger': 0,
                        'max_trigger': 0, 'min_trigger': 9999999999,

                        'p_vol': 0, 'p_bvol': 0, 'p_max': 0, 'p_ratio': 0,
                        # ▼▼▼▼▼ pp_ (3봉 집계) 변수 초기화 ▼▼▼▼▼
                        'pp_amt': 0, 'pp_bamt': 0, 'pp_tick': 0, 'pp_vol': 0, 'pp_bvol': 0,
                        # ▼▼▼▼▼ p12_ (5봉 집계) 변수 초기화 ▼▼▼▼▼
                        'p12_amt': 0, 'p12_bamt': 0, 'p12_tick': 0, 'p12_vol': 0, 'p12_bvol': 0,
                        'p18_amt': 0, 'p18_bamt': 0, 'p18_tick': 0, 'p18_vol': 0, 'p18_bvol': 0,

                    }})

                    # 루프를 사용하여 관련 변수들을 일괄 초기화
                    for w in self.windows_all:
                        self.stock[code][f'vol_{w}'] = 0
                        self.stock[code][f'cbv_{w}'] = 0
                        self.stock[code][f'max_cbv{w}'] = 0
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

            # 진입 포지션이 없을 때만 (entry_t 이전까지만) 최대 H/L 비율을 갱신
            # high_t = self.stock[code]['high'][t]
            # low_t = self.stock[code]['low'][t]
            # current_hl_t_pct = round(((high_t / low_t) - 1) * 100, 3)

            # if current_hl_t_pct > self.stock[code]['max_hl_t']:
            #     self.stock[code]['max_hl_t'] = current_hl_t_pct
            #     self.stock[code]['max_hl_t_time'] = t  # 시간 대신 t(인덱스)를 저장

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

        time_str = self.stock[code]['time'][t]
        # 'HHMM' 부분 (예: '0915')
        hhmm = time_str[:4]
        # 'SS' 부분을 정수로 변환 (예: 23)
        ss = int(time_str[4:6])

        # 초(ss)를 10으로 나눈 몫을 구해 10초 단위 그룹을 만듭니다 (0, 1, 2, 3, 4, 5)
        second_interval_group = ss // 10

        # 'HHMM'과 그룹 번호를 합쳐서 고유한 candle_time을 생성합니다.
        # 예: 09시 15분 23초 -> '0915' + '2' -> 9152
        # 예: 09시 15분 30초 -> '0915' + '3' -> 9153 (새로운 캔들 시작)
        candle_time = int(f"{hhmm}{second_interval_group}")

        # ... 이하 로직은 동일

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
            self.stock[code]['candle_bamt'].append(self.stock[code]['buy_vol'][t] * self.stock[code]['close'][t])
            # self.stock[code]['candle_lc'].append(self.stock[code]['high'][t] / self.stock[code]['low'][t])

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
            self.stock[code]['candle_bamt'][-1] += self.stock[code]['buy_vol'][t] * self.stock[code]['close'][t]

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
                # 90
            elif time_spend > 30:
                # 직전 캔들의 저점 중 더 낮은 값을 기준으로 함 -13
                trailing_stop_price = min(self.stock[code]['candle_low'][-6:-1])
                if self.stock[code]['low'][t] <= trailing_stop_price:
                    exit_msg = 'trail'
                    # 97self.stock[code]['max_t'] * 0.97
            elif self.stock[code]['low'][t] < self.stock[code]['max_t'] * 0.98:
                exit_msg = 'lostest'

            elif self.stock[code]['loss_cut_price'] > 0 and self.stock[code]['low'][t] < self.stock[code]['loss_cut_price']:
                exit_msg = 'losscut'

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
                self.stock[code]['loss_cut_price'] = 0

    def Control_Strategy(self, code, t):

        stock = self.stock[code]

        # 조건 \ \

        if 90000 < int(stock['time'][t]) < 140000 \
                and len(stock['candle_high']) > 4 \
                and 30 < t < self.set_time \
                and stock['position'] == 0 \
                and stock['state'] == 0 \
                and t + 1 < len(stock['time']) \
                and stock['state'] == 0:

            # 완성된 캔들 목록을 가져옵니다 (현재 진행중인 [-1] 캔들은 제외).

            # trigger = stock['candle_close'][-2]
            # trigger = stock['candle_high'][-2]
            trigger = stock['open'][t]
            # trigger = stock['close_at_max_lc_c']
            # trigger = stock['candle_low'][-2]
            # trigger = stock['candle_close'][-2]
            # trigger = stock['y_high']

            stock['trigger'] = trigger

            stock['starttime'] = stock['time'][0]
            stock['entry_t'] = t + 1
            stock['entry_time'] = stock['time'][t + 1]
            # stock['maxhl_entry'] = (t + 1) - stock['max_hl_t_time']

            start_entry = self.calculate_time_spread('second', stock['starttime'], stock['entry_time'])
            stock['start_entry'] = start_entry
            stock['time_1'] = self.calculate_time_spread('second', stock['time'][t - 1], stock['time'][t])

            tick_size = self.calculate_ticksize(trigger, self.csv_open['Code'][self.today_index])
            stock['tick_size'] = tick_size
            stock['tick_rate'] = round(tick_size / trigger * 100, 3)

            # 틱 조건

            if stock['upper'] > trigger > stock['candle_open'][-1] \
                    and stock['candle_close'][-2] >= stock['candle_close'][-3] >= stock['candle_open'][-3]\
                    and stock['candle_high'][-1] > stock['candle_close'][-2] >= stock['candle_open'][-2] \
                    and stock['candle_vol'][-1] >= stock['candle_vol'][-2] >= stock['candle_vol'][-3] \
                    and stock['tick_rate'] < 0.16:

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

                # ▼▼▼▼▼ t-1 시점 (직전 1초) 데이터 계산 추가 ▼▼▼▼▼
                stock['p1s_vol'] = stock['vol'][t]
                stock['p1s_cbv_1'] = stock['buy_vol'][t]
                stock['p1s_amt_1s'] = stock['vol'][t] * stock['close'][t] / 10000
                stock['p1s_bamt_1s'] = stock['buy_vol'][t] * stock['close'][t] / 10000
                stock['p1s_tick_1s'] = stock['tick'][t]

                stock['max_cbv'] = max(stock['buy_vol'][1:t]) + 1  # 0으로 나누는거 방지용
                stock['max_tick'] = max(stock['tick'][1:t])

                stock = self.stock[code]

                for w in self.windows_all:
                    t_ws = max(0, t - w)
                    # 총 거래량, 매수 거래량, 평균 가격 계산
                    vol_ws = sum(stock['vol'][t_ws:t])
                    bvol_ws = sum(stock['buy_vol'][t_ws:t])
                    tick_ws = sum(stock['tick'][t_ws:t])
                    buy_tick_ws = sum(stock['buy_tick'][t_ws:t])
                    price_ws = np.mean(stock['close'][t_ws:t])

                    min_price_w = min(stock['low'][t - w: t])
                    max_price_w = max(stock['high'][t - w: t])

                    # 0으로 나누는 것을 방지
                    stock[f'min{w}_trigger'] = round((min_price_w / trigger - 1) * 100, 3)
                    stock[f'max{w}_trigger'] = round((max_price_w / trigger - 1) * 100, 3)
                    # 시간 스프레드 대비 정규화
                    norm_factor = min(w, time_spread) if time_spread > 0 else 1

                    # stock 딕셔너리에 amt_10s, bamt_10s 등으로 저장
                    stock[f'vol_{w}'] = vol_ws / norm_factor
                    stock[f'cbv_{w}'] = bvol_ws / norm_factor
                    stock[f'amt_{w}s'] = vol_ws * price_ws / 10000 / norm_factor
                    stock[f'bamt_{w}s'] = bvol_ws * price_ws / 10000 / norm_factor
                    stock[f'tick_{w}s'] = tick_ws / norm_factor
                    stock[f'buy_tick_{w}s'] = buy_tick_ws / norm_factor

                stock['cvtotal'] = sum(stock['vol'][:t]) / time_spread
                stock['cbvtotal'] = sum(stock['buy_vol'][:t]) / time_spread
                stock['ctotal'] = sum(stock['tick'][:t]) / time_spread
                stock['cbtotal'] = sum(stock['buy_tick'][:t]) / time_spread * 10000
                stock['cum_amt'] = sum(stock['candle_amt']) / 10000 / 10000

                stock['open_trigger'] = round((trigger / stock['candle_open'][-2] - 1) * 100, 3)
                stock['high_trigger'] = round((trigger / stock['candle_high'][-2] - 1) * 100, 3)
                stock['low_trigger'] = round((trigger / stock['candle_low'][-2] - 1) * 100, 3)
                stock['close_trigger'] = round((trigger / stock['candle_close'][-2] - 1) * 100, 3)

                stock['yopen_trigger'] = round((trigger / stock['y_open'] - 1) * 100, 3)
                stock['yhigh_trigger'] = round((trigger / stock['y_high'] - 1) * 100, 3)
                stock['ylow_trigger'] = round((trigger / stock['y_low'] - 1) * 100, 3)
                stock['yclose_trigger'] = round((trigger / stock['y_close'] - 1) * 100, 3)

                stock['max_trigger'] = round((max(stock['high'][:t]) / trigger - 1) * 100, 3)
                stock['min_trigger'] = round((min(stock['low'][:t]) / trigger - 1) * 100, 3)

                stock['p_amt'] = stock['candle_amt'][-2] / 10000
                stock['p_bamt'] = stock['candle_bamt'][-2] / 10000
                stock['p_tick'] = stock['candle_tick'][-2]
                stock['p_vol'] = stock['candle_vol'][-2]
                stock['p_bvol'] = stock['candle_buy_vol'][-2]

                # ▼▼▼▼▼ 이전 3개 캔들 집계 (pp_) ▼▼▼▼▼
                stock['pp_amt'] = sum(stock['candle_amt'][-3:-1]) / 10000
                stock['pp_bamt'] = sum(stock['candle_bamt'][-3:-1]) / 10000
                stock['pp_tick'] = sum(stock['candle_tick'][-3:-1])
                stock['pp_vol'] = sum(stock['candle_vol'][-3:-1])
                stock['pp_bvol'] = sum(stock['candle_buy_vol'][-3:-1])

                # ▼▼▼▼▼ 이전 12개 캔들 집계 (p12_) ▼▼▼▼▼
                stock['p12_amt'] = sum(stock['candle_amt'][max(0, len(stock['candle_vol']) - 13):-3]) / 10000
                stock['p12_bamt'] = sum(stock['candle_bamt'][max(0, len(stock['candle_vol']) - 13):-3]) / 10000
                stock['p12_tick'] = sum(stock['candle_tick'][max(0, len(stock['candle_vol']) - 13):-3])
                stock['p12_vol'] = sum(stock['candle_vol'][max(0, len(stock['candle_vol']) - 13):-3])
                stock['p12_bvol'] = sum(stock['candle_buy_vol'][max(0, len(stock['candle_vol']) - 13):-3])

                # ▼▼▼▼▼ 이전 18개 캔들 집계 (p18_) ▼▼▼▼▼
                stock['p18_amt'] = sum(stock['candle_amt'][max(0, len(stock['candle_vol']) - 19):-3]) / 10000
                stock['p18_bamt'] = sum(stock['candle_bamt'][max(0, len(stock['candle_vol']) - 19):-3]) / 10000
                stock['p18_tick'] = sum(stock['candle_tick'][max(0, len(stock['candle_vol']) - 19):-3])
                stock['p18_vol'] = sum(stock['candle_vol'][max(0, len(stock['candle_vol']) - 19):-3])
                stock['p18_bvol'] = sum(stock['candle_buy_vol'][max(0, len(stock['candle_vol']) - 19):-3])

                stock['p_max'] = max(stock['candle_buy_vol'])
                if stock['p_max'] > 0:
                    stock['p_ratio'] = stock['candle_buy_vol'][-2] / stock['p_max']
                else:
                    stock['p_ratio'] = 0

                t_open = stock['open'][t - 1]
                t_high = stock['high'][t - 1]
                t_low = stock['low'][t - 1]
                t_close = stock['close'][t - 1]

                stock['t_hl'] = round(100 * (t_high / t_low - 1), 3) if t_low > 0 else 0
                stock['t_oh'] = round(100 * (t_high / t_open - 1), 3) if t_open > 0 else 0
                stock['t_oc'] = round(100 * (t_close / t_open - 1), 3) if t_open > 0 else 0
                stock['t_hc'] = round(100 * (t_close / t_high - 1), 3) if t_high > 0 else 0
                stock['t_ol'] = round(100 * (t_low / t_open - 1), 3) if t_open > 0 else 0
                stock['t_lc'] = round(100 * (t_close / t_low - 1), 3) if t_low > 0 else 0

                # ▼▼▼▼▼ 이전 캔들 관련 지표 계산 ▼▼▼▼▼
                p_open = stock['candle_open'][-2]
                p_high = stock['candle_high'][-2]
                p_low = stock['candle_low'][-2]
                p_close = stock['candle_close'][-2]

                stock['p_hl'] = round(100 * (p_high / p_low - 1), 3) if p_low > 0 else 0
                stock['p_oh'] = round(100 * (p_high / p_open - 1), 3) if p_open > 0 else 0
                stock['p_oc'] = round(100 * (p_close / p_open - 1), 3) if p_open > 0 else 0
                stock['p_hc'] = round(100 * (p_close / p_high - 1), 3) if p_high > 0 else 0
                stock['p_ol'] = round(100 * (p_low / p_open - 1), 3) if p_open > 0 else 0
                stock['p_lc'] = round(100 * (p_close / p_low - 1), 3) if p_low > 0 else 0

                # ▼▼▼▼▼ 이전 3개 캔들 관련 지표 계산 ▼▼▼▼▼
                pp_open = stock['candle_open'][-3]
                pp_high = max(stock['candle_high'][-3:-1])
                pp_low = min(stock['candle_low'][-3:-1])

                stock['pp_hl'] = round(100 * (pp_high / pp_low - 1), 3) if pp_low > 0 else 0
                stock['pp_oh'] = round(100 * (pp_high / pp_open - 1), 3) if pp_open > 0 else 0
                stock['pp_oc'] = round(100 * (p_close / pp_open - 1), 3) if pp_open > 0 else 0
                stock['pp_hc'] = round(100 * (p_close / pp_high - 1), 3) if pp_high > 0 else 0
                stock['pp_ol'] = round(100 * (pp_low / pp_open - 1), 3) if pp_open > 0 else 0
                stock['pp_lc'] = round(100 * (p_close / pp_low - 1), 3) if pp_low > 0 else 0

                # ▼▼▼▼▼ 이전 5개 캔들 관련 지표 계산 ▼▼▼▼▼
                p12_open = stock['candle_open'][max(0, len(stock['candle_vol']) - 13)]
                p12_high = max(stock['candle_high'][max(0, len(stock['candle_vol']) - 13):-3])
                p12_low = min(stock['candle_low'][max(0, len(stock['candle_vol']) - 13):-3])
                ppp_close = stock['candle_close'][-4]

                stock['p12_hl'] = round(100 * (p12_high / p12_low - 1), 3) if p12_low > 0 else 0
                stock['p12_oh'] = round(100 * (p12_high / p12_open - 1), 3) if p12_open > 0 else 0
                stock['p12_oc'] = round(100 * (ppp_close / p12_open - 1), 3) if p12_open > 0 else 0
                stock['p12_hc'] = round(100 * (ppp_close / p12_high - 1), 3) if p12_high > 0 else 0
                stock['p12_ol'] = round(100 * (p12_low / p12_open - 1), 3) if p12_open > 0 else 0
                stock['p12_lc'] = round(100 * (ppp_close / p12_low - 1), 3) if p12_low > 0 else 0

                # ▼▼▼▼▼ 이전 5개 캔들 관련 지표 계산 ▼▼▼▼▼
                p18_open = stock['candle_open'][max(0, len(stock['candle_vol']) - 19)]
                p18_high = max(stock['candle_high'][max(0, len(stock['candle_vol']) - 19):-3])
                p18_low = min(stock['candle_low'][max(0, len(stock['candle_vol']) - 19):-3])

                stock['p18_hl'] = round(100 * (p18_high / p18_low - 1), 3) if p18_low > 0 else 0
                stock['p18_oh'] = round(100 * (p18_high / p18_open - 1), 3) if p18_open > 0 else 0
                stock['p18_oc'] = round(100 * (ppp_close / p18_open - 1), 3) if p18_open > 0 else 0
                stock['p18_hc'] = round(100 * (ppp_close / p18_high - 1), 3) if p18_high > 0 else 0
                stock['p18_ol'] = round(100 * (p18_low / p18_open - 1), 3) if p18_open > 0 else 0
                stock['p18_lc'] = round(100 * (ppp_close / p18_low - 1), 3) if p18_low > 0 else 0

                # ▼▼▼▼▼ 누적(Cumulative) 캔들 지표 계산 ▼▼▼▼▼
                # 진입 시점까지의 누적 데이터 사용 (현재 진행중인 캔들 [-1] 제외)
                c_open0 = stock['candle_open'][0]
                c_high = max(stock['candle_high'][:-1])
                c_low = min(stock['candle_low'][:-1])

                stock['c_hl'] = round(100 * (c_high / c_low - 1), 3) if c_low > 0 else 0
                stock['c_oh'] = round(100 * (c_high / c_open0 - 1), 3) if c_open0 > 0 else 0
                stock['c_oc'] = round(100 * (p_close / c_open0 - 1), 3) if c_open0 > 0 else 0
                stock['c_hc'] = round(100 * (p_close / c_high - 1), 3) if c_high > 0 else 0
                stock['c_ol'] = round(100 * (c_low / c_open0 - 1), 3) if c_open0 > 0 else 0
                stock['c_lc'] = round(100 * (p_close / c_low - 1), 3) if c_low > 0 else 0

                # ▼▼▼▼▼ p1_ (현재 봉) 관련 추가 ▼▼▼▼▼
                # 현재 진행중인 봉([-1])의 데이터를 가져옴
                p1_open = stock['candle_open'][-1]
                p1_high = stock['candle_high'][-1]
                p1_low = stock['candle_low'][-1]
                p1_close = stock['candle_close'][-1]  # 실시간 가격으로 계속 업데이트 됨

                stock['p1_amt'] = stock['candle_amt'][-1] / 10000
                stock['p1_bamt'] = stock['candle_bamt'][-1] / 10000
                stock['p1_tick'] = stock['candle_tick'][-1]
                stock['p1_vol'] = stock['candle_vol'][-1]
                stock['p1_bvol'] = stock['candle_buy_vol'][-1]

                stock['p1_hl'] = round(100 * (p1_high / p1_low - 1), 3) if p1_low > 0 else 0
                stock['p1_oh'] = round(100 * (p1_high / p1_open - 1), 3) if p1_open > 0 else 0
                stock['p1_oc'] = round(100 * (p1_close / p1_open - 1), 3) if p1_open > 0 else 0
                stock['p1_hc'] = round(100 * (p1_close / p1_high - 1), 3) if p1_high > 0 else 0
                stock['p1_ol'] = round(100 * (p1_low / p1_open - 1), 3) if p1_open > 0 else 0
                stock['p1_lc'] = round(100 * (p1_close / p1_low - 1), 3) if p1_low > 0 else 0
                # ▲▲▲▲▲ p1_ (현재 봉) 관련 추가 ▲▲▲▲▲

                # ───────────────────────────────────────────────
                # 1단계: trigger 와 같은 값이 과거에 있었는가?
                # ───────────────────────────────────────────────
                # target_price = trigger
                last_high_t = None
                search_array = stock['high'][:t]
                indices = np.where(search_array == trigger)[0]

                if indices.size > 0:
                    last_high_t = indices[-1]

                # ───────────────────────────────────────────────
                # 거리 계산 & 결과 저장
                # ───────────────────────────────────────────────
                if last_high_t is None:
                    time_since_last_high = 99999
                else:
                    time_since_last_high = t - last_high_t

                stock['last_high_elapsed'] = time_since_last_high

                # 진입 조건 설정(초봉 전략)
                #  and stock['p1s_bamt_1s'] > 2000 \

                if start_entry <= self.set_time \
                        and stock['yclose_trigger'] > 5 \
                        and stock['ctotal'] > 6 \
                        and stock['p18_tick'] < 2000 \
                        and stock['p18_lc'] < 2 \
                        and stock['max_tick'] < 220 \
                        and stock['c_hl'] < 7 \
                        and stock['min30_trigger'] > -1.5 \
                        and stock['c_hc'] < -3:

                    # === t 시점 ===
                    offer_p_list = list(self.data.iloc[t][11:21])
                    offer_vol_list = list(self.data.iloc[t][21:31])
                    max_offer_v = max(offer_vol_list)
                    max_offer_v_index = offer_vol_list.index(max_offer_v)
                    max_offer_p = offer_p_list[max_offer_v_index]

                    stock['sum_offer_v'] = sum(offer_vol_list)
                    stock['sum_offer_amt'] = sum([vol * p for vol, p in zip(offer_vol_list, offer_p_list)]) / 10000
                    stock['max_offer_v'] = max_offer_v
                    stock['max_offer_v_index'] = max_offer_v_index
                    stock['max_offer_p'] = max_offer_p
                    stock['max_offer_amt'] = (max_offer_v * max_offer_p) / 10000
                    stock['max_offer_trigger'] = round((max_offer_p / trigger - 1) * 100, 3)

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
                    stock['max_bid_trigger'] = round((max_bid_p / trigger - 1) * 100, 3)

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

                    # 호가 조건 \
                    #                             and stock['max_bid_v_ch'] > -20 \
                    #                             and stock['max_bid_amt'] > 3300 \
                    #                             and stock['sum_offer_v_ch'] < 1400

                    if stock['sum_offer_amt'] > 10000:
                        stock['position'] = 1
                        stock['n'] += 1
                        stock['entry_price'] = stock['high'][t + 1]
                        stock['loss_cut_price'] = stock['candle_open'][-3]

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
            # self.trading['maxhl_entry'].append(self.stock[code]['maxhl_entry'])
            self.trading['msg'].append(self.stock[code]['msg'])

            self.trading['ytd_oh'].append(self.stock[code]['ytd_oh'])
            self.trading['ytd_oc'].append(self.stock[code]['ytd_oc'])
            self.trading['ytd_hc'].append(self.stock[code]['ytd_hc'])
            self.trading['ytd_lc'].append(self.stock[code]['ytd_lc'])

            self.trading['ytd_hl'].append(self.stock[code]['ytd_hl'])
            self.trading['inc3'].append(self.stock[code]['inc3'])

            self.trading['n'].append(self.stock[code]['n'])

            self.trading['entry_time'].append(self.stock[code]['entry_time'])
            self.trading['start_entry'].append(self.stock[code]['start_entry'])

            self.trading['entry_t'].append(self.stock[code]['entry_t'])

            self.trading['entry_price'].append(self.stock[code]['entry_price'])
            self.trading['exit_time'].append(self.stock[code]['exit_time'])
            self.trading['exit_price'].append(self.stock[code]['exit_price'])
            self.trading['time_spend'].append(self.stock[code]['time_spend'])
            self.trading['time_1'].append(self.stock[code]['time_1'])

            # ▼▼▼▼▼ t-1 시점 (직전 1초) 데이터 저장 ▼▼▼▼▼
            self.trading['p1s_vol'].append(self.stock[code]['p1s_vol'])
            self.trading['p1s_cbv_1'].append(self.stock[code]['p1s_cbv_1'])
            self.trading['p1s_amt_1s'].append(self.stock[code]['p1s_amt_1s'])
            self.trading['p1s_bamt_1s'].append(self.stock[code]['p1s_bamt_1s'])
            self.trading['p1s_tick_1s'].append(self.stock[code]['p1s_tick_1s'])

            self.trading['max_cbv'].append(self.stock[code]['max_cbv'])
            self.trading['max_tick'].append(self.stock[code]['max_tick'])

            for w in self.windows_all:
                # amt_?s, bamt_?s 키로 trading에 추가
                self.trading[f'amt_{w}s'].append(self.stock[code][f'amt_{w}s'])
                self.trading[f'bamt_{w}s'].append(self.stock[code][f'bamt_{w}s'])
                self.trading[f'tick_{w}s'].append(self.stock[code][f'tick_{w}s'])
                self.trading[f'buy_tick_{w}s'].append(self.stock[code][f'buy_tick_{w}s'])
                self.trading[f'vol_{w}'].append(self.stock[code][f'vol_{w}'])
                self.trading[f'cbv_{w}'].append(self.stock[code][f'cbv_{w}'])
                self.trading[f'min{w}_trigger'].append(self.stock[code]['min' + str(w) + '_trigger'])
                self.trading[f'max{w}_trigger'].append(self.stock[code]['max' + str(w) + '_trigger'])

            self.trading['cvtotal'].append(self.stock[code]['cvtotal'])
            self.trading['cbvtotal'].append(self.stock[code]['cbvtotal'])
            self.trading['ctotal'].append(self.stock[code]['ctotal'])
            self.trading['cbtotal'].append(self.stock[code]['cbtotal'])
            self.trading['cum_amt'].append(self.stock[code]['cum_amt'])

            self.trading['open_trigger'].append(self.stock[code]['open_trigger'])
            self.trading['high_trigger'].append(self.stock[code]['high_trigger'])
            self.trading['low_trigger'].append(self.stock[code]['low_trigger'])
            self.trading['close_trigger'].append(self.stock[code]['close_trigger'])

            self.trading['yopen_trigger'].append(self.stock[code]['yopen_trigger'])
            self.trading['yhigh_trigger'].append(self.stock[code]['yhigh_trigger'])
            self.trading['ylow_trigger'].append(self.stock[code]['ylow_trigger'])
            self.trading['yclose_trigger'].append(self.stock[code]['yclose_trigger'])

            # self.trading['max_low_trigger'].append(self.stock[code]['max_low_trigger'])
            # self.trading['max_close_trigger'].append(self.stock[code]['max_close_trigger'])

            self.trading['max_trigger'].append(self.stock[code]['max_trigger'])
            self.trading['min_trigger'].append(self.stock[code]['min_trigger'])

            self.trading['p_amt'].append(self.stock[code]['p_amt'])
            self.trading['p_bamt'].append(self.stock[code]['p_bamt'])
            self.trading['p_tick'].append(self.stock[code]['p_tick'])
            self.trading['p_vol'].append(self.stock[code]['p_vol'])
            self.trading['p_bvol'].append(self.stock[code]['p_bvol'])
            self.trading['p_max'].append(self.stock[code]['p_max'])
            self.trading['p_ratio'].append(self.stock[code]['p_ratio'])

            # ▼▼▼▼▼ p1_ (현재 봉) 관련 추가 ▼▼▼▼▼
            self.trading['p1_amt'].append(self.stock[code]['p1_amt'])
            self.trading['p1_bamt'].append(self.stock[code]['p1_bamt'])
            self.trading['p1_tick'].append(self.stock[code]['p1_tick'])
            self.trading['p1_vol'].append(self.stock[code]['p1_vol'])
            self.trading['p1_bvol'].append(self.stock[code]['p1_bvol'])
            self.trading['p1_hl'].append(self.stock[code]['p1_hl'])
            self.trading['p1_oh'].append(self.stock[code]['p1_oh'])
            self.trading['p1_oc'].append(self.stock[code]['p1_oc'])
            self.trading['p1_hc'].append(self.stock[code]['p1_hc'])
            self.trading['p1_ol'].append(self.stock[code]['p1_ol'])
            self.trading['p1_lc'].append(self.stock[code]['p1_lc'])
            # ▲▲▲▲▲ p1_ (현재 봉) 관련 추가 ▲▲▲▲▲

            # ▼▼▼▼▼ pp_ (3봉 집계) 데이터 저장 ▼▼▼▼▼
            self.trading['pp_amt'].append(self.stock[code]['pp_amt'])
            self.trading['pp_bamt'].append(self.stock[code]['pp_bamt'])
            self.trading['pp_tick'].append(self.stock[code]['pp_tick'])
            self.trading['pp_vol'].append(self.stock[code]['pp_vol'])
            self.trading['pp_bvol'].append(self.stock[code]['pp_bvol'])

            # # ▼▼▼▼▼ p12_ (5봉 집계) 데이터 저장 ▼▼▼▼▼
            self.trading['p12_amt'].append(self.stock[code]['p12_amt'])
            self.trading['p12_bamt'].append(self.stock[code]['p12_bamt'])
            self.trading['p12_tick'].append(self.stock[code]['p12_tick'])
            self.trading['p12_vol'].append(self.stock[code]['p12_vol'])
            self.trading['p12_bvol'].append(self.stock[code]['p12_bvol'])

            # # ▼▼▼▼▼ p18_ (5봉 집계) 데이터 저장 ▼▼▼▼▼
            self.trading['p18_amt'].append(self.stock[code]['p18_amt'])
            self.trading['p18_bamt'].append(self.stock[code]['p18_bamt'])
            self.trading['p18_tick'].append(self.stock[code]['p18_tick'])
            self.trading['p18_vol'].append(self.stock[code]['p18_vol'])
            self.trading['p18_bvol'].append(self.stock[code]['p18_bvol'])

            self.trading['t_hl'].append(self.stock[code]['t_hl'])
            self.trading['t_oh'].append(self.stock[code]['t_oh'])
            self.trading['t_oc'].append(self.stock[code]['t_oc'])
            self.trading['t_hc'].append(self.stock[code]['t_hc'])
            self.trading['t_ol'].append(self.stock[code]['t_ol'])
            self.trading['t_lc'].append(self.stock[code]['t_lc'])

            self.trading['p_hl'].append(self.stock[code]['p_hl'])
            self.trading['p_oh'].append(self.stock[code]['p_oh'])
            self.trading['p_oc'].append(self.stock[code]['p_oc'])
            self.trading['p_hc'].append(self.stock[code]['p_hc'])
            self.trading['p_ol'].append(self.stock[code]['p_ol'])
            self.trading['p_lc'].append(self.stock[code]['p_lc'])

            self.trading['pp_hl'].append(self.stock[code]['pp_hl'])
            self.trading['pp_oh'].append(self.stock[code]['pp_oh'])
            self.trading['pp_oc'].append(self.stock[code]['pp_oc'])
            self.trading['pp_hc'].append(self.stock[code]['pp_hc'])
            self.trading['pp_ol'].append(self.stock[code]['pp_ol'])
            self.trading['pp_lc'].append(self.stock[code]['pp_lc'])

            self.trading['p12_hl'].append(self.stock[code]['p12_hl'])
            self.trading['p12_oh'].append(self.stock[code]['p12_oh'])
            self.trading['p12_oc'].append(self.stock[code]['p12_oc'])
            self.trading['p12_hc'].append(self.stock[code]['p12_hc'])
            self.trading['p12_ol'].append(self.stock[code]['p12_ol'])
            self.trading['p12_lc'].append(self.stock[code]['p12_lc'])

            self.trading['p18_hl'].append(self.stock[code]['p18_hl'])
            self.trading['p18_oh'].append(self.stock[code]['p18_oh'])
            self.trading['p18_oc'].append(self.stock[code]['p18_oc'])
            self.trading['p18_hc'].append(self.stock[code]['p18_hc'])
            self.trading['p18_ol'].append(self.stock[code]['p18_ol'])
            self.trading['p18_lc'].append(self.stock[code]['p18_lc'])

            # ▼▼▼▼▼ 누적 지표 저장 ▼▼▼▼▼
            self.trading['c_hl'].append(self.stock[code]['c_hl'])
            self.trading['c_oh'].append(self.stock[code]['c_oh'])
            self.trading['c_oc'].append(self.stock[code]['c_oc'])
            self.trading['c_hc'].append(self.stock[code]['c_hc'])
            self.trading['c_ol'].append(self.stock[code]['c_ol'])
            self.trading['c_lc'].append(self.stock[code]['c_lc'])

            self.trading['sum_bid_amt'].append(self.stock[code]['sum_bid_amt'])
            self.trading['sum_bid_v'].append(self.stock[code]['sum_bid_v'])
            self.trading['max_bid_amt'].append(self.stock[code]['max_bid_amt'])
            self.trading['max_bid_v'].append(self.stock[code]['max_bid_v'])
            self.trading['max_bid_trigger'].append(self.stock[code]['max_bid_trigger'])

            self.trading['sum_offer_amt'].append(self.stock[code]['sum_offer_amt'])
            self.trading['sum_offer_v'].append(self.stock[code]['sum_offer_v'])

            self.trading['max_offer_amt'].append(self.stock[code]['max_offer_amt'])
            self.trading['max_offer_v'].append(self.stock[code]['max_offer_v'])
            self.trading['max_offer_trigger'].append(self.stock[code]['max_offer_trigger'])

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

# 파일 끝에 있던 코드는 클래스 내부에 있어야 할 것으로 보여 클래스 메소드 안으로 이동시켰습니다.
# 만약 독립적인 코드였다면, 해당 코드는 이 파일의 실행 로직과 관련이 없어 보입니다.
# 원본 코드에는 p1_open 관련 코드가 클래스 밖에 존재하여 구문 오류를 발생시킬 수 있었습니다.