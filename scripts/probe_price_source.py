"""Bounded KRX/pykrx source diagnostic; never certifies or writes Daily prices."""
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import sys
from urllib.parse import urlparse
from uuid import uuid4

import requests

ROOT = Path(__file__).resolve().parents[1]


class ProbeTransport:
    def __init__(self, directory, original):
        self.directory, self.original = Path(directory), original
        self.count = 0
        self.blocked = False
        self.records = []

    def request(self, session, method, url, **kwargs):
        if self.blocked or self.count >= 4:
            raise requests.RequestException("source probe request budget exhausted or provider refused access")
        if urlparse(url).hostname != "data.krx.co.kr":
            raise requests.RequestException("unapproved source host")
        self.count += 1
        kwargs.update(timeout=10, allow_redirects=False, stream=True)
        record = dict(method=method, url=url, status=None, error=None)
        self.records.append(record)
        try:
            response = self.original(session, method, url, **kwargs)
        except requests.RequestException as exc:
            record["error"] = type(exc).__name__
            raise
        try:
            body = bytearray()
            for chunk in response.iter_content(65536):
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise requests.RequestException("source response exceeds 2 MiB")
            response._content = bytes(body)
            response._content_consumed = True
            path = self.directory / f"response_{self.count}.bin"
            path.write_bytes(body)
            record.update(status=response.status_code, bytes=len(body), file=path.name)
            if response.status_code >= 300:
                self.blocked = True
                raise requests.RequestException(f"provider HTTP {response.status_code}; no retry")
            return response
        finally:
            response.close()


def probe(directory):
    from pykrx import stock
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    original = requests.sessions.Session.request
    transport = ProbeTransport(directory, original)
    requests.sessions.Session.request = lambda session, method, url, **kwargs: transport.request(session, method, url, **kwargs)
    result = dict(schema="price_source_probe_v1", checked_at_utc=datetime.now(timezone.utc).isoformat(),
        package_version=importlib.metadata.version("pykrx"), code="005930", from_date="20180427", to_date="20180504",
        production_approved=False, daily_written=False, samples={})
    try:
        for name, fetch in (
            ("unadjusted_ohlcv", lambda: stock.get_market_ohlcv_by_date("20180427", "20180504", "005930", adjusted=False)),
            ("historical_shares", lambda: stock.get_market_cap_by_date("20180427", "20180504", "005930")),
        ):
            try:
                frame = fetch()
                result["samples"][name] = dict(rows=len(frame), columns=list(frame.columns),
                    outcome="candidate_requires_independent_validation" if len(frame) else "empty")
                if len(frame):
                    frame.to_csv(directory / f"{name}.csv", encoding="utf-8")
            except Exception as exc:
                result["samples"][name] = dict(outcome="unavailable", error=f"{type(exc).__name__}: {exc}")
    finally:
        requests.sessions.Session.request = original
        result["http"] = transport.records
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    directory = ROOT / "operations_state/price_source_probes" / uuid4().hex
    print(json.dumps(probe(directory), ensure_ascii=False, indent=2))
    print(directory / "result.json")
