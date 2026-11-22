"""Validation only: run the image under scoring limits and charge its entry process via wait4."""
import argparse
import json
import math
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from score import evaluate_run

CLOCK = """import os,sys
cmd=sys.argv[1:]
pid=os.posix_spawnp(cmd[0],cmd,os.environ)
_,status,usage=os.wait4(pid,0)
print('ENTRY_CPU_US='+str(round(1000000*(usage.ru_utime+usage.ru_stime))),file=sys.stderr)
sys.exit(os.waitstatus_to_exitcode(status))
"""


def run(image, data, answers, command):
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    start = time.monotonic()
    result = subprocess.run([
        "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
        "--cpus", "2", "--memory", "4096m", "--memory-swap", "4096m", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,size=64m,mode=1777", "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges", "--user", "65534:65534", "--env", "HOME=/tmp",
        "--volume", f"{data / 'commits'}:/data/commits:ro",
        "--volume", f"{data / 'tests.json'}:/data/tests.json:ro",
        "--entrypoint", "python", image, "-c", CLOCK, *command
    ], capture_output=True, timeout=270 if len(answers) > 30 else 180, check=True)
    wall = time.monotonic() - start
    if len(result.stdout) > 2_000_000:
        raise ValueError("stdout exceeds 2 MB")
    lines = result.stdout.decode("utf-8").splitlines()
    for line in lines:
        record = json.loads(line)
        if any(not w for w in record["workers"]):
            raise ValueError("empty worker")
    if {p.stem for p in (data / "commits").glob("*.patch")} != set(answers):
        raise ValueError("labels do not match input directory")
    evaluation = evaluate_run(result.stdout, tests, answers)
    if not evaluation["ok"]:
        raise ValueError(evaluation)
    stderr = result.stderr.decode().splitlines()
    for msg in stderr:
        if not msg.startswith("ENTRY_CPU_US="):
            print(msg, file=sys.stderr)
    cpu, = (int(msg.split("=")[1]) for msg in stderr if msg.startswith("ENTRY_CPU_US="))
    simulated = evaluation["simulated_milliseconds"]
    return {"cpu_us": cpu, "simulated_ms": simulated, "total_us": simulated*1000+cpu,
            "misses": evaluation["missed_commits"], "wall_s": round(wall, 3),
            "lines": len(lines), "bytes": len(result.stdout)}, [json.loads(s) for s in lines]


def expanded(source, target, answers):
    inventory = json.loads((source / "tests.json").read_bytes())
    rows = inventory["tests"]
    rng = random.Random(7801)
    ids = rng.sample(range(100_000, 100_000 + 20 * len(rows)), len(rows))
    renumber = {}
    for r, replacement in zip(rows, ids):
        renumber[r["number"]] = replacement
        r["number"] = replacement
    rng.shuffle(rows)
    (target / "tests.json").write_text(json.dumps(inventory), encoding="utf-8")
    (target / "commits").mkdir()
    labels, originals = {}, {}
    changes = sorted((source / "commits").glob("*.patch"))
    for i in range(math.ceil(1.5*len(changes))):
        patch = changes[i % len(changes)]
        name = f"{rng.getrandbits(96):024x}"
        (target / "commits" / f"{name}.patch").write_bytes(patch.read_bytes())
        labels[name] = {"failing": [renumber[n] for n in answers[patch.stem]["failing"]]}
        originals[name] = patch.stem
    return labels, originals, {b:a for a,b in renumber.items()}


def selected(record):
    return {n for worker in record["workers"] for n in worker}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", default="blast-radius-entry")
    ap.add_argument("--data", default=ROOT / "sample", type=Path)
    ap.add_argument("--answers", type=Path)
    ap.add_argument("--repeats", default=3, type=int)
    ap.add_argument("--expand", action="store_true")
    ap.add_argument("--slack", action="store_true")
    args = ap.parse_args()
    data = args.data.resolve()
    answers = json.loads((args.answers or data / "answers.json").read_bytes())["commits"]
    config = json.loads(subprocess.check_output(["docker", "image", "inspect", args.image]))[0]["Config"]
    command = (config.get("Entrypoint") or []) + (config["Cmd"] or [])
    rows = []
    for _ in range(args.repeats):
        result, records = run(args.image, data, answers, command)
        rows.append(result["total_us"])
        print(json.dumps(result), flush=True)
    mid = sorted(rows)[len(rows)//2]
    print(f"middle total = {mid/1_000_000:.6f} s, rounded = {(mid+50000)//100000/10:.1f} s")
    if args.slack:
        for rec in records:
            expected = set(answers[rec["commit"]]["failing"])
            got = selected(rec)
            print(rec["commit"], "extra", len(got-expected), "missing", len(expected-got))
    if args.expand:
        base = {r["commit"]:selected(r) for r in records}
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder).resolve()
            labels, originals, restore = expanded(data, target, answers)
            stats, records = run(args.image, target, labels, command)
            for r in records:
                if {restore[n] for n in selected(r)} != base[originals[r["commit"]]]:
                    raise ValueError("selection changed after renumbering")
            print("expanded and renumbered:", json.dumps(stats))

            inventory_path = target / "tests.json"
            inventory = json.loads(inventory_path.read_bytes())
            # Stress the runtime contract with a new test and a different timing profile.
            # Its real failure status is unknown, so only check its inclusion here.
            unknown = max(t["number"] for t in inventory["tests"]) + 1
            for index, test in enumerate(inventory["tests"]):
                factor = 3 if index % 5 == 0 else 0.5
                test["seconds"] = round(test["seconds"] * factor, 3)
            inventory["tests"].append({"number": unknown, "id": "tests/validation.py::test_new",
                                       "file": "tests/validation.py", "seconds": 0.013})
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            changed, records = run(args.image, target, labels, command)
            if any(unknown not in selected(record) for record in records):
                raise ValueError("unprofiled inventory test was omitted")
            print("altered durations and additional test (synthetic):", json.dumps(changed))


if __name__ == "__main__":
    main()
