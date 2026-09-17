"""별도 OCX 소규모 모의 시세 검증. 주문 없음, 원본 수집 DB 접근 없음."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from uuid import uuid4

from collector.kiwoom.nxt_probe import KST, observation, prepare_plan


class Probe:
    def __init__(self, app, ocx, timer, directory, plan):
        self.app, self.ocx, self.timer = app, ocx, timer
        self.directory, self.plan = Path(directory), plan
        self.directory.mkdir(parents=True, exist_ok=False)
        self.stream = (self.directory / "observations.jsonl").open("x", encoding="utf-8")
        self.session = self.directory.name
        self.server = None
        self.index = -1
        self.active = ""
        self.deadline = time.monotonic() + 180
        self.done = False
        self.counts = Counter()
        self.events = 0
        self.exit_code = 2
        self.write("plan", plan=plan)

    def write(self, kind, **values):
        self.stream.write(json.dumps(dict(kind=kind,
            at_utc=datetime.now(timezone.utc).isoformat(), **values), ensure_ascii=False) + "\n")
        self.stream.flush()

    def login(self, error):
        if self.done:
            return
        if error != 0 or self.server is not None:
            self.finish("login_error_or_reconnect")
            return
        flag = str(self.ocx.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", "")).strip()
        self.write("server", flag=flag)
        if flag != "1":
            self.finish("mock_server_required")
            return
        self.server = "mock"
        self.advance()

    def advance(self):
        self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        self.index += 1
        if self.index == len(self.plan["subscription_codes"]):
            self.finish("observation_windows_completed", success=True)
            return
        self.active = self.plan["subscription_codes"][self.index]
        self.deadline = time.monotonic() + self.plan["duration_seconds"]
        result = self.ocx.dynamicCall("SetRealReg(QString, QString, QString, QString)",
                                     str(9100 + self.index), self.active, "10;15;20;21;41;61", "0")
        self.write("registration", subscription_code=self.active, result=result,
                   attribution="active window only; late callbacks may cross registration boundaries")
        print(f"구독 요청 {self.active}: 반환 {result}; {self.plan['duration_seconds']}초 관측", flush=True)

    def receive(self, code, real_type, raw_data):
        if self.done or self.server is None:
            return
        try:
            fids = {}
            selected = ((20, 10, 15, 14, 27, 28) if real_type == "주식체결" else
                        (21, *range(41, 81)) if real_type == "주식호가잔량" else ())
            for fid in selected:
                fids[str(fid)] = str(self.ocx.dynamicCall("GetCommRealData(QString, int)", code, fid))
            packet = observation(subscription_code=self.active, callback_code=code,
                real_type=real_type, fids=fids, received_at_utc=datetime.now(timezone.utc).isoformat(),
                received_ns=time.perf_counter_ns(), session_id=self.session, server=self.server)
            self.write("callback", packet=packet, callback_data_raw=raw_data)
            self.counts[(self.active, code, real_type)] += 1
            self.events += 1
            if self.events >= 50000:
                self.finish("event_safety_limit")
        except Exception as exc:
            self.finish(f"callback_error:{type(exc).__name__}:{exc}")

    def tick(self):
        if self.done:
            return
        if time.monotonic() >= self.deadline:
            if self.server is None:
                self.finish("login_timeout")
            else:
                self.advance()

    def finish(self, reason, success=False):
        if self.done:
            return
        self.done = True
        self.timer.stop()
        removal_error = None
        try:
            self.ocx.dynamicCall("SetRealRemove(QString, QString)", "ALL", "ALL")
        except Exception as exc:
            removal_error = str(exc)
        self.exit_code = 0 if success and removal_error is None else 2
        result = dict(reason=reason, server=self.server, events=self.events,
            exit_code=self.exit_code, removal_error=removal_error,
            counts=[dict(subscription_code=k[0], callback_code=k[1], real_type=k[2], count=v)
                    for k, v in self.counts.items()],
            venue="unknown", nxt_coverage="unconfirmed",
            note="창 종료는 NXT 지원·무누락 인증이 아님; 0건도 미지원 증거가 아님")
        try:
            self.write("finish", result=result)
            self.stream.close()
            (self.directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            print(f"검증 종료: {reason}, 이벤트 {self.events}건", flush=True)
            self.app.quit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", default="005930")
    parser.add_argument("--seconds", type=int, default=60)
    args = parser.parse_args()
    plan = prepare_plan(args.code, server="mock", start_at=datetime.now(KST).isoformat(),
                        duration_seconds=args.seconds)
    # 검증 계획의 순서를 그대로 사용하며 프로덕션의 15:35 종료 조건은 사용하지 않는다.
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QAxContainer import QAxWidget
    from PyQt5.QtCore import QTimer
    from collector.kiwoom.collector_lease import CollectorLease
    from collector.kiwoom.capture_diagnostics import CaptureDiagnostics
    root = Path(__file__).resolve().parents[2]
    directory = root / "operations_state" / "nxt_probes" / uuid4().hex
    with CollectorLease(root), CaptureDiagnostics(root / "logs"):
        app = QApplication(sys.argv)
        ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
        timer = QTimer()
        probe = Probe(app, ocx, timer, directory, plan)
        ocx.OnEventConnect.connect(probe.login)
        ocx.OnReceiveRealData.connect(probe.receive)
        timer.timeout.connect(probe.tick)
        app.aboutToQuit.connect(lambda: probe.finish("application_exit"))
        timer.start(250)
        print(f"결과 폴더: {directory}\n모의투자 접속으로 로그인해 주세요.", flush=True)
        try:
            result = ocx.dynamicCall("CommConnect()")
            probe.write("login_request", result=result)
            if result != 0:
                probe.finish("login_request_rejected")
            if not probe.done:
                app.exec_()
        finally:
            probe.finish("runner_exit")
        return probe.exit_code


if __name__ == "__main__":
    sys.exit(main())
