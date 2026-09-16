import requests
import pytest

from scripts.probe_price_source import ProbeTransport


def response(status=200, body=b"{}"):
    result = requests.Response()
    result.status_code = status
    result._content = body
    result._content_consumed = True
    return result


def test_refusal_stops_without_retry_and_keeps_evidence(tmp_path):
    calls = []
    def send(*args, **kwargs):
        calls.append(kwargs)
        return response(403, b"denied")
    transport = ProbeTransport(tmp_path, send)
    for _ in range(2):
        with pytest.raises(requests.RequestException):
            transport.request(None, "GET", "http://data.krx.co.kr/test")
    assert len(calls) == 1
    assert calls[0]["timeout"] == 10 and not calls[0]["allow_redirects"]
    assert (tmp_path / "response_1.bin").read_bytes() == b"denied"


def test_request_budget_and_source_host_are_enforced(tmp_path):
    transport = ProbeTransport(tmp_path, lambda *a, **k: response())
    with pytest.raises(requests.RequestException):
        transport.request(None, "GET", "https://example.com")
    for _ in range(4):
        transport.request(None, "GET", "https://data.krx.co.kr/test")
    with pytest.raises(requests.RequestException):
        transport.request(None, "GET", "https://data.krx.co.kr/test")
