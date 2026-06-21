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
    from .common import (
        DATA_DIR,
        dynamic_path_set,
        load_config,
        setup_logging,
        utc_now_iso,
    )
    from .fixes import attach_fixes
except ImportError:  # allow running as a plain script
    from common import (  # type: ignore
        DATA_DIR,
        dynamic_path_set,
        load_config,
        setup_logging,
        utc_now_iso,
    )
    from fixes import attach_fixes  # type: ignore


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
    findings.extend(current.get("gsc", {}).get("findings", []))
    findings.extend(current.get("onpage", {}).get("findings", []))

    cur_checks = current.get("monitor", {}).get("checks", [])
    prev_idx = _index_checks(previous)
    multiplier = float(config["thresholds"].get("latency_spike_multiplier", 2.0))
    has_previous = previous is not None

    # (site, path) pairs where the whole runner IP was blocked (every UA refused).
    # For these we DON'T emit per-request critical 403/FETCH_ERROR findings —
    # monitor_response already raised a single RUNNER_IP_BLOCKED warning, and the
    # criticals would just be a false-alarm flood reflecting our runner's IP.
    runner_blocked = {
        (f.get("site"), f.get("path"))
        for f in current.get("monitor", {}).get("findings", [])
        if f.get("type") == "RUNNER_IP_BLOCKED"
    }

    for cur in cur_checks:
        site, path, ua = cur["site"], cur["path"], cur["user_agent_label"]
        key = (site, path, ua)
        prev = prev_idx.get(key)
        status = cur.get("status_code")
        label = f"{path} [{ua}]"
        is_runner_blocked = (site, path) in runner_blocked

        # 2. New 403s and 5xx server errors (absolute, threshold-gated for 403).
        if status == 403 and not is_runner_blocked:
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
        # Suppressed when the whole runner IP is blocked (covered by the single
        # RUNNER_IP_BLOCKED warning) so a blocked runner can't flood criticals.
        if cur.get("error") is not None and not is_runner_blocked:
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

    # 5. CONTENT_CHANGE — collapsed to at most one finding per page per run.
    #    Dynamic pages (live homepages) are exempt; other pages compare the
    #    NORMALIZED hash (scripts/tokens/timestamps stripped) so only a real
    #    template/content edit fires.
    if has_previous:
        findings.extend(
            _content_change_findings(cur_checks, prev_idx, config, now)
        )

    # 6. GSC delta findings (need previous-run context, which lives here):
    #    DEINDEXED, POSITION_DROP, IMPRESSIONS_DROP.
    findings.extend(
        _gsc_delta_findings(
            current.get("gsc", {}),
            (previous or {}).get("gsc", {}),
            config,
            now,
        )
    )

    order = {"critical": 0, "warning": 1, "info": 2}
    findings.sort(key=lambda f: order.get(f.get("severity"), 3))

    # Attach a recommended fix to every finding (flows into JSON/report/dashboard).
    attach_fixes(findings)

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


def _content_change_findings(
    cur_checks: List[Dict[str, Any]],
    prev_idx: Dict[tuple, Dict[str, Any]],
    config: Dict[str, Any],
    now: str,
) -> List[Dict[str, Any]]:
    """Emit at most one CONTENT_CHANGE (info) per page per run.

    Dynamic pages (config ``dynamic: true``) are skipped entirely. Other pages
    compare the *normalized* hash (scripts/tokens/timestamps stripped) across both
    runs; if it differs for one or more user-agents, a single finding is emitted
    naming the affected UAs.

    Args:
        cur_checks: Current run's monitor check records.
        prev_idx: Previous run's checks indexed by (site, path, user-agent).
        config: The loaded configuration.
        now: Timestamp for findings.

    Returns:
        A list of CONTENT_CHANGE finding dicts (one per changed page).
    """
    dynamic = dynamic_path_set(config)
    ua_labels = list(config.get("user_agents", {}).keys())
    cur_idx = {(c["site"], c["path"], c["user_agent_label"]): c for c in cur_checks}
    site_paths = sorted({(c["site"], c["path"]) for c in cur_checks})

    findings: List[Dict[str, Any]] = []
    for site, path in site_paths:
        if (site, path) in dynamic:
            continue  # dynamic page — body churn is expected, never a finding

        changed_uas: List[str] = []
        for ua in ua_labels:
            cur = cur_idx.get((site, path, ua))
            prev = prev_idx.get((site, path, ua))
            if not cur or not prev:
                continue
            if cur.get("status_code") != 200 or prev.get("status_code") != 200:
                continue
            cur_h = cur.get("normalized_sha256")
            prev_h = prev.get("normalized_sha256")
            # Only compare when both runs have a normalized hash (skips the
            # one-off transition from older runs that predate this field).
            if cur_h and prev_h and cur_h != prev_h:
                changed_uas.append(ua)

        if changed_uas:
            findings.append(
                {
                    "type": "CONTENT_CHANGE",
                    "severity": "info",
                    "site": site,
                    "path": path,
                    "message": (
                        f"Normalized page content changed on {path} "
                        f"(seen by: {', '.join(changed_uas)})."
                    ),
                    "details": {"changed_user_agents": changed_uas},
                    "timestamp": now,
                }
            )
    return findings


def _gsc_delta_findings(
    cur_gsc: Dict[str, Any],
    prev_gsc: Dict[str, Any],
    config: Dict[str, Any],
    now: str,
) -> List[Dict[str, Any]]:
    """Compute GSC findings that need the previous run for comparison.

    Produces:
      * DEINDEXED (critical) — a key URL that was indexed last run is no longer
        indexed; or, on first sight (no previous GSC snapshot), a key URL that is
        currently not indexed.
      * POSITION_DROP (warning) — overall site avg position, or a tracked
        keyword's position, worsened by more than ``gsc.position_drop_threshold``.
      * IMPRESSIONS_DROP (warning) — window impressions fell by more than
        ``gsc.impressions_drop_pct`` percent.

    Args:
        cur_gsc: ``current["gsc"]``.
        prev_gsc: ``previous["gsc"]`` (may be empty/None).
        config: The loaded configuration.
        now: Timestamp for the findings.

    Returns:
        A list of finding dicts (empty when GSC is disabled this run).
    """
    findings: List[Dict[str, Any]] = []
    if not cur_gsc or not cur_gsc.get("enabled"):
        return findings

    gsc_cfg = config.get("gsc", {}) or {}
    pos_threshold = float(gsc_cfg.get("position_drop_threshold", 5))
    impr_drop_pct = float(gsc_cfg.get("impressions_drop_pct", 40))
    keywords = [k.lower() for k in config.get("keywords", [])]

    cur_sites = cur_gsc.get("sites", {})
    prev_enabled = bool(prev_gsc) and prev_gsc.get("enabled")
    prev_sites = prev_gsc.get("sites", {}) if prev_enabled else {}

    for domain, cur_site in cur_sites.items():
        prev_site = prev_sites.get(domain, {})

        # --- DEINDEXED (per key URL) ---
        prev_idx = {r.get("url"): r for r in prev_site.get("url_inspection", [])}
        for rec in cur_site.get("url_inspection", []):
            if rec.get("error") or rec.get("indexed") is None:
                continue
            if rec.get("indexed"):
                continue  # currently indexed — fine
            url = rec.get("url")

            # Intentional noindex (e.g. /dmca) is correct behaviour, not a drop —
            # report it as info, never as a critical DEINDEXED regression.
            if rec.get("noindex"):
                findings.append(
                    {
                        "type": "NOINDEX_PAGE",
                        "severity": "info",
                        "site": domain,
                        "path": url,
                        "message": (
                            f"{url} is intentionally excluded from Google's index "
                            f"(indexingState={rec.get('indexingState')}, "
                            f"coverageState={rec.get('coverageState')}). Expected."
                        ),
                        "details": {
                            "indexingState": rec.get("indexingState"),
                            "coverageState": rec.get("coverageState"),
                        },
                        "timestamp": now,
                    }
                )
                continue

            prev_rec = prev_idx.get(url)
            was_indexed = bool(prev_rec) and prev_rec.get("indexed")
            first_sight = not prev_enabled or prev_rec is None
            if was_indexed or first_sight:
                reason = "was indexed last run" if was_indexed else "not indexed (first GSC snapshot)"
                findings.append(
                    {
                        "type": "DEINDEXED",
                        "severity": "critical",
                        "site": domain,
                        "path": url,
                        "message": (
                            f"{url} should be indexed but isn't ({reason}). "
                            f"verdict={rec.get('verdict')}, "
                            f"coverageState={rec.get('coverageState')}."
                        ),
                        "details": {
                            "verdict": rec.get("verdict"),
                            "coverageState": rec.get("coverageState"),
                            "was_indexed": was_indexed,
                        },
                        "timestamp": now,
                    }
                )

        # The remaining deltas need a previous GSC snapshot for this site.
        if not prev_enabled or not prev_site:
            continue

        cur_sa = cur_site.get("search_analytics") or {}
        prev_sa = prev_site.get("search_analytics") or {}
        cur_tot = cur_sa.get("totals") or {}
        prev_tot = prev_sa.get("totals") or {}

        # --- POSITION_DROP (overall) ---
        cur_pos, prev_pos = cur_tot.get("position"), prev_tot.get("position")
        if isinstance(cur_pos, (int, float)) and isinstance(prev_pos, (int, float)):
            if cur_pos - prev_pos > pos_threshold:  # higher position = worse
                findings.append(
                    {
                        "type": "POSITION_DROP",
                        "severity": "warning",
                        "site": domain,
                        "message": (
                            f"Avg position worsened {prev_pos:.1f} -> {cur_pos:.1f} "
                            f"(> {pos_threshold:g}) for {domain} over the GSC window."
                        ),
                        "details": {"previous": prev_pos, "current": cur_pos},
                        "timestamp": now,
                    }
                )

        # --- POSITION_DROP (per tracked keyword) ---
        if keywords:
            cur_q = {r.get("query", "").lower(): r for r in cur_sa.get("queries", [])}
            prev_q = {r.get("query", "").lower(): r for r in prev_sa.get("queries", [])}
            for kw in keywords:
                cq, pq = cur_q.get(kw), prev_q.get(kw)
                if not cq or not pq:
                    continue
                cp, pp = cq.get("position"), pq.get("position")
                if isinstance(cp, (int, float)) and isinstance(pp, (int, float)):
                    if cp - pp > pos_threshold:
                        findings.append(
                            {
                                "type": "POSITION_DROP",
                                "severity": "warning",
                                "site": domain,
                                "message": (
                                    f"Keyword '{kw}' position worsened {pp:.1f} -> "
                                    f"{cp:.1f} (> {pos_threshold:g}) for {domain}."
                                ),
                                "details": {"keyword": kw, "previous": pp, "current": cp},
                                "timestamp": now,
                            }
                        )

        # --- IMPRESSIONS_DROP (overall) ---
        cur_impr, prev_impr = cur_tot.get("impressions"), prev_tot.get("impressions")
        if isinstance(prev_impr, (int, float)) and prev_impr > 0 and isinstance(cur_impr, (int, float)):
            drop_pct = (prev_impr - cur_impr) / prev_impr * 100
            if drop_pct > impr_drop_pct:
                findings.append(
                    {
                        "type": "IMPRESSIONS_DROP",
                        "severity": "warning",
                        "site": domain,
                        "message": (
                            f"Impressions fell {drop_pct:.0f}% ({prev_impr:.0f} -> "
                            f"{cur_impr:.0f}, > {impr_drop_pct:g}%) for {domain}."
                        ),
                        "details": {
                            "previous": prev_impr,
                            "current": cur_impr,
                            "drop_pct": round(drop_pct, 1),
                        },
                        "timestamp": now,
                    }
                )

    return findings


def main() -> None:
    """CLI entry point: requires a current run on stdin or runs checks fresh."""
    logger = setup_logging()
    config = load_config()

    # Build a fresh current run by importing the check modules, so analyze.py can
    # be exercised standalone against the live sites + latest saved run.
    try:
        from . import compare_duplicates, gsc, monitor_response
    except ImportError:
        import compare_duplicates  # type: ignore
        import gsc  # type: ignore
        import monitor_response  # type: ignore

    current = {
        "monitor": monitor_response.run(config, logger),
        "duplicates": compare_duplicates.run(config, logger),
        "gsc": gsc.run(config, logger),
    }
    previous = load_previous_run()
    findings = analyze(current, previous, config, logger)
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
