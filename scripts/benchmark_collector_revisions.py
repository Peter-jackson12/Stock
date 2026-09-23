"""GitHub-hosted fixed-input comparison for historical collector revisions.

This is a synthetic Python-path benchmark. It does not create an OCX login, read
operational raw/evidence, or certify native/32-bit/live-market performance.
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

REVISIONS = (
    ("2026-09-18-mock", "f352e024fdde24b966846b783b60eb4dd9d45495"),
    ("2026-09-21-live", "4821762fd93230b658339fee084d6c08e3e53ce9"),
    ("2026-09-22-live", "6a6d6076649befc767e5d8d59151cbcfb2f27c34"),
    ("2026-09-23-live", "5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70"),
)
CURRENT_TELEMETRY_REVISION = "5b5156f810b7852c6b5fa4b5c42b77ddfbca0a70"
PREFIX = "REV_BENCH_JSON "


def _run(command, *, cwd=None, check=True, capture_output=False):
    return subprocess.run(
        command,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture_output,
    )


def ensure_history(repo: Path) -> None:
    missing = []
    for _, revision in REVISIONS:
        probe = subprocess.run(
            ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
            cwd=repo,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode:
            missing.append(revision)
    if not missing:
        return
    # The repository is public. Fetch bounded master history only on the hosted
    # runner; this is not a local-user checkout workflow.
    _run(["git", "fetch", "--no-tags", "--depth=256", "origin", "master"], cwd=repo)
    for revision in missing:
        _run(["git", "cat-file", "-e", f"{revision}^{{commit}}"], cwd=repo)


def checkout_revision(repo: Path, revision: str, target: Path) -> None:
    _run(["git", "worktree", "add", "--detach", str(target), revision], cwd=repo)


def remove_worktree(repo: Path, target: Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(target)],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def child_command(script: Path, checkout: Path, label: str, revision: str,
                  pairs: int, repeats: int, telemetry: bool):
    return [
        sys.executable, str(script),
        "--child",
        "--checkout", str(checkout),
        "--label", label,
        "--revision", revision,
        "--pairs", str(pairs),
        "--repeats", str(repeats),
        "--telemetry", "on" if telemetry else "off",
    ]


def run_revision(repo: Path, script: Path, label: str, revision: str,
                 pairs: int, repeats: int, telemetry: bool):
    with tempfile.TemporaryDirectory(prefix="stock_revision_bench_") as td:
        checkout = Path(td) / "checkout"
        checkout_revision(repo, revision, checkout)
        try:
            result = _run(
                child_command(script, checkout, label, revision, pairs, repeats, telemetry),
                cwd=checkout,
                check=False,
                capture_output=True,
            )
            if result.returncode:
                raise RuntimeError(
                    f"benchmark child failed for {label} telemetry={telemetry}: "
                    f"exit={result.returncode}\nstdout:\n{result.stdout[-8000:]}\n"
                    f"stderr:\n{result.stderr[-8000:]}"
                )
        finally:
            remove_worktree(repo, checkout)
    payloads = [
        json.loads(line[len(PREFIX):])
        for line in result.stdout.splitlines()
        if line.startswith(PREFIX)
    ]
    if len(payloads) != 1:
        raise RuntimeError(
            f"expected exactly one benchmark payload for {label}; "
            f"stdout={result.stdout[-4000:]!r} stderr={result.stderr[-4000:]!r}"
        )
    return payloads[0]


class Signal:
    def connect(self, fn):
        self.fn = fn

    def emit(self, *args):
        fn = getattr(self, "fn", None)
        if fn is not None:
            return fn(*args)
        return None


class FakeApp:
    def __init__(self, _):
        self.aboutToQuit = Signal()
        self.quits = 0

    def quit(self):
        self.quits += 1

    def exec_(self):
        return 0


class FakeTimer:
    def __init__(self):
        self.timeout = Signal()
        self.running = False

    def start(self, _):
        self.running = True

    def stop(self):
        self.running = False


class FakeOcx:
    def __init__(self, _):
        self.OnEventConnect = Signal()
        self.OnReceiveRealData = Signal()
        self.fid_calls = 0
        self.values = {
            20: "090000", 10: "+10000", 15: "+2", 14: "100000",
            27: "10001", 28: "10000", 21: "090000",
        }
        self.values.update({fid: "10001" for fid in range(41, 51)})
        self.values.update({fid: "10000" for fid in range(51, 61)})
        self.values.update({fid: "3" for fid in range(61, 81)})

    def dynamicCall(self, method, *args):
        if method.startswith("KOA_Functions"):
            return "1"  # fixed mock classification on every revision
        if method.startswith("GetConnectState"):
            return 1
        if method.startswith("GetCodeListByMarket"):
            return "005930;" if args and args[0] == "0" else ""
        if method.startswith("GetMasterCodeName"):
            return "삼성전자"
        if method.startswith("GetCommRealData"):
            self.fid_calls += 1
            return self.values[args[1]]
        if method.startswith("SetRealReg") or method.startswith("SetRealRemove"):
            return "0"
        return "0"


class DummyLog:
    def __init__(self, _):
        self.closed = False

    def emit(self, _):
        return None

    def emit_all(self, _):
        return None

    def status(self, _):
        return None

    def end_status_line(self):
        return None

    def close(self):
        self.closed = True


class DummyResourceHistory:
    def __init__(self, *_args, **_kwargs):
        pass

    def sample(self, *args, **kwargs):
        return None

    def snapshot(self):
        return None


def install_fake_qt():
    import types

    modules = {
        "PyQt5": types.SimpleNamespace(),
        "PyQt5.QtWidgets": types.SimpleNamespace(QApplication=FakeApp),
        "PyQt5.QtCore": types.SimpleNamespace(QTimer=FakeTimer),
        "PyQt5.QAxContainer": types.SimpleNamespace(QAxWidget=FakeOcx),
    }
    for name, value in modules.items():
        sys.modules[name] = value


def import_historical_logger(checkout: Path):
    install_fake_qt()
    sys.path.insert(0, str(checkout))
    import importlib.util

    path = checkout / "collector" / "kiwoom" / "kiwoom_universe_logger.py"
    spec = importlib.util.spec_from_file_location("revision_benchmark_logger", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load historical logger: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def configure_module(module, root: Path, revision: str):
    if not hasattr(module, "_benchmark_original_capture"):
        module._benchmark_original_capture = module.LiveRawCapture
    original_capture = module._benchmark_original_capture
    facts = SimpleNamespace(
        pid=os.getpid(),
        started_at_utc="2026-09-23T00:00:00+00:00",
        executable=str(Path(sys.executable).resolve()),
        python_bits=32,
    )

    def capture_factory(_root, **kwargs):
        return original_capture(root, facts=facts, **kwargs)

    module.LiveRawCapture = capture_factory
    module.ResourceHistory = DummyResourceHistory
    module.SessionLog = DummyLog
    module.PROJECT_ROOT = root
    module.RAW_DIR = root / "sampledata" / "raw_ticks"
    module.LOG_DIR = root / "logs"
    module.LOG_DIR.mkdir(parents=True, exist_ok=True)


def make_logger(module, revision: str, telemetry: bool):
    kwargs = dict(storage="raw-v2", code_revision=revision)
    params = inspect.signature(module.KiwoomUniverseLogger).parameters
    if "capture_telemetry" in params:
        kwargs["capture_telemetry"] = telemetry
    elif telemetry:
        raise RuntimeError("telemetry requested on revision without telemetry support")
    if "explicit_ocx_teardown" in params:
        kwargs["explicit_ocx_teardown"] = False
    logger = module.KiwoomUniverseLogger(**kwargs)
    logger._stats_worker = lambda: None
    logger._register_all_universe = lambda: None
    logger.ocx.dynamicCall = FakeOcx("fixture").dynamicCall
    # Preserve the concrete fake object for FID call accounting.
    fake = FakeOcx("fixture")
    logger.ocx.dynamicCall = fake.dynamicCall
    logger._benchmark_fake_ocx = fake
    logger._on_login(0)
    if logger.raw_capture is None:
        raise RuntimeError("historical revision did not create raw-v2 capture")
    return logger


def one_run(module, root: Path, revision: str, pairs: int, telemetry: bool):
    logger = make_logger(module, revision, telemetry)
    fake = logger._benchmark_fake_ocx
    expected_callbacks = pairs * 2
    expected_fids = pairs * (6 + 41)

    start = time.perf_counter()
    for _ in range(pairs):
        logger._on_receive_real_data("005930", "주식체결", "")
        logger._on_receive_real_data("005930", "주식호가잔량", "")
    submitted = time.perf_counter()
    logger._shutdown("fixed-input revision benchmark")
    finished = time.perf_counter()
    # Current production main() performs process-resource cleanup in finally
    # after the Qt shutdown path returns. Keep it outside the measured service
    # interval, but execute it so optional telemetry/teardown handles follow
    # the real lifetime and temporary evidence can be released on Windows.
    finish_resources = getattr(logger, "_finish_process_resources", None)
    if finish_resources is not None:
        finish_resources()

    snapshot = logger.raw_capture.queue.snapshot()
    if logger.exit_code != 0:
        raise RuntimeError(f"collector exit_code={logger.exit_code}, snapshot={snapshot}")
    if snapshot["accepted_callbacks"] != expected_callbacks:
        raise RuntimeError(
            f"accepted mismatch: {snapshot['accepted_callbacks']} != {expected_callbacks}"
        )
    if snapshot["committed_callbacks"] != expected_callbacks:
        raise RuntimeError(
            f"committed mismatch: {snapshot['committed_callbacks']} != {expected_callbacks}"
        )
    if snapshot["state"] != "closed" or not snapshot["writer_closed"] or snapshot["error"]:
        raise RuntimeError(f"capture did not close cleanly: {snapshot}")
    if fake.fid_calls != expected_fids:
        raise RuntimeError(f"FID read count changed: {fake.fid_calls} != {expected_fids}")

    callback_seconds = submitted - start
    end_to_end_seconds = finished - start
    return {
        "callbacks": expected_callbacks,
        "fid_calls": fake.fid_calls,
        "callback_seconds": callback_seconds,
        "callback_rate": expected_callbacks / callback_seconds,
        "end_to_end_seconds": end_to_end_seconds,
        "end_to_end_rate": expected_callbacks / end_to_end_seconds,
        "committed_seq": snapshot["committed_seq"],
    }


def median(values):
    return float(statistics.median(values))


def child_main(args):
    checkout = Path(args.checkout).resolve()
    module = import_historical_logger(checkout)
    telemetry = args.telemetry == "on"

    with tempfile.TemporaryDirectory(prefix="stock_fixed_input_") as td:
        base = Path(td)
        # Warm the import/SQLite path without including it in measured medians.
        warm_root = base / "warm"
        configure_module(module, warm_root, args.revision)
        one_run(module, warm_root, args.revision, min(100, args.pairs), telemetry)

        runs = []
        for index in range(args.repeats):
            run_root = base / f"run_{index}"
            configure_module(module, run_root, args.revision)
            runs.append(one_run(module, run_root, args.revision, args.pairs, telemetry))

    payload = {
        "label": args.label,
        "revision": args.revision,
        "telemetry": telemetry,
        "pairs": args.pairs,
        "repeats": args.repeats,
        "callback_rate_median": median([x["callback_rate"] for x in runs]),
        "end_to_end_rate_median": median([x["end_to_end_rate"] for x in runs]),
        "callback_seconds_median": median([x["callback_seconds"] for x in runs]),
        "end_to_end_seconds_median": median([x["end_to_end_seconds"] for x in runs]),
        "fid_calls_each": [x["fid_calls"] for x in runs],
        "committed_seq_each": [x["committed_seq"] for x in runs],
        "callback_rate_each": [x["callback_rate"] for x in runs],
        "end_to_end_rate_each": [x["end_to_end_rate"] for x in runs],
    }
    print(PREFIX + json.dumps(payload, sort_keys=True))


def parent_main(args):
    repo = Path(__file__).resolve().parents[1]
    ensure_history(repo)
    script = Path(__file__).resolve()

    results = []
    for label, revision in REVISIONS:
        result = run_revision(
            repo, script, label, revision, args.pairs, args.repeats, telemetry=False
        )
        results.append(result)
        print(
            "REVISION_BENCH "
            f"{label} telemetry=off "
            f"callback_rate={result['callback_rate_median']:.1f}/s "
            f"end_to_end_rate={result['end_to_end_rate_median']:.1f}/s"
        )

    telemetry_result = run_revision(
        repo, script, "2026-09-23-live", CURRENT_TELEMETRY_REVISION,
        args.pairs, args.repeats, telemetry=True,
    )
    results.append(telemetry_result)
    print(
        "REVISION_BENCH "
        "2026-09-23-live telemetry=on "
        f"callback_rate={telemetry_result['callback_rate_median']:.1f}/s "
        f"end_to_end_rate={telemetry_result['end_to_end_rate_median']:.1f}/s"
    )

    baseline = results[0]["callback_rate_median"]
    for result in results:
        result["callback_rate_vs_0918"] = result["callback_rate_median"] / baseline

    print("REVISION_BENCHMARK_SUMMARY " + json.dumps(results, sort_keys=True))


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--pairs", type=int, default=2000)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--child", action="store_true")
    parser.add_argument("--checkout")
    parser.add_argument("--label")
    parser.add_argument("--revision")
    parser.add_argument("--telemetry", choices=("on", "off"), default="off")
    args = parser.parse_args(argv)
    if args.pairs < 100 or args.pairs > 4000:
        parser.error("--pairs must be between 100 and 4000")
    if args.repeats < 1 or args.repeats > 5:
        parser.error("--repeats must be between 1 and 5")
    if args.child and not all((args.checkout, args.label, args.revision)):
        parser.error("--child requires --checkout, --label, and --revision")
    return args


if __name__ == "__main__":
    parsed = parse_args()
    if parsed.child:
        child_main(parsed)
    else:
        parent_main(parsed)
