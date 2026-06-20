"""Orchestrator: run every check, analyse, and report.

Pipeline:  monitor_response -> compare_duplicates -> analyze -> report

Usage::

    python scripts/run_all.py            # full run: writes data + updates README
    python scripts/run_all.py --dry-run  # run everything, PRINT only, write nothing

``--dry-run`` is the safe local-testing path: it performs all live checks and
prints the full markdown report and a summary line without touching the
filesystem or git.

The process always exits 0 on a completed run (even when sites are down — that is
recorded as findings, not a crash). It exits non-zero only if the run itself
could not be performed (e.g. missing/invalid config).
"""

from __future__ import annotations

import argparse
import sys
from typing import Any, Dict

try:
    from . import analyze, compare_duplicates, gsc, monitor_response, report
    from .common import load_config, setup_logging, utc_now_compact, utc_now_iso
except ImportError:  # allow running as a plain script
    import analyze  # type: ignore
    import compare_duplicates  # type: ignore
    import gsc  # type: ignore
    import monitor_response  # type: ignore
    import report  # type: ignore
    from common import (  # type: ignore
        load_config,
        setup_logging,
        utc_now_compact,
        utc_now_iso,
    )


def run(dry_run: bool, config_path: str | None = None) -> Dict[str, Any]:
    """Execute the full pipeline.

    Args:
        dry_run: If True, print results but write/commit nothing.
        config_path: Optional path to an alternate config file.

    Returns:
        The assembled run dict (with summary + findings).
    """
    logger = setup_logging()
    config = load_config(config_path) if config_path else load_config()

    logger.info("=== SEO Monitor run starting (dry_run=%s) ===", dry_run)

    # Step 1 + 2: checks. Each is wrapped so one failing subsystem doesn't abort
    # the whole run.
    try:
        monitor_result = monitor_response.run(config, logger)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("monitor_response failed: %s", exc)
        monitor_result = {"timestamp_utc": utc_now_iso(), "checks": [], "findings": []}

    try:
        duplicates_result = compare_duplicates.run(config, logger)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("compare_duplicates failed: %s", exc)
        duplicates_result = {"timestamp_utc": utc_now_iso(), "pairs": [], "findings": []}

    # Google Search Console — the authoritative crawl signal (no-ops without
    # credentials). Wrapped so a GSC failure never aborts the run.
    try:
        gsc_result = gsc.run(config, logger)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("gsc failed: %s", exc)
        gsc_result = {"enabled": False, "timestamp_utc": utc_now_iso(), "sites": {}, "findings": []}

    current: Dict[str, Any] = {
        "timestamp_utc": utc_now_iso(),
        "run_id": utc_now_compact(),
        "monitor": monitor_result,
        "duplicates": duplicates_result,
        "gsc": gsc_result,
    }

    # Step 3: analyse against the previous run.
    previous = analyze.load_previous_run()
    current["findings"] = analyze.analyze(current, previous, config, logger)

    # Step 4: report.
    report_result = report.write_report(current, config, dry_run=dry_run, logger=logger)

    if dry_run:
        print("\n" + "=" * 70)
        print(report_result["markdown"])
        print("=" * 70 + "\n")

    _print_summary(current, dry_run)
    return current


def _print_summary(run_data: Dict[str, Any], dry_run: bool) -> None:
    """Print a single-line machine-friendly summary of the run."""
    summary = run_data.get("summary", {})
    sites = summary.get("sites", {})
    counts = summary.get("findings_counts", {})
    site_bits = " | ".join(
        f"{d}: {s.get('worst_status') or 'down'}" for d, s in sites.items()
    )
    mode = "DRY-RUN" if dry_run else "WROTE"
    print(
        f"SUMMARY [{mode}] {site_bits} || findings: "
        f"critical={counts.get('critical', 0)} "
        f"warning={counts.get('warning', 0)} "
        f"info={counts.get('info', 0)}"
    )


def main() -> int:
    """CLI entry point. Returns a process exit code."""
    parser = argparse.ArgumentParser(description="Run all SEO monitoring checks.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run all checks and print results without writing files or committing.",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to an alternate config.yaml (defaults to repo config.yaml).",
    )
    args = parser.parse_args()

    try:
        run(dry_run=args.dry_run, config_path=args.config)
    except (FileNotFoundError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
