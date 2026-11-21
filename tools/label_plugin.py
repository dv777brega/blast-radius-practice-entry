"""Validation-only pytest plugin. Count each failed subtest against its parent node ID."""

import json
from pathlib import Path


failed = set()
observed = set()
collection_errors = set()


def pytest_runtest_logreport(report):
    if report.failed:
        failed.add(report.nodeid)
    if report.when == "call" or report.failed or report.skipped:
        observed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        collection_errors.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if hasattr(session.config, "workerinput"):
        return
    Path("/reports/events.json").write_text(json.dumps({
        "failed": sorted(failed),
        "observed": sorted(observed),
        "collection_errors": sorted(collection_errors),
        "exit": int(exitstatus),
    }))
