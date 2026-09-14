import os
import sqlite3
import traceback
from datetime import datetime
from typing import Optional, Sequence
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
    TARGET_CODES,
    SEC_PATH,
)
from engine.utils import (
    calculate_ticksize,
    calculate_upperlimit,
    calculate_time_spread,
)
from engine.data_loader import DataLoader
from engine.risk_manager import check_exit_signals
from engine.strategy import (
    REQUIRED_FEATURES,
    apply_stored_metrics,
    calculate_window_metrics,
    check_entry_conditions,
)

# 🆕 Phase A — 표준 결과 계약(L5). 기존 CSV 출력은 그대로 두고 병행 저장한다.
from core.contracts import Trade
from core.runstore import (
    RunManifest,
    RunStore,
    current_git_sha,
    hash_params,
    make_run_id,
)

# 🆕 Phase B-1 — L2 피처 레이어. 루프에서 계산하던 값을 미리 계산된 parquet 에서 읽는다.
from features.store import FeatureStore

# 🆕 Phase B-4 — 유니버스 단일 소스. 일봉 매트릭스가 암묵적으로 정하던 것을 선언으로.
from core.universe import (
    MISSING_THRESHOLD_PCT,
    UniverseReconciliation,
    load_universe,
    reconcile,
    resolve_codes,
)

#: feature_source 로 허용되는 값
FEATURE_SOURCE_STORE = "store"      # fs_v1 parquet 조회 (기본)
FEATURE_SOURCE_INLINE = "inline"    # calculate_window_metrics() 루프 계산 (대조용)


class MissingFeaturesError(RuntimeError):
    """
    백테스트할 날짜·종목의 피처가 스토어에 없다.

    **인라인 계산으로 조용히 폴백하지 않는다.** 폴백하면 "피처 스토어를 쓰고 있다"고
    믿으면서 실제로는 안 쓰는 상태가 되고, 그 상태는 어떤 로그에도 남지 않는다.
    Phase B 가 약속한 이득(파라미터 스윕 시 피처 재계산 0회)이 실현되고 있는지를
    런 매니페스트만 보고는 알 수 없게 된다 — 정확히 §1.6 이 잡아낸 실패 양상이다.
    """


class UniverseGapError(RuntimeError):
    """
    선언된 유니버스와 실제 처리된 종목의 차이가 임계를 넘었다 (--strict-universe).

    조용한 축소를 막는 것이 B-4 의 전부다. 운영에서 daily_collector 가 종목을
    놓치면 그 종목은 모든 백테스트에서 사라지는데, 지금까지는 매니페스트의
    universe_size 가 그냥 작은 숫자를 보고할 뿐이었다.
    """


class BackTestEngine:
    def __init__(
        self,
        part: int = 1,
        split: int = 12,
        runs_root=None,
        codes: Optional[Sequence[str]] = None,
        dates: Optional[Sequence[str]] = None,
        feature_source: str = FEATURE_SOURCE_STORE,
        feature_root=None,
        universe: Optional[str] = None,
        universe_path=None,
        missing_threshold_pct: float = MISSING_THRESHOLD_PCT,
        strict_universe: bool = False,
    ):
        """
        codes — 대상 종목 필터. None 이면 engine.config.TARGET_CODES 를 따른다
        (그것도 None 이면 전체 종목). 과거에는 `if code != '000270': continue` 로
        소스에 박혀 있었다 (ARCHITECTURE_V2.md §1.6). 이제는 세 경로로 지정한다.

            BackTestEngine(codes=["000270"])                       # 코드
            uv run python -m engine.main --codes 000270            # CLI
            TARGET_CODES=000270,005930 uv run python -m engine.main # 환경변수

        dates — 대상 날짜 필터(YYYYMMDD). None 이면 일봉 매트릭스의 전체 날짜.
                특정 구간만 재현해야 하는 대조 실행에 쓴다.

        feature_source — "store" 는 fs_v1 parquet 조회(기본), "inline" 은 기존
                calculate_window_metrics() 루프 계산. 두 경로의 거래 목록이
                일치하는지는 scripts/verify_engine_feature_parity.py 가 대조한다.
                **대조가 통과하기 전에는 인라인 경로를 제거하지 않는다** (§8).

        universe — universe.yaml 의 선언 이름. None 이면 파일의 default.
                과거에는 일봉 매트릭스 컬럼이 유니버스를 암묵적으로 정했다
                (ARCHITECTURE_V2.md §3.6.1 추가 발견). 이제는 선언이 기준이고,
                선언과 실제 처리의 차이가 사유별로 매니페스트에 남는다.

        codes 와 universe 의 관계: codes 는 선언 위에 다시 씌우는 **필터**다.
                유니버스를 넓히는 용도가 아니라 좁히는 용도이므로, 선언에 없는
                종목을 codes 로 지정해도 처리되지 않는다.
        """
        if feature_source not in (FEATURE_SOURCE_STORE, FEATURE_SOURCE_INLINE):
            raise ValueError(
                f"feature_source 는 {FEATURE_SOURCE_STORE} 또는 {FEATURE_SOURCE_INLINE} "
                f"여야 합니다: {feature_source}"
            )

        self.part = part
        self.split = split
        self.strategy = STRATEGY_NAME
        self.loader = DataLoader()
        self.codes: Optional[tuple[str, ...]] = tuple(codes) if codes is not None else TARGET_CODES
        self.dates: Optional[tuple[str, ...]] = tuple(str(d) for d in dates) if dates is not None else None

        # 🆕 Phase B-1 — 피처 출처. 인라인 런은 fs_v1 을 읽지 않았으므로 "none" 을
        # 기록한다. 이 값이 run_id 에 들어가므로 두 경로의 런은 절대 같은 id 가 아니다.
        self.feature_source = feature_source
        self.feature_set_version = (
            FEATURE_SET_VERSION if feature_source == FEATURE_SOURCE_STORE else "none"
        )
        self.feature_store = (
            FeatureStore(version=FEATURE_SET_VERSION, root=feature_root)
            if feature_source == FEATURE_SOURCE_STORE
            else None
        )

        # 🆕 Phase B-4 — 유니버스 선언. 해석은 날짜가 정해진 뒤(run) 한다.
        self.universe_spec = load_universe(universe, universe_path)
        self.missing_threshold_pct = missing_threshold_pct
        self.strict_universe = strict_universe
        self.reconciliation: Optional[UniverseReconciliation] = None

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
        source_label = (
            f"피처 스토어 {self.feature_set_version} ({len(REQUIRED_FEATURES)}개 컬럼)"
            if self.feature_source == FEATURE_SOURCE_STORE
            else "인라인 계산 (calculate_window_metrics)"
        )
        print(f"🚀 [Part {self.part}/{self.split}] 백테스트 엔진 가동 시작 — {source_label}")
        self.trading = self._init_trading_dict()
        self.trades = []

        if 'open' not in self.daily_data or self.daily_data['open'].empty:
            print("⚠️ 일봉 데이터(open.csv 등)를 읽을 수 없습니다.")
            return

        csv_open = self.daily_data['open']

        # 🆕 Phase B-4 — 날짜의 출처를 일봉 매트릭스에서 LOB 파일 목록으로 옮긴다.
        # 백테스트할 수 있는 날은 '일봉 CSV 에 행이 있는 날'이 아니라 '초봉이 수집된
        # 날'이다. 과거에는 두 조건이 암묵적으로 AND 로 걸려 있었고(없는 날은 conn
        # is None 으로 건너뜀), 그래서 date_range 가 실제 실행 구간보다 훨씬 넓게
        # 기록됐다. 파일 목록이 더 정직한 소스다.
        date_list = self._collectable_dates()

        if self.dates is not None:
            wanted = set(self.dates)
            date_list = [d for d in date_list if d in wanted]

        length = int(len(date_list) / self.split)
        if self.part < self.split:
            load_dates = date_list[length * (self.part - 1): length * self.part]
        else:
            load_dates = date_list[length * (self.part - 1):]

        print(f"📅 담당 백테스팅 날짜 범위: {load_dates[0] if load_dates else '없음'} ~ {load_dates[-1] if load_dates else '없음'}")

        # 🆕 Phase A — 런 식별자 확정.
        # Trade 레코드가 run_id 를 들고 있어야 하므로 루프 '전에' 계산한다.
        self._prepare_run_identity(load_dates)

        # 🆕 Phase B-4 — 선언된 유니버스. daily_col 은 종목명·전일종가 조회용 컬럼명이다.
        declared = self._declared_codes(load_dates)
        daily_col = {
            (c[1:] if str(c).startswith('A') else str(c)): c for c in csv_open.keys()[1:]
        }

        processed: set[str] = set()
        seen_in_lob: set[str] = set()
        missing_features: set[str] = set()

        for today_date in load_dates:
            today_str = str(today_date)
            conn = self.loader.get_lob_db_connection(today_str)
            if conn is None:
                continue

            cursor = conn.cursor()
            sec_tables = set(name[0] for name in cursor.execute("SELECT name FROM sqlite_master WHERE type='table';"))
            seen_in_lob |= sec_tables

            # 선언된 유니버스 ∩ 그날 수집된 종목 ∩ 일봉 매트릭스에 있는 종목.
            # 세 번째 조건이 아직 남아 있는 이유: 종목명과 전일 종가(상한가 계산의
            # 입력)를 일봉 매트릭스에서만 얻을 수 있기 때문이다. 그래서 여기서
            # 빠지는 종목은 no_daily 로 집계된다 — 사라지되 세어진다.
            targets = [
                (daily_col[code], code)
                for code in declared
                if code in sec_tables and code in daily_col
            ]

            # 🆕 Phase B-1 — 하루치 피처를 파일 하나에서 한 번에 읽는다.
            # 종목마다 열면 같은 parquet 푸터를 종목 수만큼 다시 파싱하게 된다.
            day_features = self._load_day_features(today_str, [c for _, c in targets])

            for code_col, code in targets:
                if self.feature_source == FEATURE_SOURCE_STORE and code not in day_features:
                    missing_features.add(code)
                    continue
                processed.add(code)
                self._process_stock(conn, code_col, code, today_str, day_features.get(code))

            conn.close()

        self.reconciliation = reconcile(
            declared,
            processed=processed,
            seen_in_lob=seen_in_lob,
            seen_in_daily=daily_col,
            missing_features=missing_features,
        )
        self._report_universe()

        # 결과 CSV 저장 (레거시 경로 — 런 스토어 검증이 끝날 때까지 유지한다)
        result_df = pd.DataFrame(self.trading)
        output_file = RESULT_DIR / f"{self.strategy}_{self.part}.csv"
        result_df.to_csv(output_file, encoding=ENCODING, index=False)
        print(f"✅ [Part {self.part}] 백테스팅 완료! 저장 건수: {len(result_df)}건 ➔ 파일: {output_file}")

        # 🆕 Phase A — 표준 Trade 레코드를 런 스토어에도 저장 (CSV 와 병행)
        self._save_run(result_df, len(processed))

    # ------------------------------------------------------------------
    # 🆕 Phase B-4 — 유니버스 선언과 대조
    # ------------------------------------------------------------------

    @staticmethod
    def _collectable_dates() -> list[str]:
        """초봉이 수집된 날짜 (temp/*_LOB.db). 백테스트가 실제로 돌 수 있는 날이다."""
        return sorted(p.stem.replace("_LOB", "") for p in SEC_PATH.glob("*_LOB.db"))

    def _declared_codes(self, dates: Sequence[str]) -> tuple[str, ...]:
        """
        선언을 종목 목록으로 푼다. codes 인자가 있으면 그 위에 필터로 덧씌운다.

        codes 는 선언을 넓히지 못한다 — 선언에 없는 종목을 지정해도 처리되지 않는다.
        유니버스의 단일 소스는 어디까지나 universe.yaml 이다.
        """
        declared = resolve_codes(
            self.universe_spec, dates=dates, daily_frame=self.daily_data.get('open')
        )
        if self.codes:
            wanted = set(self.codes)
            declared = tuple(c for c in declared if c in wanted)
        return declared

    def _report_universe(self) -> None:
        """
        선언 대비 처리 결과를 출력하고, 결손이 임계를 넘으면 경고하거나 실패시킨다.

        지금까지는 "탐색한 총 종목 수: 3개"만 찍혔다. 3이 의도한 값인지 아닌지를
        아무도 알 수 없었고, 그래서 아무도 의심하지 않았다 (§3.6.1 추가 발견).
        """
        rec = self.reconciliation
        spec = self.universe_spec
        print(f"🌐 유니버스 '{spec.name}' ({spec.source}) — {rec.describe()}")

        if rec.within(self.missing_threshold_pct):
            return

        for reason, codes in sorted(rec.missing_codes.items()):
            sample = ", ".join(codes[:5])
            more = f" 외 {len(codes) - 5}종목" if len(codes) > 5 else ""
            print(f"   {reason:<12} {len(codes):>5}종목  예: {sample}{more}")

        message = (
            f"선언된 유니버스의 {rec.missing_pct:.1f}% 가 처리되지 않았습니다 "
            f"(임계 {self.missing_threshold_pct:.0f}%)"
        )
        if self.strict_universe:
            raise UniverseGapError(message)
        print(f"   ⚠️ {message}")

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
        # universe 는 **선언**만 넣는다. 해석 결과(종목 수)는 데이터가 채워지는 대로
        # 변하므로 run_id 에 들어가면 같은 선언의 런이 서로 다른 id 를 갖게 된다.
        # 대조 결과는 매니페스트의 별도 필드로 나간다 (_save_run).
        self.run_params = {
            "set_time": SET_TIME,
            "fee_pct": FEE_PCT,
            "exit_rule": EXIT_RULE_ID,
            "universe": self.universe_spec.as_params(),
            "universe_filter": ",".join(self.codes) if self.codes else "all",
            "partition": {"part": self.part, "split": self.split},
        }
        self.run_id = make_run_id(
            strategy_id=self.strategy,
            strategy_version=STRATEGY_VERSION,
            param_hash=hash_params(self.run_params),
            feature_set_version=self.feature_set_version,
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
            feature_set_version=self.feature_set_version,
            engine="bar",                      # 1초봉 해상도 엔진
            mode="backtest",
            date_range=self.date_range,
            universe_size=processed_stocks,
            git_sha=current_git_sha(),
            params=self.run_params,
            universe=self.reconciliation.as_manifest() if self.reconciliation else {},
            notes=(
                f"feature_source={self.feature_source} · "
                f"universe={self.universe_spec.name} · "
                f"legacy CSV 병행 출력: {RESULT_DIR / f'{self.strategy}_{self.part}.csv'}"
            ),
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

    def _prev_close(self, code_col: str, today_str: str) -> Optional[float]:
        """
        전일 종가 조회 — calculate_upperlimit() 의 point-in-time 입력.

        daily_data['close'] 는 0행이 종목명, 1행부터 날짜 오름차순이다. 오늘 날짜
        바로 앞 행이 전일 종가다. 데이터셋의 첫 거래일이라 전일이 없으면 None.
        (§1.6 — engine/data_loader.py 가 로드만 하고 쓰지 않던 'close' 매트릭스를
        여기서 처음 사용한다)
        """
        csv_close = self.daily_data.get('close')
        if csv_close is None or code_col not in csv_close.columns:
            return None

        dates = csv_close['Code'].astype(str)
        matches = dates.index[dates == today_str]
        if len(matches) == 0:
            return None

        idx = matches[0]
        if idx < 2:                 # idx=0 은 종목명 행, idx=1 은 첫 거래일(전일 없음)
            return None

        try:
            value = float(csv_close[code_col].iloc[idx - 1])
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    # ------------------------------------------------------------------
    # 🆕 Phase B-1 — 피처 스토어 연동
    # ------------------------------------------------------------------

    def _load_day_features(self, date: str, codes: Sequence[str]) -> dict:
        """
        하루치 피처를 종목별 numpy 배열 묶음으로 읽는다. 파일당 1회 I/O.

        읽는 컬럼은 REQUIRED_FEATURES 4개 + 키 2개뿐이다. 파일에 든 44개 중
        나머지 38개는 디스크에서 꺼내지도 않는다 — 이게 §3.5 가 말한 컬럼 선택
        읽기이고, Parquet 을 택한 실제 이유다(압축률은 1.26배에 불과하다).

        파일 자체가 없으면 MissingFeaturesError — 인라인 계산으로 조용히 폴백하지
        않는다. 반면 **파일 안에 특정 종목이 없는 것**은 에러가 아니라 유니버스
        결손이다(B-4). 그 종목은 처리에서 빠지고 no_features 로 집계돼 매니페스트에
        남는다. 전자는 운영 실패이고 후자는 계층 간 데이터 격차라, 다루는 방식이 다르다.
        """
        if self.feature_source != FEATURE_SOURCE_STORE or not codes:
            return {}

        try:
            frame = self.feature_store.read(date, codes=list(codes), names=REQUIRED_FEATURES)
        except FileNotFoundError as exc:
            raise MissingFeaturesError(
                f"{date}: 피처 파일이 없습니다 ({self.feature_store.path_for(date)}). "
                f"먼저 빌드하세요 — uv run python scripts/build_features.py --date {date}"
            ) from exc

        by_code: dict[str, dict] = {}
        for code, group in frame.groupby("code", sort=False):
            by_code[str(code)] = {
                "time": group["time"].to_numpy(dtype=str),
                **{name: group[name].to_numpy(dtype=float) for name in REQUIRED_FEATURES},
            }
        return by_code

    @staticmethod
    def _check_feature_alignment(code, today_str, times, features) -> None:
        """
        피처 행과 LOB 행이 같은 시각을 가리키는지 확인한다.

        parquet 은 (code, time) 정렬로 저장되고 LOB 은 삽입 순서로 읽힌다. 정상
        거래일에는 둘이 같지만, 한 칸이라도 밀리면 전략이 t 시점에 다른 시각의
        피처를 보게 된다 — 에러 없이 결과만 바뀌는 종류의 사고다. 종목당 1회 비교로 막는다.
        """
        feature_times = features["time"]
        if len(feature_times) != len(times) or not np.array_equal(feature_times, times):
            raise MissingFeaturesError(
                f"{today_str}/{code}: 피처 행과 LOB 행이 어긋납니다 "
                f"(피처 {len(feature_times)}행 vs LOB {len(times)}행). "
                f"해당 날짜 피처를 다시 빌드하세요"
            )

    def _process_stock(self, conn, code_col, code, today_str, features=None):
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

            # 상한가·호가단위 — engine/utils.py 에 구현되어 있었지만 호출되지 않던
            # calculate_upperlimit()/calculate_ticksize() 를 연결한다 (§1.6).
            #
            #   upper      전일 종가 기준 +30% 호가단위 절사가.
            #              과거에는 opens[0] * 1.3 (당일 시가 기준, 호가단위 미절사) 이었다.
            #              전일 종가 데이터가 없으면(데이터셋 첫 거래일) 그 근사치로 대체한다.
            #   tick_rate  당일 시가 기준 호가단위 비율(%). 과거에는 상수 0.1 이었다.
            #              check_entry_conditions() 의 `tick_rate <= 0.18` 필터가 이제
            #              종목·날짜별로 실제 호가단위를 반영해 판단한다.
            #
            # ⚠️ 두 값 모두 백테스트 결과를 바꾼다 — 상수가 아니라 실제 계산이기 때문이다.
            prev_close = self._prev_close(code_col, today_str)
            if prev_close is not None:
                upper = calculate_upperlimit(prev_close, today_str)
            else:
                upper = opens[0] * 1.3
            tick_rate = (
                calculate_ticksize(opens[0], today_str) / opens[0] * 100
                if opens[0] > 0 else 0.1
            )

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
                'max_t': 0.0, 'min_t': 999999999.0, 'upper': upper,
                'tick_rate': tick_rate, 'max_cbv5': 0.0, 'max_cbv10': 0.0, 'max_cbv30': 0.0, 'max_cbv60': 0.0,
                'tmax_cbv5': 0.0, 'tmax_cbv10': 0.0, 'tmax_cbv30': 0.0, 'tmax_cbv60': 0.0, 'tmax_cbv1': 0.0
            }

            if self.feature_source == FEATURE_SOURCE_STORE:
                self._check_feature_alignment(code, today_str, times, features)

            for t in range(len(stock['time'])):
                time_str = str(stock['time'][t])

                # 캔들 데이터 업데이트 (candle_close 는 어디서도 읽지 않아 제거함 — §1.6)
                stock['candle_high'].append(stock['high'][t])
                stock['candle_low'].append(stock['low'][t])
                stock['candle_open'].append(stock['open'][t])

                # 윈도우 수치 — 스토어 경로는 조회만, 인라인 경로는 매 t 재계산
                if self.feature_source == FEATURE_SOURCE_STORE:
                    apply_stored_metrics(stock, t, features)
                else:
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
            # stock 딕셔너리는 try 블록 중간(위 stock = {...})에서만 만들어진다.
            # 그 전에 예외가 나면(예: BLOB 오염 데이터로 astype(float) 실패) stock 이
            # 아직 없어서 stock['name'] 참조 자체가 UnboundLocalError 를 내고
            # 원래 예외를 덮어써 버린다. locals() 로 존재를 확인하고 안전하게 대체한다.
            stock_label = locals().get("stock", {}).get("name") or locals().get("stock_name", code)
            print(f"❌ 종목 [{stock_label}({code})] 처리 중 에러 발생: {e}")
            traceback.print_exc()