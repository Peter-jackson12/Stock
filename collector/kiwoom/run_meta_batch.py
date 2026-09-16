"""32비트 전용 opt10001 파일럿 배치. 실시간 틱 수집기와 별도 실행."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import struct
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from collector.kiwoom.meta_batch import BatchController, BatchStore, validate_job

STATE = ROOT / "sampledata" / "kiwoom_meta" / "state.db"
FIELDS = ["종목코드", "종목명", "상장주식", "시가총액", "유통주식", "유통비율",
          "현재가", "기준가", "시가", "고가", "저가", "거래량"]


class ProcessLock:
    def __enter__(self):
        import msvcrt
        STATE.parent.mkdir(parents=True, exist_ok=True)
        self.file = (STATE.parent / ".batch.lock").open("a+b")
        self.file.seek(0, 2)
        if not self.file.tell():
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.file.close()
            raise RuntimeError("another metadata batch is running")
        return self

    def __exit__(self, *args):
        self.file.close()  # OS가 프로세스 종료 때도 잠금을 해제한다.


def run(job):
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QAxContainer import QAxWidget
    from PyQt5.QtCore import QTimer

    app = QApplication(sys.argv)
    ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
    if ocx.isNull():
        raise RuntimeError("Kiwoom OCX unavailable")
    store = BatchStore(STATE, job)
    screen = "7901"
    login_started = time.monotonic()
    logged_in = False

    def send(rqname, code):
        ocx.dynamicCall("SetInputValue(QString, QString)", "종목코드", code)
        return int(ocx.dynamicCall("CommRqData(QString, QString, int, QString)", rqname, "opt10001", 0, screen))

    controller = BatchController(store, send)

    def on_login(error):
        nonlocal logged_in
        if error != 0:
            controller.stop(f"login_failed: {error}")
            return
        # 설치된 KOA 확장 함수로 실제 서버를 확인한다. 알 수 없는 값은 중단.
        try:
            flag = str(ocx.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", "")).strip()
        except Exception as exc:
            controller.stop(f"server_lookup_failed: {exc}")
            return
        actual = {"1": "mock", "0": "live"}.get(flag)
        if actual != job["server"]:
            controller.stop(f"server_mismatch: expected={job['server']}, observed={actual!r} (raw={flag!r})")
            return
        logged_in = True
        print(f"로그인 확인: {actual}; 이미 완료 {len(store.complete_codes())}/{len(job['codes'])}", flush=True)

    def on_data(scr, rqname, trcode, record, prev, *_unused):
        if scr != screen or trcode.lower() != "opt10001" or not controller.active:
            return
        if rqname != f"meta_{controller.active[0]}":
            return
        try:
            raw = {field: str(ocx.dynamicCall("GetCommData(QString, QString, int, QString)",
                                            trcode, record, 0, field)).strip() for field in FIELDS}
            controller.receive(rqname, raw)
            print(f"응답 보관: {raw.get('종목코드')}; 완료 {len(store.complete_codes())}/{len(job['codes'])}", flush=True)
        except Exception as exc:
            controller.stop(f"response_error: {exc}")

    def on_message(scr, rqname, trcode, message):
        if scr == screen:
            print(f"키움 메시지 [{rqname}]: {message}", flush=True)

    def tick():
        try:
            if not logged_in and time.monotonic() - login_started > 120:
                controller.stop("login_timeout")
            if logged_in:
                if int(ocx.dynamicCall("GetConnectState()")) != 1:
                    controller.stop("disconnected")
                controller.pump()
            if controller.stopped:
                print(f"배치 종료: {controller.stopped}", flush=True)
                app.exit(0 if controller.stopped == "complete" else 2)
        except Exception as exc:
            controller.stop(f"batch_error: {exc}")
            print(f"배치 오류: {exc}", flush=True)
            app.exit(2)

    ocx.OnEventConnect.connect(on_login)
    ocx.OnReceiveTrData.connect(on_data)
    ocx.OnReceiveMsg.connect(on_message)
    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    previous = signal.signal(signal.SIGINT, lambda *_: controller.stop("user_interrupt"))
    try:
        if int(ocx.dynamicCall("CommConnect()")) != 0:
            controller.stop("login_request_failed")
        exit_code = app.exec_()
        # Qt 창 종료 자체의 0은 배치 완료를 뜻하지 않는다.
        return 0 if exit_code == 0 and controller.stopped == "complete" else 2
    finally:
        timer.stop()
        if controller.active:
            controller.stop("process_exit")
        signal.signal(signal.SIGINT, previous)
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args(argv)
    if struct.calcsize("P") != 4:
        parser.error("use .venv32/Scripts/python.exe")
    job = validate_job(json.loads(args.job.read_text(encoding="utf-8")))
    from datetime import datetime
    from collector.kiwoom.stock_meta import KST
    if job["date"] != datetime.now(KST).strftime("%Y%m%d"):
        parser.error("job must be prepared for today; historical requests are not supported")
    with ProcessLock():
        return run(job)


if __name__ == "__main__":
    raise SystemExit(main())
