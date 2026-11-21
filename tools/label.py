"""Validation only. Label independent patches from clean Git snapshots inside a locked image.

Run this host-side in Linux / WSL with Git and Docker. Reports live outside the submission.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import time


PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
TOOLS = Path(__file__).resolve().parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def label(case, patch, options, snapshot, environment, tests):
    reports = options.output / case
    reports.mkdir(exist_ok=True)
    final = reports / "result.json"
    signature = digest(environment + patch)
    if final.is_file():
        previous = json.loads(final.read_text())
        if previous["signature"] == signature:
            return previous
    with tempfile.TemporaryDirectory(dir=options.output, prefix="source-") as tmp:
        src = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
            archive.extractall(src, filter="data")
        if patch:
            subprocess.run(["git", "apply", "--whitespace=nowarn", "-"], input=patch,
                           cwd=src, check=True)
        events = reports / "events.json"
        events.unlink(missing_ok=True)
        args = [
            "docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
            "--cpus", str(options.cpus), "--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp",
            "--env", "SKIP_INTEGRATION=0", "--env", "PYTHONPATH=/src:/instrument",
            "--env", "OPENBLAS_NUM_THREADS=1", "--env", "OMP_NUM_THREADS=1",
            "--env", "PYTHON_CPU_COUNT=2", "--env", "PYTHONDONTWRITEBYTECODE=1",
            "--volume", f"{src}:/src:ro", "--workdir", "/src",
            "--volume", f"{reports}:/reports", "--volume", f"{TOOLS}:/instrument:ro",
            options.image, "python", "-m", "pytest", "tests", "-p", "label_plugin",
            "-p", "no:cacheprovider", "-q", "--tb=no", "--disable-warnings",
            "--continue-on-collection-errors", "--junitxml=/reports/junit.xml",
            "--timeout", str(options.timeout), "--max-worker-restart=0",
            "-n", str(options.workers), "--dist", "load",
        ]
        start = time.monotonic()
        with (reports / "pytest.log").open("wb") as log:
            run = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT)
        if run.returncode not in (0, 1, 5) or not events.is_file():
            raise RuntimeError(f"{case}: pytest exit {run.returncode}, see {reports}/pytest.log")
        report = json.loads(events.read_text())
        failed = set(report["failed"])
        observed = set(report["observed"])
        errors = set(report["collection_errors"])
        for row in tests:
            node = row["id"]
            file = row["file"]
            for error in errors:
                if node == error or node.startswith(error + "::") or file.startswith(error + "/"):
                    failed.add(node)
                    break
            if not (src / file).is_file():
                failed.add(node)
        missing = {r["id"] for r in tests} - observed - failed
        if missing:
            raise RuntimeError(f"{case}: {len(missing)} inventory tests unaccounted for, "
                               f"e.g. {min(missing)}. See {reports}/pytest.log")
        result = {"signature": signature,
                  "failing": sorted(r["number"] for r in tests if r["id"] in failed),
                  "collection_errors": sorted(errors),
                  "wall_seconds": round(time.monotonic() - start, 3)}
        write_json(final, result)
        print(f"{case} {len(result['failing'])} failed, {result['wall_seconds']}s", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Git source repository with the pinned commit")
    parser.add_argument("data", type=Path, help="Directory containing tests.json and commits/")
    parser.add_argument("output", type=Path, help="Private validation output directory")
    parser.add_argument("--image", default="blast-radius-lab-locked")
    parser.add_argument("--workers", type=int, default=4, help="pytest-xdist workers per patch")
    parser.add_argument("--jobs", type=int, default=1, help="concurrent full-suite runs")
    parser.add_argument("--timeout", type=int, default=180, help="per-test timeout in seconds")
    parser.add_argument("--expected", type=Path, help="compare completed results to sample answers")
    parser.add_argument("--baselines-only", action="store_true")
    parser.add_argument("--cpus", type=float, default=2, help="Docker CPU limit per validation container")
    options = parser.parse_args()
    options.output = options.output.resolve()
    options.output.mkdir(parents=True, exist_ok=True)
    snapshot = subprocess.check_output(["git", "-C", options.source, "archive", PIN])
    inventory = (options.data / "tests.json").read_bytes()
    tests = json.loads(inventory)["tests"]
    image_id = subprocess.check_output(["docker", "image", "inspect", "--format", "{{.Id}}", options.image])
    environment = b"\x00".join([PIN.encode(), image_id, inventory, Path(__file__).read_bytes(),
                                (TOOLS / "label_plugin.py").read_bytes(),
                                str((options.workers, options.timeout, options.cpus)).encode()])
    for case in ("baseline-1", "baseline-2"):
        label(case, b"", options, snapshot, environment, tests)
    if options.baselines_only:
        return
    patches = sorted((options.data / "commits").glob("*.patch"))

    def run(patch):
        result = label(patch.stem, patch.read_bytes(), options, snapshot, environment, tests)
        return patch.stem, {"failing": result["failing"]}

    with ThreadPoolExecutor(max_workers=options.jobs) as pool:
        answers = dict(pool.map(run, patches))
    write_json(options.output / "answers.json", {"commits": answers})
    if options.expected:
        expected = json.loads(options.expected.read_text())["commits"]
        delta = {key: (expected.get(key), answers.get(key)) for key in expected.keys() | answers.keys()
                 if expected.get(key, {}).get("failing") != answers.get(key, {}).get("failing")}
        write_json(options.output / "differences.json", delta)
        print(f"{len(delta)} label disagreements across {len(answers)} patches", flush=True)
        if delta:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
