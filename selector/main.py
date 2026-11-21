"""Offline CI selector. Only scored JSON records go to standard output."""
import argparse
import json
import marshal
import sys
import time
import zlib
from pathlib import Path

from impact import Impact
from schedule import Scheduler


def mask_to_tests(mask, mapped, unknown):
    chosen = set(unknown)
    while mask:
        lsb = mask & -mask
        idx = lsb.bit_length() - 1
        if idx < len(mapped):
            test_num = mapped[idx]
            if test_num is not None:
                chosen.add(test_num)
        mask ^= lsb
    return frozenset(chosen)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("/data"))
    parser.add_argument("--source", type=Path, default=Path("/opt/sqlglot"))
    parser.add_argument("--maps", type=Path, default=Path("/app/maps"))
    parser.add_argument("--risk-scale", type=float, default=1.0, help="scale the insertion-risk allowance")
    args = parser.parse_args()
    started = time.monotonic()
    tests = json.loads((args.data / "tests.json").read_bytes())["tests"]
    all_numbers = frozenset(t["number"] for t in tests)
    schedule = Scheduler(tests)
    engine = None
    mapped = []
    unknown = frozenset()
    try:
        index = marshal.loads(zlib.decompress((args.maps / "index.zlib").read_bytes()))
        if index["version"] != 1 or index["commit"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
            raise ValueError("incompatible coverage index")
        observations = None
        if (args.maps / "observations.zlib").is_file():
            from observations import Observations
            observations = Observations.load(args.maps / "observations.zlib", index["tests"])
            observations.line_coverage = index["lines"]
        engine = Impact(args.source, index, observations)
        inventory = {t["id"]: t["number"] for t in tests}
        mapped = [inventory.get(nodeid) for nodeid in index["tests"]]
        unknown = all_numbers.difference(mapped)
    except Exception as error:
        print(f"coverage unavailable: {error}", file=sys.stderr)
    all_cost = None
    for path in sorted((args.data / "commits").glob("*.patch")):
        try:
            chosen = all_numbers
            if engine is not None and time.monotonic() - started < 155:
                mask = engine.changed(path.read_text("utf-8"))
                if mask != engine.every:
                    chosen = mask_to_tests(mask, mapped, unknown)
                    if engine.risk:
                        if all_cost is None:
                            all_cost = schedule.plan(all_numbers)[0]
                        premium = all_cost - schedule.plan(chosen)[0]
                        if premium < 300_000 * min(1.0, engine.risk * args.risk_scale):
                            chosen = all_numbers
            workers = schedule.plan(chosen)[1]
        except Exception as error:
            print(f"{path.stem}: {type(error).__name__}: {error}", file=sys.stderr)
            workers = schedule.plan(all_numbers)[1]
        print(json.dumps({"commit": path.stem, "workers": workers}, separators=(",", ":")))


if __name__ == "__main__":
    main()
