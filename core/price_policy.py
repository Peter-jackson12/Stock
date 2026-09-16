"""D-6 provenance gate for daily CSVs consumed as absolute price levels.

The sidecar describes files in its own directory, never its parent/children.
An 'actual' declaration is a source contract, not independent verification.
"""
import json
from pathlib import Path

PRICE_MANIFEST = "_price_manifest.json"
PRICE_FILES = frozenset({"open.csv", "high.csv", "low.csv", "close.csv"})
ACTUAL = "actual_at_event_time"
ADJUSTED = "split_adjusted_observed"


class PriceBasisError(ValueError):
    pass


def check_daily_price_basis(directory, *, require_actual=False):
    directory = Path(directory)
    # Covers historical quarantine exports produced before the sidecar existed.
    if "unverified_fchart" in {part.casefold() for part in directory.resolve().parts}:
        raise PriceBasisError("D-6: fchart 격리 가격은 절대 가격 판단에 사용할 수 없습니다")
    path = directory / PRICE_MANIFEST
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if require_actual:
            raise PriceBasisError(f"D-6: 실제가 출처 선언이 없습니다: {path}")
        return {"price_basis": "unknown_legacy", "source": "undeclared"}
    except (OSError, UnicodeError) as exc:
        raise PriceBasisError(f"D-6: 가격 출처 선언을 읽을 수 없습니다: {path}") from exc
    try:
        manifest = json.loads(raw)
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise ValueError("unsupported schema")
        source = manifest["source"]
        basis = manifest["price_basis"]
        files = manifest["files"]
        if not isinstance(source, str) or not source.strip():
            raise ValueError("missing source")
        if basis not in (ACTUAL, ADJUSTED, "unknown"):
            raise ValueError("unsupported price basis")
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ValueError("invalid file scope")
        present = {name for name in PRICE_FILES if (directory / name).exists()}
        if not present.issubset(set(files)):
            raise ValueError("price files outside manifest scope")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PriceBasisError(f"D-6: 가격 출처 선언을 읽거나 검증할 수 없습니다: {path}: {exc}") from exc
    if basis == ADJUSTED:
        raise PriceBasisError(f"D-6: 수정주가({source})는 절대 가격 판단에 사용할 수 없습니다")
    if require_actual and basis != ACTUAL:
        raise PriceBasisError(f"D-6: 실제가 출처가 확정되지 않았습니다: {basis}")
    return {"price_basis": basis, "source": source}
