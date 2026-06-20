"""Google Search Console collector (Phase 2) — the authoritative crawl signal.

Why this exists: the fetch-based monitor runs from a datacenter IP (incl. GitHub
Actions runners), and these sites return 403 to any datacenter/non-residential
IP (Cloudflare bot protection). So an HTTP fetch from our runner cannot tell us
whether Google can crawl the sites. Google crawls from its own allowlisted IPs,
and the Search Console **URL Inspection API** reports what Google actually sees —
its ``pageFetchState`` is the real answer to "is Google being blocked".

Auth: a Google Cloud **service account** with the Search Console API enabled
(works headless in Actions). Credentials are read from either:

* ``GSC_SERVICE_ACCOUNT_JSON`` — the raw service-account JSON string, or
* ``GSC_SERVICE_ACCOUNT_FILE`` — a path to the JSON key file.

If neither is set (or the libraries/config disable it), the module **gracefully
no-ops**: it logs "skipping" and returns ``{"enabled": False, ...}`` so the rest
of the pipeline runs normally in local dev.

Scope: ``https://www.googleapis.com/auth/webmasters.readonly``.

Findings produced HERE (current-state only): ``GOOGLE_FETCH_FAIL`` (critical).
The delta findings that need the previous run — ``POSITION_DROP``,
``IMPRESSIONS_DROP`` and ``DEINDEXED`` — are computed in ``analyze.py``, which is
where previous-run context lives (this ``run()`` has no access to it).

Run directly:

    python scripts/gsc.py
"""

from __future__ import annotations

import json
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

try:
    from .common import build_url, load_config, path_str, setup_logging, utc_now_iso
except ImportError:  # allow running as a plain script
    from common import build_url, load_config, path_str, setup_logging, utc_now_iso  # type: ignore

# Read-only Search Console scope.
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# URL Inspection pageFetchState values that mean Google fetched the page OK.
SUCCESS_FETCH_STATES = {"SUCCESSFUL"}


def is_noindex(indexing_state: Optional[str], coverage_state: Optional[str]) -> bool:
    """True when a URL is *intentionally* excluded from Google's index.

    A ``noindex`` meta tag / ``X-Robots-Tag`` is a deliberate signal (e.g. on a
    /dmca boilerplate page) — Google fetched the page fine and was told not to
    index it. That is correct behaviour, not a problem, so it must not be treated
    as a DEINDEXED regression.

    Args:
        indexing_state: GSC ``indexingState`` (e.g. INDEXING_ALLOWED,
            BLOCKED_BY_META_TAG, BLOCKED_BY_HTTP_HEADER).
        coverage_state: GSC ``coverageState`` (e.g. "Excluded by 'noindex' tag").

    Returns:
        True if the page is intentionally noindex.
    """
    state = (indexing_state or "").upper()
    if state in ("BLOCKED_BY_META_TAG", "BLOCKED_BY_HTTP_HEADER"):
        return True
    if "noindex" in (coverage_state or "").lower():
        return True
    return False


def _load_credentials(logger) -> Tuple[Optional[Any], Optional[str]]:
    """Load service-account credentials from env, or return a no-op reason.

    Returns:
        (credentials, None) on success, or (None, reason) when GSC should no-op.
    """
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    path = os.environ.get("GSC_SERVICE_ACCOUNT_FILE")
    if not raw and not path:
        return None, "GSC credentials not configured (set GSC_SERVICE_ACCOUNT_JSON or GSC_SERVICE_ACCOUNT_FILE)"

    try:
        from google.oauth2 import service_account  # lazy: optional dependency
    except ImportError as exc:
        return None, f"google-auth not installed ({exc})"

    try:
        if raw:
            info = json.loads(raw)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        else:
            creds = service_account.Credentials.from_service_account_file(
                path, scopes=SCOPES
            )
        return creds, None
    except (ValueError, json.JSONDecodeError, OSError) as exc:
        return None, f"could not load service-account credentials: {exc}"


def _build_service(creds) -> Any:
    """Build the Search Console API client (lazy import of googleapiclient)."""
    from googleapiclient.discovery import build  # lazy: optional dependency

    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def _rows_to_dicts(rows: List[Dict[str, Any]], key_name: str) -> List[Dict[str, Any]]:
    """Convert GSC searchanalytics rows into flat dicts keyed by ``key_name``."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        keys = r.get("keys", [])
        out.append(
            {
                key_name: keys[0] if keys else None,
                "clicks": r.get("clicks", 0),
                "impressions": r.get("impressions", 0),
                "ctr": r.get("ctr", 0.0),
                "position": r.get("position", 0.0),
            }
        )
    return out


def _aggregate_totals(daily: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate daily rows into window totals (impression-weighted position)."""
    clicks = sum(d["clicks"] for d in daily)
    impressions = sum(d["impressions"] for d in daily)
    weighted = sum(d["position"] * d["impressions"] for d in daily)
    position = round(weighted / impressions, 2) if impressions else None
    ctr = round(clicks / impressions, 4) if impressions else None
    return {
        "clicks": clicks,
        "impressions": impressions,
        "ctr": ctr,
        "position": position,
    }


def _search_analytics(
    service, prop: str, lookback_days: int, logger
) -> Dict[str, Any]:
    """Pull daily totals, per-query and per-page rows for the window.

    Note: GSC Search Analytics data lags ~2-3 days; we request through today and
    GSC simply returns whatever it has finalised.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)
    base = {"startDate": start.isoformat(), "endDate": end.isoformat()}

    def query(dimensions: List[str], row_limit: Optional[int] = None) -> List[Dict[str, Any]]:
        body = dict(base, dimensions=dimensions)
        if row_limit:
            body["rowLimit"] = row_limit
        resp = service.searchanalytics().query(siteUrl=prop, body=body).execute()
        return resp.get("rows", [])

    daily = _rows_to_dicts(query(["date"]), "date")
    queries = _rows_to_dicts(query(["query"], 100), "query")
    pages = _rows_to_dicts(query(["page"], 100), "page")
    # Per-country breakdown — keep the top ~10 by clicks. Reveals where each
    # domain actually ranks (position can differ a lot by market).
    by_country = _rows_to_dicts(query(["country"], 100), "country")
    by_country.sort(key=lambda r: r.get("clicks", 0), reverse=True)
    by_country = by_country[:10]

    return {
        "window": {"start": base["startDate"], "end": base["endDate"], "days": lookback_days},
        "totals": _aggregate_totals(daily),
        "daily": daily,
        "queries": queries,
        "pages": pages,
        "by_country": by_country,
    }


def _url_inspection(
    service, prop: str, urls: List[str], delay: float, logger
) -> List[Dict[str, Any]]:
    """Inspect each URL and capture Google's index status fields.

    Errors per URL are recorded in the record's ``error`` field, never raised.
    """
    out: List[Dict[str, Any]] = []
    for url in urls:
        rec: Dict[str, Any] = {"url": url, "error": None}
        try:
            resp = (
                service.urlInspection()
                .index()
                .inspect(body={"inspectionUrl": url, "siteUrl": prop})
                .execute()
            )
            result = resp.get("inspectionResult", {})
            idx = result.get("indexStatusResult", {})
            mobile = result.get("mobileUsabilityResult", {})
            verdict = idx.get("verdict")
            rec.update(
                verdict=verdict,
                coverageState=idx.get("coverageState"),
                robotsTxtState=idx.get("robotsTxtState"),
                indexingState=idx.get("indexingState"),
                pageFetchState=idx.get("pageFetchState"),
                lastCrawlTime=idx.get("lastCrawlTime"),
                googleCanonical=idx.get("googleCanonical"),
                userCanonical=idx.get("userCanonical"),
                mobileUsability=mobile.get("verdict"),
                indexed=(verdict == "PASS"),
                noindex=is_noindex(
                    idx.get("indexingState"), idx.get("coverageState")
                ),
            )
            logger.info(
                "  inspect %s -> verdict=%s fetch=%s coverage=%s",
                url,
                verdict,
                idx.get("pageFetchState"),
                idx.get("coverageState"),
            )
        except Exception as exc:  # API/network error for this URL — record it
            rec["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("  inspect %s failed: %s", url, rec["error"])
        out.append(rec)
        time.sleep(delay)
    return out


def _fetch_findings(domain: str, inspections: List[Dict[str, Any]], now: str) -> List[Dict[str, Any]]:
    """Emit GOOGLE_FETCH_FAIL (critical) for any URL Google could not fetch."""
    findings: List[Dict[str, Any]] = []
    for rec in inspections:
        state = rec.get("pageFetchState")
        if state and state not in SUCCESS_FETCH_STATES:
            findings.append(
                {
                    "type": "GOOGLE_FETCH_FAIL",
                    "severity": "critical",
                    "site": domain,
                    "path": rec.get("url"),
                    "message": (
                        f"Google could not fetch {rec.get('url')}: pageFetchState="
                        f"{state}. This is Google's own view (authoritative) — the "
                        f"site is blocking or failing Googlebot, not just our runner."
                    ),
                    "details": {
                        "pageFetchState": state,
                        "coverageState": rec.get("coverageState"),
                        "robotsTxtState": rec.get("robotsTxtState"),
                        "lastCrawlTime": rec.get("lastCrawlTime"),
                    },
                    "timestamp": now,
                }
            )
    return findings


def run(config: Dict[str, Any], logger=None) -> Dict[str, Any]:
    """Pull Search Console data for each configured site.

    Args:
        config: The loaded configuration.
        logger: Optional logger.

    Returns:
        ``{enabled, timestamp_utc, sites: {domain: {...}}, findings: [...]}``.
        ``enabled`` is False (and ``sites`` empty) when GSC is not configured.
    """
    log = logger or setup_logging()
    now = utc_now_iso()
    gsc_cfg = config.get("gsc", {}) or {}
    result: Dict[str, Any] = {
        "enabled": False,
        "timestamp_utc": now,
        "sites": {},
        "findings": [],
    }

    if not gsc_cfg.get("enabled", True):
        log.info("gsc: disabled in config; skipping")
        result["reason"] = "disabled in config"
        return result

    creds, reason = _load_credentials(log)
    if creds is None:
        log.info("gsc: %s; skipping", reason)
        result["reason"] = reason
        return result

    try:
        service = _build_service(creds)
    except Exception as exc:  # pragma: no cover - depends on optional libs
        log.warning("gsc: could not build client (%s); skipping", exc)
        result["reason"] = f"client build failed: {exc}"
        return result

    result["enabled"] = True
    lookback = int(gsc_cfg.get("lookback_days", 28))
    delay = float(config.get("request", {}).get("delay_seconds", 1.5))
    findings: List[Dict[str, Any]] = []

    for site in config.get("sites", []):
        domain = site["domain"]
        prop = site.get("gsc_property")
        if not prop:
            log.info("gsc: no gsc_property for %s; skipping that site", domain)
            continue

        log.info("gsc: querying %s (%s), lookback=%dd", domain, prop, lookback)
        site_res: Dict[str, Any] = {
            "property": prop,
            "search_analytics": None,
            "url_inspection": [],
            "error": None,
        }

        try:
            site_res["search_analytics"] = _search_analytics(service, prop, lookback, log)
        except Exception as exc:
            site_res["error"] = f"search_analytics: {type(exc).__name__}: {exc}"
            log.warning("gsc: search_analytics failed for %s: %s", domain, exc)

        urls = [build_url(domain, path_str(p)) for p in site.get("paths", ["/"])]
        try:
            inspections = _url_inspection(service, prop, urls, delay, log)
            site_res["url_inspection"] = inspections
            findings.extend(_fetch_findings(domain, inspections, now))
        except Exception as exc:
            prev = site_res["error"] + "; " if site_res["error"] else ""
            site_res["error"] = f"{prev}url_inspection: {type(exc).__name__}: {exc}"
            log.warning("gsc: url_inspection failed for %s: %s", domain, exc)

        result["sites"][domain] = site_res

    result["findings"] = findings
    log.info("gsc: done — %d site(s), %d finding(s)", len(result["sites"]), len(findings))
    return result


def main() -> None:
    """CLI entry point: run the GSC collector and print JSON."""
    logger = setup_logging()
    config = load_config()
    print(json.dumps(run(config, logger), indent=2))


if __name__ == "__main__":
    main()
