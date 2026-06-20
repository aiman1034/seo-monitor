"""Write run outputs and update the README status block.

Produces, per run:

1. ``data/run-<stamp>.json`` — the full run data (monitor + duplicates +
   findings + summary). This is the time-series record committed to the
   ``data`` branch.
2. ``data/report-<stamp>.md`` — a human-readable markdown report.
3. ``data/latest-summary.json`` — a small, stable file the GitHub Actions
   workflow reads to build the commit message and decide on alert Issues.

It also rewrites the ``<!-- STATUS:BEGIN -->...<!-- STATUS:END -->`` block in
README.md with the current per-site status and shields.io badges.

With ``dry_run=True`` nothing is written — the markdown report is returned (and
printed by run_all) so you can preview a run locally.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

try:
    from .common import (
        REPO_ROOT,
        ensure_data_dir,
        load_config,
        setup_logging,
        utc_now_compact,
        utc_now_iso,
    )
except ImportError:  # allow running as a plain script
    from common import (  # type: ignore
        REPO_ROOT,
        ensure_data_dir,
        load_config,
        setup_logging,
        utc_now_compact,
        utc_now_iso,
    )

README_PATH = os.path.join(REPO_ROOT, "README.md")
_STATUS_BEGIN = "<!-- STATUS:BEGIN -->"
_STATUS_END = "<!-- STATUS:END -->"

# Static dashboard template (shipped on main, published to the data branch each run).
DASHBOARD_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard_template.html")
RUNS_INDEX_CAP = 240  # keep the time-series file bounded


def summarize(run_data: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    """Build a per-site summary from the run's monitor checks.

    For each site this records the homepage status per user-agent, the worst
    status seen across all that site's checks, and an OK/ISSUE verdict.

    Args:
        run_data: The assembled run dict (with ``monitor`` and ``findings``).
        config: The loaded configuration.

    Returns:
        A summary dict keyed by domain, plus ``findings_counts`` and ``run_id``.
    """
    checks: List[Dict[str, Any]] = run_data.get("monitor", {}).get("checks", [])
    gsc = run_data.get("gsc", {}) or {}
    gsc_sites = gsc.get("sites", {}) if gsc.get("enabled") else {}
    sites: Dict[str, Any] = {}

    for site_cfg in config.get("sites", []):
        domain = site_cfg["domain"]
        site_checks = [c for c in checks if c["site"] == domain]
        home = {
            c["user_agent_label"]: c.get("status_code")
            for c in site_checks
            if c["path"] == "/"
        }
        # "Worst" = highest non-None status code, or a sentinel if any error.
        codes = [c.get("status_code") for c in site_checks if c.get("status_code")]
        any_error = any(c.get("error") for c in site_checks)
        worst = max(codes) if codes else None
        ok = (not any_error) and bool(codes) and all(c == 200 for c in codes)
        entry = {
            "homepage_status": home,
            "worst_status": worst,
            "any_error": any_error,
            "verdict": "OK" if ok else "ISSUE",
        }
        # Attach a compact GSC summary when available (authoritative signal).
        gsite = gsc_sites.get(domain)
        if gsite:
            totals = (gsite.get("search_analytics") or {}).get("totals") or {}
            entry["gsc"] = {
                "clicks": totals.get("clicks"),
                "impressions": totals.get("impressions"),
                "position": totals.get("position"),
                "ctr": totals.get("ctr"),
            }
        sites[domain] = entry

    findings = run_data.get("findings", [])
    counts = {
        sev: sum(1 for f in findings if f.get("severity") == sev)
        for sev in ("critical", "warning", "info")
    }
    return {
        "sites": sites,
        "findings_counts": counts,
        "gsc_enabled": bool(gsc.get("enabled")),
        "run_id": run_data.get("run_id"),
        "timestamp_utc": run_data.get("timestamp_utc"),
    }


def _badge(label: str, message: str, color: str) -> str:
    """Build a shields.io static badge URL (URL-encoding label/message)."""

    def enc(s: str) -> str:
        return (
            s.replace("-", "--")
            .replace("_", "__")
            .replace(" ", "_")
        )

    return f"https://img.shields.io/badge/{enc(label)}-{enc(message)}-{color}"


def _status_block(summary: Dict[str, Any]) -> str:
    """Render the README status block markdown (badges + per-site table)."""
    ts = summary.get("timestamp_utc") or utc_now_iso()
    counts = summary.get("findings_counts", {})

    badges = [f"![last run]({_badge('last run', ts, 'blue')})"]
    for domain, s in summary.get("sites", {}).items():
        ok = s["verdict"] == "OK"
        color = "brightgreen" if ok else "red"
        msg = "200" if ok else (str(s["worst_status"]) if s["worst_status"] else "down")
        badges.append(f"![{domain}]({_badge(domain, msg, color)})")

    lines = [_STATUS_BEGIN, "", "### Current status", "", " ".join(badges), ""]
    lines.append(f"_Last run: **{ts}** · critical: {counts.get('critical', 0)} · "
                 f"warning: {counts.get('warning', 0)} · info: {counts.get('info', 0)}_")
    lines.append("")
    gsc_on = summary.get("gsc_enabled")
    gsc_col = " GSC (clicks / impr / pos) |" if gsc_on else ""
    gsc_sep = " --- |" if gsc_on else ""
    lines.append(f"| Site | Verdict | Homepage status (by user-agent) |{gsc_col}")
    lines.append(f"| --- | --- | --- |{gsc_sep}")
    for domain, s in summary.get("sites", {}).items():
        home = s.get("homepage_status", {})
        home_str = ", ".join(f"{ua}: {st}" for ua, st in home.items()) or "—"
        gsc_cell = ""
        if gsc_on:
            g = s.get("gsc") or {}
            gsc_cell = (
                f" {g.get('clicks', '—')} / {g.get('impressions', '—')} / "
                f"{g.get('position', '—')} |"
            )
        lines.append(f"| `{domain}` | {s['verdict']} | {home_str} |{gsc_cell}")
    if not gsc_on:
        lines.append("")
        lines.append("_Search Console not configured — HTTP status above reflects "
                     "this runner's IP, which these sites may 403. See README to "
                     "enable GSC for Google's authoritative view._")
    lines.append("")
    lines.append(_STATUS_END)
    return "\n".join(lines)


def update_readme(summary: Dict[str, Any], dry_run: bool, logger=None) -> str:
    """Replace the status block in README.md with the current summary.

    Args:
        summary: The summary dict from :func:`summarize`.
        dry_run: If True, do not write — just return the rendered block.
        logger: Optional logger.

    Returns:
        The rendered status block markdown.
    """
    log = logger or setup_logging()
    block = _status_block(summary)
    if dry_run:
        return block

    if not os.path.exists(README_PATH):
        log.warning("README.md not found at %s; skipping status update", README_PATH)
        return block

    with open(README_PATH, "r", encoding="utf-8") as fh:
        content = fh.read()

    pattern = re.compile(
        re.escape(_STATUS_BEGIN) + r".*?" + re.escape(_STATUS_END), re.DOTALL
    )
    if pattern.search(content):
        content = pattern.sub(block, content)
    else:
        log.warning("STATUS markers not found in README; appending block")
        content = content.rstrip() + "\n\n" + block + "\n"

    with open(README_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    log.info("Updated README status block")
    return block


def render_markdown(run_data: Dict[str, Any], summary: Dict[str, Any]) -> str:
    """Render the full human-readable markdown report for a run."""
    ts = run_data.get("timestamp_utc")
    lines: List[str] = [f"# SEO Monitor report — {ts}", ""]

    # Findings.
    findings = run_data.get("findings", [])
    lines.append(f"## Findings ({len(findings)})")
    lines.append("")
    if findings:
        lines.append("| Severity | Type | Site | Path | Message | Fix |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for f in findings:
            lines.append(
                f"| {f.get('severity')} | {f.get('type')} | {f.get('site')} | "
                f"{f.get('path', '')} | {f.get('message')} | {f.get('fix', '')} |"
            )
    else:
        lines.append("_No findings this run._")
    lines.append("")

    # HTTP checks.
    lines.append("## HTTP checks")
    lines.append("")
    lines.append("| Site | Path | User-agent | Status | Time (ms) | TTFB (ms) | Error |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for c in run_data.get("monitor", {}).get("checks", []):
        lines.append(
            f"| {c['site']} | {c['path']} | {c['user_agent_label']} | "
            f"{c.get('status_code')} | {c.get('response_time_ms')} | "
            f"{c.get('ttfb_ms')} | {c.get('error') or ''} |"
        )
    lines.append("")

    # Duplicate content.
    lines.append("## Duplicate-content comparison")
    lines.append("")
    for pair in run_data.get("duplicates", {}).get("pairs", []):
        overall = pair.get("overall_similarity")
        overall_str = f"{overall:.1%}" if overall is not None else "n/a"
        lines.append(
            f"**{pair['domain_a']} vs {pair['domain_b']}** — overall: "
            f"{overall_str} ({pair.get('pages_compared', 0)} page(s))"
        )
        lines.append("")
        lines.append("| Path | Sequence | Jaccard | Combined |")
        lines.append("| --- | --- | --- | --- |")
        for p in pair.get("pages", []):
            def pct(v: Optional[float]) -> str:
                return f"{v:.1%}" if isinstance(v, (int, float)) else "—"

            lines.append(
                f"| {p['path']} | {pct(p.get('sequence_ratio'))} | "
                f"{pct(p.get('jaccard'))} | {pct(p.get('combined'))} |"
            )
        lines.append("")

    # Search Console (Phase 2) — Google's own, authoritative view.
    lines.append("## Search Console (Google's view)")
    lines.append("")
    gsc = run_data.get("gsc", {}) or {}
    if not gsc.get("enabled"):
        reason = gsc.get("reason", "not configured")
        lines.append(f"_GSC not enabled this run ({reason})._")
        lines.append("")
    else:
        for domain, site in gsc.get("sites", {}).items():
            sa = site.get("search_analytics") or {}
            totals = sa.get("totals") or {}
            window = sa.get("window") or {}
            lines.append(
                f"**{domain}** (`{site.get('property')}`) — window "
                f"{window.get('start', '?')}→{window.get('end', '?')}: "
                f"clicks {totals.get('clicks', '—')}, "
                f"impressions {totals.get('impressions', '—')}, "
                f"avg position {totals.get('position', '—')}"
            )
            if site.get("error"):
                lines.append(f"  _error: {site['error']}_")
            lines.append("")
            lines.append("| Key URL | Verdict | pageFetchState | coverageState | lastCrawlTime |")
            lines.append("| --- | --- | --- | --- | --- |")
            for rec in site.get("url_inspection", []):
                if rec.get("error"):
                    lines.append(f"| {rec.get('url')} | error | {rec['error']} | | |")
                    continue
                lines.append(
                    f"| {rec.get('url')} | {rec.get('verdict') or '—'} | "
                    f"{rec.get('pageFetchState') or '—'} | "
                    f"{rec.get('coverageState') or '—'} | "
                    f"{rec.get('lastCrawlTime') or '—'} |"
                )
            lines.append("")

    return "\n".join(lines)


def write_report(
    run_data: Dict[str, Any], config: Dict[str, Any], dry_run: bool, logger=None
) -> Dict[str, Any]:
    """Write all run outputs and update the README.

    Args:
        run_data: Assembled run dict (monitor + duplicates + findings + run_id).
        config: The loaded configuration.
        dry_run: If True, write nothing; just compute + return the artifacts.
        logger: Optional logger.

    Returns:
        A dict with ``summary``, ``markdown``, and (when not dry-run) the written
        ``json_path`` / ``md_path``.
    """
    log = logger or setup_logging()
    summary = summarize(run_data, config)
    run_data["summary"] = summary
    markdown = render_markdown(run_data, summary)
    result: Dict[str, Any] = {"summary": summary, "markdown": markdown}

    if dry_run:
        log.info("[dry-run] Skipping file writes and README update")
        update_readme(summary, dry_run=True, logger=log)  # no-op, returns block
        return result

    data_dir = ensure_data_dir()
    # Overall summary (deltas vs the previous run, read from the not-yet-appended
    # runs-index.json). Stored on run_data so it lands in the run JSON too.
    overall = _compute_overall(run_data, summary, data_dir)
    run_data["overall"] = overall

    stamp = run_data.get("run_id") or utc_now_compact()
    json_path = os.path.join(data_dir, f"run-{stamp}.json")
    md_path = os.path.join(data_dir, f"report-{stamp}.md")
    latest_path = os.path.join(data_dir, "latest-summary.json")

    with open(json_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(run_data, fh, indent=2)
    with open(md_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(markdown)
    with open(latest_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(
            {
                "summary": summary,
                "overall": overall,
                "json_file": os.path.basename(json_path),
                "md_file": os.path.basename(md_path),
                # Full findings (each with its 'fix') so the dashboard can render
                # the findings table from this single file.
                "findings": run_data.get("findings", []),
                "critical_findings": [
                    f for f in run_data.get("findings", []) if f["severity"] == "critical"
                ],
            },
            fh,
            indent=2,
        )

    # Standalone live dashboard committed alongside the results (the data branch
    # has no docs README, so this is its published status page).
    status_path = os.path.join(data_dir, "STATUS.md")
    with open(status_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Live status\n\n" + _status_block(summary) + "\n")

    # Time-series index + the GitHub Pages dashboard (served from the data branch).
    _update_runs_index(run_data, summary, data_dir, logger=log)
    _write_dashboard(data_dir, logger=log)

    update_readme(summary, dry_run=False, logger=log)
    log.info("Wrote %s and %s", os.path.basename(json_path), os.path.basename(md_path))

    result.update(json_path=json_path, md_path=md_path, latest_path=latest_path)
    return result


def _compute_overall(
    run_data: Dict[str, Any], summary: Dict[str, Any], data_dir: str
) -> Dict[str, Any]:
    """Build the data-driven overall block (per-site deltas + combined headline).

    Deltas are computed against the most recent record in ``runs-index.json``
    (read before this run appends to it). Position delta is raw current-minus-
    previous; the dashboard interprets direction (lower position = better rank).

    Args:
        run_data: The assembled run dict (uses its findings).
        summary: The summary from :func:`summarize`.
        data_dir: The data directory (to read the previous run record).

    Returns:
        ``{sites: {domain: {...}}, headline: {critical, warning, info}}``.
    """
    prev_rec = None
    try:
        with open(os.path.join(data_dir, "runs-index.json"), "r", encoding="utf-8") as fh:
            idx = json.load(fh)
        if isinstance(idx, list) and idx:
            prev_rec = idx[-1]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    prev_sites = (prev_rec or {}).get("sites", {})
    findings = run_data.get("findings", [])

    def delta(cur, prev):
        if isinstance(cur, (int, float)) and isinstance(prev, (int, float)):
            return round(cur - prev, 2)
        return None

    sites_out: Dict[str, Any] = {}
    for domain, s in summary.get("sites", {}).items():
        g = s.get("gsc") or {}
        p = prev_sites.get(domain, {})
        site_counts = {
            sev: sum(
                1
                for f in findings
                if f.get("site") == domain and f.get("severity") == sev
            )
            for sev in ("critical", "warning", "info")
        }
        sites_out[domain] = {
            "verdict": s.get("verdict"),
            "position": g.get("position"),
            "position_delta": delta(g.get("position"), p.get("gsc_position")),
            "impressions": g.get("impressions"),
            "impressions_delta": delta(g.get("impressions"), p.get("gsc_impressions")),
            "findings": site_counts,
        }

    counts = summary.get("findings_counts", {})
    return {
        "sites": sites_out,
        "headline": {
            "critical": counts.get("critical", 0),
            "warning": counts.get("warning", 0),
            "info": counts.get("info", 0),
        },
    }


def _update_runs_index(
    run_data: Dict[str, Any], summary: Dict[str, Any], data_dir: str, logger=None
) -> str:
    """Append a compact record for this run to ``runs-index.json`` (capped).

    The dashboard charts read this file. Each record is small (per-site verdict,
    worst status and GSC clicks/impressions/position) so the time-series stays
    cheap even over hundreds of runs.

    Args:
        run_data: The assembled run dict.
        summary: The summary from :func:`summarize`.
        data_dir: The data directory.
        logger: Optional logger.

    Returns:
        The path to ``runs-index.json``.
    """
    log = logger or setup_logging()
    path = os.path.join(data_dir, "runs-index.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            index = json.load(fh)
        if not isinstance(index, list):
            index = []
    except (FileNotFoundError, json.JSONDecodeError):
        index = []

    gsc = run_data.get("gsc", {}) or {}
    gsc_sites = gsc.get("sites", {}) if gsc.get("enabled") else {}
    sites_rec: Dict[str, Any] = {}
    for domain, s in summary.get("sites", {}).items():
        totals = (gsc_sites.get(domain, {}).get("search_analytics") or {}).get("totals") or {}
        sites_rec[domain] = {
            "verdict": s.get("verdict"),
            "worst_status": s.get("worst_status"),
            "gsc_position": totals.get("position"),
            "gsc_impressions": totals.get("impressions"),
            "gsc_clicks": totals.get("clicks"),
        }

    index.append(
        {
            "timestamp_utc": run_data.get("timestamp_utc"),
            "run_id": run_data.get("run_id"),
            "sites": sites_rec,
            "findings_counts": summary.get("findings_counts", {}),
        }
    )
    index = index[-RUNS_INDEX_CAP:]  # newest last, bounded

    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(index, fh, indent=2)
    log.info("Updated runs-index.json (%d runs)", len(index))
    return path


def _write_dashboard(data_dir: str, logger=None) -> Optional[str]:
    """Ensure ``index.html`` in the data dir matches the dashboard template.

    Writes (or refreshes) the static dashboard so GitHub Pages serves the latest
    version from the data branch. Only rewrites when the content changed.

    Args:
        data_dir: The data directory.
        logger: Optional logger.

    Returns:
        The destination path, or None if the template is missing.
    """
    log = logger or setup_logging()
    if not os.path.exists(DASHBOARD_TEMPLATE):
        log.warning("dashboard template not found at %s; skipping", DASHBOARD_TEMPLATE)
        return None
    with open(DASHBOARD_TEMPLATE, "r", encoding="utf-8") as fh:
        template = fh.read()

    dest = os.path.join(data_dir, "index.html")
    existing = None
    if os.path.exists(dest):
        with open(dest, "r", encoding="utf-8") as fh:
            existing = fh.read()
    if existing != template:
        with open(dest, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(template)
        log.info("Wrote dashboard index.html")
    return dest


def main() -> None:
    """CLI entry point: run all checks fresh and write a real report."""
    logger = setup_logging()
    config = load_config()
    try:
        from . import analyze, compare_duplicates, gsc, monitor_response
    except ImportError:
        import analyze  # type: ignore
        import compare_duplicates  # type: ignore
        import gsc  # type: ignore
        import monitor_response  # type: ignore

    current = {
        "monitor": monitor_response.run(config, logger),
        "duplicates": compare_duplicates.run(config, logger),
        "gsc": gsc.run(config, logger),
    }
    previous = analyze.load_previous_run()
    current["findings"] = analyze.analyze(current, previous, config, logger)
    current["timestamp_utc"] = utc_now_iso()
    current["run_id"] = utc_now_compact()
    write_report(current, config, dry_run=False, logger=logger)


if __name__ == "__main__":
    main()
