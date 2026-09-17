import json
from types import SimpleNamespace

from collector.kiwoom.nxt_probe import prepare_plan
from collector.kiwoom.run_nxt_probe import Probe


def fixture(tmp_path, flag="1"):
    calls = []
    def call(method, *args):
        calls.append((method, args))
        if method.startswith("KOA_Functions"):
            return flag
        if method.startswith("GetCommRealData"):
            return " -2 "
        return 0
    plan = prepare_plan("005930", server="mock", start_at="2026-09-17T15:50:00+09:00")
    probe = Probe(SimpleNamespace(quit=lambda: None), SimpleNamespace(dynamicCall=call),
                  SimpleNamespace(stop=lambda: None), tmp_path / "probe", plan)
    return probe, calls


def test_real_server_rejected_before_subscription(tmp_path):
    probe, calls = fixture(tmp_path, "0")
    probe.login(0)
    assert probe.done and probe.exit_code == 2
    assert not any(method.startswith("SetRealReg") for method, _ in calls)


def test_three_windows_preserve_suffix_raw_and_complete_without_certifying_venue(tmp_path):
    probe, calls = fixture(tmp_path)
    probe.login(0)
    probe.advance()
    probe.receive("005930_NX", "주식체결", "raw original")
    probe.advance()
    probe.advance()
    assert probe.done and probe.exit_code == 0
    codes = [args[1] for method, args in calls if method.startswith("SetRealReg")]
    assert codes == ["005930", "005930_NX", "005930_AL"]
    rows = [json.loads(line) for line in (probe.directory / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    event = next(row for row in rows if row["kind"] == "callback")
    assert event["packet"]["callback_code_raw"] == "005930_NX"
    assert event["packet"]["fids"]["15"] == " -2 "
    assert event["packet"]["venue"] == "unknown"
    assert event["callback_data_raw"] == "raw original"


def test_login_timeout_and_idempotent_finish(tmp_path):
    probe, _ = fixture(tmp_path)
    probe.deadline = 0
    probe.tick()
    probe.finish("again")
    result = json.loads((probe.directory / "result.json").read_text(encoding="utf-8"))
    assert result["reason"] == "login_timeout" and result["events"] == 0
