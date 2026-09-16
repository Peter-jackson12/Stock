import json

import pytest
import requests

from scripts.probe_fchart import prepare, probe

XML = '<protocol><chartdata name="sample"><item data="20260915|10|11|9|10|100"/></chartdata></protocol>'


class Response:
    def __init__(self, status=200, body=XML):
        self.status_code, self.text = status, body
        self.content = body.encode()
        self.closed = False
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)
    def close(self):
        self.closed = True


class Clock:
    def __init__(self):
        self.t = 0
    def __call__(self):
        return self.t
    def sleep(self, secs):
        self.t += secs


def run(tmp_path, replies, n=3):
    clock = Clock()
    responses = iter(replies)
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        clock.sleep(.1)
        value = next(responses)
        if isinstance(value, BaseException):
            raise value
        return value
    result = probe({"codes": [f"{i:06d}" for i in range(1, n+1)]}, tmp_path / "report",
                   environment="agent_shell", get=get, clock=clock, sleep=clock.sleep)
    return result, calls


def test_retries_are_visible_and_only_diagnostics_are_written(tmp_path):
    replies = [Response(500), Response(), Response(), Response()]
    result, calls = run(tmp_path, replies)
    summary = result["summary"]
    assert summary["http_attempts"] == 4
    assert summary["successful_codes"] == 3
    assert summary["first_attempt_failures"] == summary["failed_attempts"] == 1
    assert summary["elapsed_sec"] == 2.9  # .4 HTTP + .5 retry + 2 symbol gaps
    assert summary["latest_dates"] == ["20260915"]
    assert not summary["full_universe_approved"]
    assert all(r.closed for r in replies)
    assert len(calls) == 4
    assert sorted(p.name for p in (tmp_path / "report").iterdir()) == ["attempts.jsonl", "summary.json"]


@pytest.mark.parametrize("status", [403, 429])
def test_block_response_stops_without_retry(tmp_path, status):
    result, calls = run(tmp_path, [Response(status)])
    assert len(calls) == 1
    assert result["summary"]["stopped_reason"] == f"http_{status}"
    assert result["summary"]["unattempted_codes"] == 2


def test_three_consecutive_failures_stop_and_preserve_attempt_log(tmp_path):
    result, calls = run(tmp_path, [requests.Timeout()] * 9, n=5)
    assert len(calls) == 9
    assert result["summary"]["stopped_reason"] == "three_consecutive_failed_codes"
    assert result["summary"]["failed_codes"] == 3
    assert result["summary"]["unattempted_codes"] == 2
    events = [json.loads(s) for s in (tmp_path / "report/attempts.jsonl").read_text().splitlines()]
    assert len(events) == 20  # start + 9 durable request starts + 9 outcomes + finish


@pytest.mark.parametrize("body", ["<bad", "<protocol/>", XML.replace("|10|11", "|nan|11")])
def test_http_200_bad_payload_is_not_success(tmp_path, body):
    result, calls = run(tmp_path, [Response(body=body)] * 3)
    assert len(calls) == 3  # parsing/empty data is not retried
    assert result["summary"]["successful_codes"] == 0


def test_interruption_leaves_partial_summary(tmp_path):
    result, calls = run(tmp_path, [Response(), KeyboardInterrupt()])
    assert result["summary"]["stopped_reason"] == "interrupted"
    assert result["summary"]["successful_codes"] == 1
    assert result["summary"]["http_attempts"] == 2
    assert (tmp_path / "report/summary.json").exists()


def test_output_cannot_overwrite_existing_report(tmp_path):
    run(tmp_path, [Response()] * 3)
    with pytest.raises(FileExistsError):
        run(tmp_path, [Response()] * 3)


def test_plan_uses_universe_and_fails_if_requested_sample_unavailable():
    first = prepare("blue_chips", 3, 0)
    assert set(first["codes"]) == {"000270", "000660", "005930"}
    assert first == prepare("blue_chips", 3, 0)
    with pytest.raises(ValueError, match="only 3"):
        prepare("blue_chips", 50)
