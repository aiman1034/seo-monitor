"""Redirect & reinstatement tracker (Phase 5) — separate, daily subsystem.

Tracks mirror domains that have no Search Console. It reads the Original URLs
(column C) from a Google Sheet — the place the user manages which domains/URLs to
track — probes each URL, and classifies whether it is **redirected** vs
**reinstated** over time, including whole-domain redirects to a new host. Results
(state + dated history) are written as JSON to the ``data`` branch and surfaced on
the dashboard.

It does NOT touch the hourly GSC monitor. It runs once daily (heavy probing — be
polite).

IMPORTANT — datacenter IPs: these domains may serve a bot-protection challenge /
403 to non-residential IPs (as we saw on the .tech site, and GitHub Actions
runners are datacenter IPs). ``BLOCKED`` is recorded DISTINCTLY so it is never
mistaken for a redirect. If a large share come back BLOCKED from the runner, the
fallback is to ingest the sheet's own computed columns (filled from Google IPs).

Run locally to preview classifications without writing anything:

    python scripts/redirect_tracker.py --dry-run
    python scripts/redirect_tracker.py --dry-run --limit 5   # sample per domain
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

import requests

try:
    from .common import DATA_DIR, load_config, setup_logging, utc_now_iso
except ImportError:  # allow running as a plain script
    from common import DATA_DIR, load_config, setup_logging, utc_now_iso  # type: ignore

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Substrings that indicate a bot-protection challenge (Cloudflare et al.).
_CHALLENGE_MARKERS = (
    "just a moment",
    "attention required",
    "cf-chl",
    "cf-challenge",
    "enable javascript and cookies",
    "checking your browser",
    "/cdn-cgi/challenge-platform",
    "ddos protection by",
)

_GVIZ_URL = "https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={tab}"

_throttle = threading.Lock()
_cache_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Sheet reading (column C = Original URL; A, B = section/label)
# --------------------------------------------------------------------------- #
def read_tab_gviz(sheet_id: str, tab: str, timeout: float, logger) -> List[Dict[str, str]]:
    """Read one sheet tab via the gviz CSV endpoint; return rows with a URL in C.

    Args:
        sheet_id: The spreadsheet ID.
        tab: The tab (sheet) name.
        timeout: Request timeout in seconds.
        logger: Logger.

    Returns:
        A list of {section, label, original} dicts (header + section-only rows skipped).
    """
    url = _GVIZ_URL.format(sid=sheet_id, tab=quote(tab))
    resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout)
    resp.raise_for_status()
    rows: List[Dict[str, str]] = []
    reader = csv.reader(io.StringIO(resp.text))
    # Read EVERY data row to the end of the tab. Section-header / blank / label-only
    # rows (where column C is not an http URL) are skipped WITHOUT stopping, so
    # multi-section tabs ("Top Leagues", "Top Teams", ...) are read in full.
    for i, cols in enumerate(reader):
        if i == 0:
            continue  # header
        if len(cols) < 3:
            continue
        original = cols[2].strip().lstrip("﻿")  # tolerate stray BOM
        if not original.lower().startswith("http"):
            continue  # section-only or empty row — skip, don't stop
        rows.append(
            {"section": cols[0].strip(), "label": cols[1].strip(), "original": original}
        )
    logger.info("  sheet[%s]: %d original URLs", tab, len(rows))
    return rows


def read_tab(config: Dict[str, Any], tab: str, logger) -> List[Dict[str, str]]:
    """Read a tab using the configured method (gviz by default)."""
    rcfg = config["redirects"]
    timeout = float(rcfg.get("request", {}).get("timeout_seconds", 15))
    method = rcfg.get("read_method", "gviz")
    if method == "gviz":
        return read_tab_gviz(rcfg["sheet_id"], tab, timeout, logger)
    raise ValueError(f"Unsupported redirects.read_method: {method}")


# --------------------------------------------------------------------------- #
# Probing + classification
# --------------------------------------------------------------------------- #
def _is_homepage(url: str) -> bool:
    """True if a URL points at a domain root / homepage."""
    path = urlparse(url).path or "/"
    return path in ("", "/", "/index.html", "/home")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().lstrip("www.")


def clean_original(url: str) -> str:
    """Strip a trailing ``/<id>`` segment and a ``-streams``/``-stream`` suffix.

    e.g. ``/soccer-streams`` -> ``/soccer``; ``/news/12345`` -> ``/news``.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    segments = [s for s in path.split("/") if s]
    if segments and re.fullmatch(r"[0-9]+|[0-9a-fA-F-]{6,}", segments[-1]):
        segments.pop()  # drop trailing id-like segment
    if segments:
        segments[-1] = re.sub(r"-streams?$", "", segments[-1])
    new_path = "/" + "/".join(segments)
    return f"{parsed.scheme}://{parsed.netloc}{new_path}"


def probe_url(
    url: str, timeout: float, max_redirects: int, delay: float,
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch a URL following redirects; classify reachability. Errors-as-data.

    Returns a dict with final_url, status_code, redirect_chain, blocked, error.
    If ``cache`` is given, a previously-probed URL is returned from it (so the
    network-map pass reuses homepages already fetched by the sheet-tabs pass
    instead of double-probing the same URL in one run).
    """
    if cache is not None:
        with _cache_lock:
            if url in cache:
                return cache[url]
    out: Dict[str, Any] = {
        "url": url,
        "final_url": None,
        "status_code": None,
        "redirect_chain": [],
        "blocked": False,
        "error": None,
    }
    with _throttle:
        time.sleep(delay)  # politeness, serialised so concurrency stays gentle
    try:
        session = requests.Session()
        session.max_redirects = max_redirects
        resp = session.get(
            url,
            headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"},
            timeout=timeout,
            allow_redirects=True,
        )
        out["status_code"] = resp.status_code
        out["final_url"] = resp.url
        out["redirect_chain"] = [
            {"url": r.url, "status_code": r.status_code} for r in resp.history
        ]
        body_sample = resp.text[:4000].lower() if resp.text else ""
        challenged = any(m in body_sample for m in _CHALLENGE_MARKERS)
        if resp.status_code in (403, 429, 503) and challenged:
            out["blocked"] = True
        elif resp.status_code == 403:
            out["blocked"] = True  # plain 403 from these hosts = bot block
        elif challenged:
            out["blocked"] = True
        resp.close()
    except requests.exceptions.RequestException as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    if cache is not None:
        with _cache_lock:
            cache[url] = out
    return out


def classify_status(probe: Dict[str, Any], original_url: str) -> str:
    """Compute the Status (column E) string for the original-URL probe."""
    if probe["blocked"]:
        return "BLOCKED"
    if probe["error"]:
        return "ERROR"
    redirected = bool(probe["redirect_chain"])
    code = probe["redirect_chain"][0]["status_code"] if redirected else probe["status_code"]
    if redirected and _host(probe["final_url"]) and probe["status_code"] and probe["status_code"] < 400:
        if _is_homepage(probe["final_url"]):
            return f"Redirect ({code}) -> HOMEPAGE"
        return f"Redirect ({code}) -> {probe['final_url']}"
    if probe["status_code"] == 200:
        return "Live (200)"
    return f"ERROR ({probe['status_code']})"


def _single_class(probe: Optional[Dict[str, Any]]) -> str:
    """Reduce a probe to one of: reinstated/redirected/redirected_homepage/dead/blocked."""
    if probe is None:
        return "dead"
    if probe["blocked"]:
        return "blocked"
    if probe["error"]:
        return "dead"
    if probe["redirect_chain"]:
        return "redirected_homepage" if _is_homepage(probe["final_url"]) else "redirected"
    if probe["status_code"] == 200:
        return "reinstated"
    return "dead"


def classify_reinstated(
    orig: Dict[str, Any], clean: Optional[Dict[str, Any]]
) -> str:
    """Compute Redirected-vs-Reinstated (column I) from original + clean probes."""
    co, cc = _single_class(orig), _single_class(clean)
    classes = {co, cc}
    if "reinstated" in classes:
        return "Reinstated (200)"
    if "redirected" in classes:
        return "Redirected (3xx)"
    if "redirected_homepage" in classes:
        return "Redirected -> homepage"
    if "blocked" in classes:
        return "Blocked"
    return "Dead"


def _probe_row(
    row: Dict[str, str], domain: str, timeout: float, max_redirects: int, delay: float,
    cache: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Probe one row's original + clean-original URLs and classify everything."""
    original = row["original"]
    clean = clean_original(original)
    orig_probe = probe_url(original, timeout, max_redirects, delay, cache=cache)
    clean_probe = (
        probe_url(clean, timeout, max_redirects, delay, cache=cache)
        if clean != original else orig_probe
    )

    return {
        "section": row["section"],
        "label": row["label"],
        "original": original,
        "current": orig_probe["final_url"],
        "status": classify_status(orig_probe, original),
        "redirected_vs_reinstated": classify_reinstated(orig_probe, clean_probe),
        "clean_original": clean,
        "clean_resolves_to": clean_probe["final_url"],
        "blocked": orig_probe["blocked"],
        "redirect_chain": orig_probe["redirect_chain"],
        "_final_host": _host(orig_probe["final_url"]) if orig_probe["final_url"] else None,
    }


# Minimum distinct non-homepage URLs needed before we'll judge a whole domain.
_MIN_NON_HOMEPAGE = 3


def _domain_status(domain: str, urls: List[Dict[str, Any]], threshold: float) -> str:
    """Classify a domain's overall status from a MEANINGFUL sample.

    Returns ``DOMAIN_REDIRECTED -> host`` only when both hold:
      * at least ``_MIN_NON_HOMEPAGE`` distinct non-homepage URLs were probed, and
      * >= ``threshold`` (e.g. 60%) of probed URLs redirect off-domain to that host.

    A lone homepage (or any single URL) redirecting must NOT trigger it — that URL
    is classified individually and the domain stays ``active``. A domain too thin
    to judge (e.g. only the homepage, or nothing reachable) is reported honestly as
    ``insufficient_sample`` rather than over-concluding.
    """
    base = domain.lower().lstrip("www.")
    probed = [u for u in urls if not u["blocked"] and u["current"]]
    non_homepage = [u for u in probed if not _is_homepage(u["original"])]

    if not probed or len(non_homepage) < _MIN_NON_HOMEPAGE:
        # Not enough to conclude a whole-domain redirect.
        return "insufficient_sample" if len(probed) <= 1 else "active"

    off_counts: Dict[str, int] = {}
    for u in probed:
        h = u["_final_host"]
        if h and h != base:
            off_counts[h] = off_counts.get(h, 0) + 1
    if not off_counts:
        return "active"

    host, n = max(off_counts.items(), key=lambda kv: kv[1])
    if n / len(probed) >= threshold:
        return f"DOMAIN_REDIRECTED -> {host}"
    return "active"


def _summarize(urls: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count URLs by class for a domain summary card."""
    out = {"live": 0, "redirected": 0, "redirected_to_homepage": 0,
           "reinstated": 0, "dead": 0, "blocked": 0, "total": len(urls)}
    for u in urls:
        s, rvr = u["status"], u["redirected_vs_reinstated"]
        if u["blocked"]:
            out["blocked"] += 1
        elif s.startswith("Live"):
            out["live"] += 1
        elif "HOMEPAGE" in s:
            out["redirected_to_homepage"] += 1
        elif s.startswith("Redirect"):
            out["redirected"] += 1
        if rvr.startswith("Reinstated"):
            out["reinstated"] += 1
        elif rvr == "Dead":
            out["dead"] += 1
    return out


def run(
    config: Dict[str, Any], logger=None, limit: Optional[int] = None
) -> Dict[str, Any]:
    """Probe all configured domains and classify each URL. Errors-as-data.

    Args:
        config: Loaded configuration.
        logger: Optional logger.
        limit: If set, probe at most this many URLs per domain (for a fast local
            sample).

    Returns:
        ``{timestamp_utc, domains: {domain: {domain_status, urls, summary}}}``.
    """
    log = logger or setup_logging()
    rcfg = config["redirects"]
    req = rcfg.get("request", {})
    timeout = float(req.get("timeout_seconds", 15))
    delay = float(req.get("delay_seconds", 0.5))
    max_redirects = int(req.get("max_redirects", 10))
    max_workers = int(req.get("max_workers", 4))
    threshold = float(rcfg.get("domain_redirect_threshold", 0.6))

    result: Dict[str, Any] = {
        "timestamp_utc": utc_now_iso(),
        "sheet_id": rcfg.get("sheet_id"),
        "domains": {},
    }
    # Run-scoped probe cache so the network-map pass reuses homepages already
    # fetched by the sheet-tabs pass (no double-probing in one run).
    probe_cache: Dict[str, Any] = {}

    for tab in rcfg.get("tabs", []):
        try:
            rows = read_tab(config, tab, log)
        except Exception as exc:
            log.warning("sheet read failed for %s: %s", tab, exc)
            result["domains"][tab] = {"domain_status": "active", "urls": [],
                                      "summary": {}, "error": f"sheet read: {exc}"}
            continue
        if limit:
            rows = rows[:limit]

        log.info("probing %s (%d URLs)...", tab, len(rows))
        urls: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [
                pool.submit(_probe_row, r, tab, timeout, max_redirects, delay, probe_cache)
                for r in rows
            ]
            for fut in futures:
                try:
                    urls.append(fut.result())
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("probe failed: %s", exc)

        domain_status = _domain_status(tab, urls, threshold)
        summary = _summarize(urls)
        result["domains"][tab] = {
            "domain_status": domain_status,
            "urls": urls,
            "summary": summary,
        }
        log.info(
            "  %s -> %s | live=%d redirected=%d ->home=%d reinstated=%d dead=%d blocked=%d",
            tab, domain_status, summary["live"], summary["redirected"],
            summary["redirected_to_homepage"], summary["reinstated"],
            summary["dead"], summary["blocked"],
        )

    # Competitor watchlist — external homepages, same prober, status-only framing.
    wl_urls = _watchlist_urls(rcfg.get("watchlist", []))
    if wl_urls:
        log.info("probing watchlist (%d URLs)...", len(wl_urls))
        watchlist: List[Dict[str, Any]] = []
        for url in wl_urls:
            p = probe_url(url, timeout, max_redirects, delay)
            watchlist.append(
                {
                    "url": url,
                    "current": p["final_url"],
                    "status": classify_status(p, url),
                    "blocked": p["blocked"],
                    "redirect_chain": p["redirect_chain"],
                }
            )
            log.info("  watch %s -> %s", url, watchlist[-1]["status"])
        result["watchlist"] = watchlist

    # Network redirect map — our own domains' homepages: where does each land now?
    # Reuses cached homepage probes (e.g. thin sheet tabs already fetched these).
    net_domains = rcfg.get("network", [])
    if net_domains:
        log.info("probing network map (%d domains)...", len(net_domains))
        network: List[Dict[str, Any]] = []
        for domain in net_domains:
            hp = f"https://{domain}/"
            p = probe_url(hp, timeout, max_redirects, delay, cache=probe_cache)
            cls = _network_classify(domain, p)
            network.append({
                "domain": domain,
                "lands_on": cls["lands_on"],
                "status": cls["status"],
                "final_url": p.get("final_url"),
            })
            log.info("  net %s -> %s", domain, cls["lands_on"])
        result["network"] = network

    return result


def _network_classify(domain: str, probe: Dict[str, Any]) -> Dict[str, Any]:
    """From a homepage probe, return where the domain lands + a status string.

    ``lands_on`` is ``self`` (standalone/live), a host (redirected off-domain),
    ``blocked``, or ``dead``.
    """
    base = domain.lower().lstrip("www.")
    if probe.get("blocked"):
        return {"lands_on": "blocked", "status": "Blocked"}
    if probe.get("error"):
        return {"lands_on": "dead", "status": "Dead"}
    sc = probe.get("status_code")
    final_host = _host(probe.get("final_url"))
    if sc and sc >= 400:
        return {"lands_on": "dead", "status": f"Dead ({sc})"}
    if final_host and final_host != base:
        chain = probe.get("redirect_chain") or []
        code = chain[0]["status_code"] if chain else (sc or "3xx")
        return {"lands_on": final_host, "status": f"Redirect ({code}) -> {final_host}"}
    return {"lands_on": "self", "status": "Live (200)" if sc == 200 else f"HTTP {sc}"}


def _watchlist_urls(entries: List[Any]) -> List[str]:
    """Expand watchlist config entries (string URL or {url, paths}) into URLs."""
    out: List[str] = []
    for e in entries:
        if isinstance(e, str):
            url = e if e.startswith("http") else f"https://{e}"
            out.append(url)
        elif isinstance(e, dict):
            base = e.get("url") or e.get("domain") or ""
            if base and not base.startswith("http"):
                base = f"https://{base}"
            if base:
                out.append(base)
                origin = f"{urlparse(base).scheme}://{urlparse(base).netloc}"
                for p in e.get("paths", []):
                    out.append(origin + (p if p.startswith("/") else f"/{p}"))
    return out


def _url_state(u: Dict[str, Any], last_checked: str) -> Dict[str, Any]:
    """Project an internal probe result into the persisted URL state shape."""
    return {
        "section": u["section"],
        "label": u["label"],
        "original": u["original"],
        "current": u["current"],
        "status": u["status"],
        "redirected_vs_reinstated": u["redirected_vs_reinstated"],
        "clean_original": u["clean_original"],
        "clean_resolves_to": u["clean_resolves_to"],
        "blocked": u["blocked"],
        "redirect_chain": u["redirect_chain"],
        "last_checked": last_checked,
        "history": [],
    }


def write_results(
    result: Dict[str, Any], data_dir: str, history_cap: int, logger
) -> Dict[str, Any]:
    """Persist per-domain state + dated history and the redirects index.

    For each domain, loads the previous ``redirects/<domain>.json``, carries each
    URL's history forward, and appends a dated entry whenever its status or
    redirected-vs-reinstated classification changed (column J). Writes the
    per-domain files and ``redirects-index.json`` (per-domain summary counts +
    domain_status + last-checked).

    Args:
        result: Output of :func:`run`.
        data_dir: Target directory (the data-branch worktree in CI).
        history_cap: Max history entries kept per URL.
        logger: Logger.

    Returns:
        The index dict that was written.
    """
    out_dir = os.path.join(data_dir, "redirects")
    os.makedirs(out_dir, exist_ok=True)
    date = result["timestamp_utc"][:10]
    index: Dict[str, Any] = {
        "checked_utc": result["timestamp_utc"],
        "sheet_id": result.get("sheet_id"),
        "domains": {},
    }

    for domain, d in result["domains"].items():
        path = os.path.join(out_dir, f"{domain}.json")
        prev_urls: Dict[str, Any] = {}
        prev_domain_status = None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                prev = json.load(fh)
            prev_urls = {u["original"]: u for u in prev.get("urls", [])}
            prev_domain_status = prev.get("domain_status")
        except (FileNotFoundError, json.JSONDecodeError):
            pass

        url_states: List[Dict[str, Any]] = []
        for u in d.get("urls", []):
            state = _url_state(u, date)
            prev_u = prev_urls.get(u["original"])
            cur_key = f"{state['status']} / {state['redirected_vs_reinstated']}"
            if prev_u is None:
                history = [{"date": date, "change": f"Baseline: {cur_key}"}]
            else:
                history = list(prev_u.get("history", []))
                prev_key = (
                    f"{prev_u.get('status')} / {prev_u.get('redirected_vs_reinstated')}"
                )
                if prev_key != cur_key:
                    history.append({"date": date, "change": f"{prev_key} -> {cur_key}"})
            state["history"] = history[-history_cap:]
            url_states.append(state)

        domain_status = d.get("domain_status", "active")
        domain_doc = {
            "domain": domain,
            "domain_status": domain_status,
            "checked_utc": result["timestamp_utc"],
            "urls": url_states,
        }
        if d.get("error"):
            domain_doc["error"] = d["error"]
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(domain_doc, fh, indent=2)

        summary = d.get("summary", {})
        index["domains"][domain] = {
            **summary,
            "domain_status": domain_status,
            "domain_status_changed": (
                prev_domain_status is not None and prev_domain_status != domain_status
            ),
            "last_checked": date,
        }

    # Competitor watchlist — status-only, with the same dated-history mechanism.
    if result.get("watchlist") is not None:
        wl_path = os.path.join(out_dir, "watchlist.json")
        prev_wl: Dict[str, Any] = {}
        try:
            with open(wl_path, "r", encoding="utf-8") as fh:
                prev_wl = {e["url"]: e for e in json.load(fh).get("entries", [])}
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        wl_entries: List[Dict[str, Any]] = []
        for w in result["watchlist"]:
            prev_w = prev_wl.get(w["url"])
            if prev_w is None:
                history = [{"date": date, "change": f"Baseline: {w['status']}"}]
            else:
                history = list(prev_w.get("history", []))
                if prev_w.get("status") != w["status"]:
                    history.append({"date": date, "change": f"{prev_w.get('status')} -> {w['status']}"})
            wl_entries.append({**w, "last_checked": date, "history": history[-history_cap:]})
        with open(wl_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"checked_utc": result["timestamp_utc"], "entries": wl_entries}, fh, indent=2)
        index["watchlist"] = [
            {"url": e["url"], "status": e["status"], "current": e["current"],
             "blocked": e["blocked"], "last_checked": e["last_checked"]}
            for e in wl_entries
        ]

    # Network redirect map — dated history keyed on WHERE each domain lands.
    if result.get("network") is not None:
        net_path = os.path.join(out_dir, "network.json")
        prev_net: Dict[str, Any] = {}
        try:
            with open(net_path, "r", encoding="utf-8") as fh:
                prev_net = {e["domain"]: e for e in json.load(fh).get("domains", [])}
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        net_entries: List[Dict[str, Any]] = []
        for n in result["network"]:
            prev_n = prev_net.get(n["domain"])
            if prev_n is None:
                history = [{"date": date, "change": f"Baseline: lands on {n['lands_on']}"}]
            else:
                history = list(prev_n.get("history", []))
                if prev_n.get("lands_on") != n["lands_on"]:
                    history.append({"date": date,
                                    "change": f"{prev_n.get('lands_on')} -> {n['lands_on']}"})
            net_entries.append({**n, "last_checked": date, "history": history[-history_cap:]})
        with open(net_path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"checked_utc": result["timestamp_utc"], "domains": net_entries}, fh, indent=2)
        index["network"] = [
            {"domain": e["domain"], "lands_on": e["lands_on"], "status": e["status"],
             "last_checked": e["last_checked"]}
            for e in net_entries
        ]

    with open(os.path.join(data_dir, "redirects-index.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(index, fh, indent=2)
    logger.info(
        "Wrote redirects/*.json (%d domains, %d watchlist, %d network) + redirects-index.json",
        len(index["domains"]), len(index.get("watchlist", [])), len(index.get("network", [])),
    )
    return index


def _print_report(result: Dict[str, Any]) -> None:
    """Print a human-readable dry-run report + overall BLOCKED rate."""
    total = blocked = 0
    print("\n" + "=" * 72)
    for domain, d in result["domains"].items():
        s = d.get("summary", {})
        print(f"\n## {domain}  [{d['domain_status']}]")
        if d.get("error"):
            print(f"   ERROR: {d['error']}")
        for u in d["urls"]:
            total += 1
            if u["blocked"]:
                blocked += 1
            print(f"   {u['status']:<42} | {u['redirected_vs_reinstated']:<22} | {u['original']}")
    print("\n" + "=" * 72)
    rate = (blocked / total * 100) if total else 0
    print(f"TOTAL probed: {total} | BLOCKED: {blocked} ({rate:.0f}%)")
    if rate >= 40:
        print("WARNING: high BLOCKED rate — consider the sheet-columns fallback.")


def main() -> int:
    """CLI entry point. ``--dry-run`` prints classifications, writes nothing."""
    parser = argparse.ArgumentParser(description="Probe mirror-domain redirects.")
    parser.add_argument("--dry-run", action="store_true", help="Print only; write nothing.")
    parser.add_argument("--limit", type=int, default=None, help="Max URLs per domain (sampling).")
    args = parser.parse_args()

    logger = setup_logging()
    config = load_config()
    if not config.get("redirects", {}).get("enabled", False):
        print("redirects disabled in config.")
        return 0

    result = run(config, logger, limit=args.limit)
    if args.dry_run:
        _print_report(result)
    else:
        history_cap = int(config["redirects"].get("history_cap", 100))
        write_results(result, DATA_DIR, history_cap, logger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
