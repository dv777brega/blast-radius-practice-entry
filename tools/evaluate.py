"""Validation only. Score predictions and write per-commit selection slack."""

import argparse
import csv
import json
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from score import evaluate_run, run_total_microseconds


def summarize(values):
    ordered = sorted(values)
    return {f"p{p}": ordered[math.ceil(len(ordered) * p / 100) - 1]
            for p in (50, 90, 99, 100)}


def analyze(inventory, labels, output, cpu):
    tests = json.loads(inventory.read_text())["tests"]
    answers = json.loads(labels.read_text())["commits"]
    prediction = output.read_bytes()
    evaluation = evaluate_run(prediction, tests, answers)
    if not evaluation["ok"]:
        raise ValueError(f"Rejected by scorer: {evaluation}")
    ms = {row["number"]: math.floor(row["seconds"] * 1000 + 0.5) for row in tests}
    results = []
    for line in prediction.splitlines():
        record = json.loads(line)
        case = record["commit"]
        workers = [w for w in record["workers"] if w]
        chosen = {n for w in workers for n in w}
        truth = set(answers[case]["failing"])
        missed = sorted(truth - chosen)
        cost = max((sum(ms[n] for n in w) for w in workers), default=0) + 2000 * len(workers)
        results.append({
            "commit": case,
            "workers": len(workers),
            "selected": len(chosen),
            "failing": len(truth),
            "extra": len(chosen - truth),
            "extra_test_ms": sum(ms[n] for n in chosen - truth),
            "missed_numbers": " ".join(map(str, missed)),
            "selection_cost_ms": cost,
            "simulated_ms": cost + 300000 * bool(missed),
        })
    total = sum(row["simulated_ms"] for row in results)
    if total != evaluation["simulated_milliseconds"]:
        raise AssertionError("Per-commit costs disagree with score.py")
    report = {"commits": len(results), "misses": len(evaluation["missed_commits"]),
              "simulated_seconds": total / 1000, "missed_commits": evaluation["missed_commits"]}
    if results:
        report["cost_ms"] = summarize([r["simulated_ms"] for r in results])
        report["extra_test_ms"] = summarize([r["extra_test_ms"] for r in results])
        report["miss_rate"] = report["misses"] / len(results)
        report["same_mix_1.5x_simulated_estimate"] = total / 1000 * 1.5
    if cpu is not None:
        report["cpu_seconds"] = cpu
        report["total_seconds"] = run_total_microseconds(total, round(cpu * 1000000)) / 1000000
    return report, results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("answers", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--cpu-seconds", type=float)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--horizon", type=int, default=45, help="commits in mix projections")
    options = parser.parse_args()
    report, rows = analyze(options.inventory, options.answers, options.predictions, options.cpu_seconds)
    if options.horizon < 1:
        parser.error("--horizon must be positive")
    affected = [r["simulated_ms"] for r in rows if r["failing"]]
    clean = [r["simulated_ms"] for r in rows if not r["failing"]]
    if clean and affected:
        mean_clean = sum(clean) / len(clean)
        mean_affected = sum(affected) / len(affected)
        report["mix_projection_commits"] = options.horizon
        report["projected_simulated_seconds_by_fraction_with_failures"] = {
            str(round(share, 3)): round(options.horizon *
                (share * mean_affected + (1 - share) * mean_clean) / 1000, 3)
            for share in sorted({0.25, len(affected) / len(rows), 0.75, 0.9})
        }
    if options.csv and rows:
        with options.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda r: r["commit"]))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
