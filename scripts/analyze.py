"""Diff the current run against the previous run and produce findings.

This is the diagnosis layer. It aggregates the findings already raised by
monitor_response (CRAWLER_BLOCKED, MOBILE_FETCH_FAIL) and compare_duplicates
(DUPLICATE_CONTENT), then adds delta-based findings by comparing the current run
to the most recent previous run loaded from the data directory:

* new 403s (and 5xx server errors)
* any status-code change since the last run
* large latency spikes (> configured multiplier x previous response time)
* content-hash (SHA-256) changes on key pages

Every finding has: ``severity`` (critical/warning/info), ``type``, ``site``,
``message`` and ``timestamp``.

Run directly to analyse against the latest saved run in data/:

    python scripts/analyze.py
"""

from __future__ import annotations

import glob
import json
import os
from typing import Any, Dict, List, Optional

try:
    from .common import DATA_DIR, load_config, setup_logging, utc_now_iso
except ImportError:  # allow running as a plain script
    from common import DATA_DIR, load_config, setup_logging, utc_now_iso  # type: ignore


def load_previous_run(
    data_dir: str = DATA_DIR, exclude_path: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Load the most recent saved run JSON from the data directory.

    Run files are named ``run-<stamp>.json``. The newest by filename (the stamp
    sorts chronologically) is returned, skipping ``exclude_path`` if given.

    Args:
        data_dir: Directory holding run JSON files (the data branch checkout).
        exclude_path: A path to skip (e.g. the current run being written).

    Returns:
        The parsed previous run dict, or None if there is no prior run.
    """
    pattern = os.path.join(data_dir, "run-*.json")
    files = sorted(glob.glob(pattern), reverse=True)
    for path in files:
        if exclude_path and os.path.abspath(path) == os.path.abspath(exclude_path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
    return None


def _index_checks(run: Optional[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """Index a run's monitor checks by (site, path, user_agent_label)."""
    if not run:
        return {}
    checks = run.get("monitor", {}).get("checks", [])
    return {(c["site"], c["path"], c["user_agent_label"]): c for c in checks}


def analyze(
    current: Dict[str, Any],
    previous: Optional[Dict[str, Any]],
    config: Dict[str, Any],
    logger=None,
) -> List[Dict[str, Any]]:
    """Produce the full findings list for the current run.

    Args:
        current: The current run dict with ``monitor`` and ``duplicates`` keys.
        previous: The previous run dict, or None on the first run.
        config: The loaded configuration.
        logger: Optional logger.

    Returns:
        A list of finding dicts, ordered critical-first.
    """
    log = logger or setup_logging()
    now = utc_now_iso()
    findings: List[Dict[str, Any]] = []

    # 1. Carry forward findings already raised by the check modules.
    findings.extend(current.get("monitor", {}).get("findings", []))
    findings.extend(current.get("duplicates", {}).get("findings", []))

    cur_checks = current.get("monitor", {}).get("checks", [])
    prev_idx = _index_checks(previous)
    multiplier = float(config["thresholds"].get("latency_spike_multiplier", 2.0))
    has_previous = previous is not None

    for cur in cur_checks:
        site, path, ua = cur["site"], cur["path"], cur["user_agent_label"]
        key = (site, path, ua)
        prev = prev_idx.get(key)
        status = cur.get("status_code")
        label = f"{path} [{ua}]"

        # 2. New 403s and 5xx server errors (absolute, threshold-gated for 403).
        if status == 403:
            was_403 = bool(prev) and prev.get("status_code") == 403
            findings.append(
                {
                    "type": "FORBIDDEN_403",
                    "severity": "critical",
                    "site": site,
                    "path": path,
                    "message": (
                        f"403 Forbidden for {ua} on {label}"
                        + ("" if was_403 else " (new since last run)")
                        + "."
                    ),
                    "details": {"user_agent": ua, "new": not was_403},
                    "timestamp": now,
                }
            )
        elif status is not None and 500 <= status <= 599:
            findings.append(
                {
                    "type": "SERVER_ERROR_5XX",
                    "severity": "critical",
                    "site": site,
                    "path": path,
                    "message": f"{status} server error for {ua} on {label}.",
                    "details": {"user_agent": ua, "status_code": status},
                    "timestamp": now,
                }
            )

        # Connection-level failure (DNS/timeout/SSL) — site effectively down.
        if cur.get("error") is not None:
            findings.append(
                {
                    "type": "FETCH_ERROR",
                    "severity": "critical",
                    "site": site,
                    "path": path,
                    "message": f"Request failed for {ua} on {label}: {cur['error']}",
                    "details": {"user_agent": ua, "error": cur["error"]},
                    "timestamp": now,
                }
            )

        if not has_previous or prev is None:
            continue  # nothing to diff against for this key

        prev_status = prev.get("status_code")

        # 3. Status-code change since last run.
        if status != prev_status:
            findings.append(
                {
                    "type": "STATUS_CHANGE",
                    "severity": "warning",
                    "site": site,
                    "path": path,
                    "message": (
                        f"Status changed {prev_status} -> {status} for {ua} on {label}."
                    ),
                    "details": {
                        "user_agent": ua,
                        "previous": prev_status,
                        "current": status,
                    },
                    "timestamp": now,
                }
            )

        # 4. Latency spike (> multiplier x previous), only when both succeeded.
        cur_rt, prev_rt = cur.get("response_time_ms"), prev.get("response_time_ms")
        if (
            isinstance(cur_rt, (int, float))
            and isinstance(prev_rt, (int, float))
            and prev_rt > 0
            and cur_rt > multiplier * prev_rt
        ):
            findings.append(
                {
                    "type": "LATENCY_SPIKE",
                    "severity": "warning",
                    "site": site,
                    "path": path,
                    "message": (
                        f"Latency {prev_rt:.0f}ms -> {cur_rt:.0f}ms "
                        f"(>{multiplier:g}x) for {ua} on {label}."
                    ),
                    "details": {
                        "user_agent": ua,
                        "previous_ms": prev_rt,
                        "current_ms": cur_rt,
                    },
                    "timestamp": now,
                }
            )

        # 5. Content-hash change on a key page (only meaningful when both 200).
        cur_hash, prev_hash = cur.get("body_sha256"), prev.get("body_sha256")
        if (
            cur_hash
            and prev_hash
            and cur_hash != prev_hash
            and status == 200
            and prev_status == 200
        ):
            findings.append(
                {
                    "type": "CONTENT_CHANGE",
                    "severity": "info",
                    "site": site,
                    "path": path,
                    "message": f"Page body changed (SHA-256 differs) for {ua} on {label}.",
                    "details": {
                        "user_agent": ua,
                        "previous_sha256": prev_hash,
                        "current_sha256": cur_hash,
                    },
                    "timestamp": now,
                }
            )

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.get("severity"), 3))

    counts = {sev: sum(1 for f in findings if f["severity"] == sev) for sev in order}
    log.info(
        "analyze: %d findings (critical=%d warning=%d info=%d; previous run=%s)",
        len(findings),
        counts["critical"],
        counts["warning"],
        counts["info"],
        "yes" if has_previous else "none (first run)",
    )
    return findings


def main() -> None:
    """CLI entry point: requires a current run on stdin or runs checks fresh."""
    logger = setup_logging()
    config = load_config()

    # Build a fresh current run by importing the check modules, so analyze.py can
    # be exercised standalone against the live sites + latest saved run.
    try:
        from . import compare_duplicates, monitor_response
    except ImportError:
        import compare_duplicates  # type: ignore
        import monitor_response  # type: ignore

    current = {
        "monitor": monitor_response.run(config, logger),
        "duplicates": compare_duplicates.run(config, logger),
    }
    previous = load_previous_run()
    findings = analyze(current, previous, config, logger)
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
