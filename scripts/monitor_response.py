"""Multi-user-agent HTTP availability checks.

For every site x every user-agent x every key path this fetches the URL and
records: HTTP status code, response time (ms), TTFB (ms), final URL after
redirects, the full redirect chain, content length and a SHA-256 of the body.

The headline detection is ``CRAWLER_BLOCKED``: if any Googlebot user-agent gets
a non-200 (especially 403) while the real browser user-agent gets 200, that is
flagged as a critical finding — the top hypothesis for the ranking volatility.
``MOBILE_FETCH_FAIL`` is flagged when mobile Googlebot fails but desktop
Googlebot succeeds.

A site being down, timing out or returning 403 is recorded as DATA, never raised
as an exception — one failing site or request must not stop the rest of the run.

Run directly to print the JSON for the configured sites:

    python scripts/monitor_response.py
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List

import requests

try:
    from .common import (
        build_url,
        iter_site_paths,
        load_config,
        setup_logging,
        utc_now_iso,
    )
except ImportError:  # allow running as a plain script (python scripts/monitor_response.py)
    from common import (  # type: ignore
        build_url,
        iter_site_paths,
        load_config,
        setup_logging,
        utc_now_iso,
    )


def fetch_once(
    url: str,
    user_agent: str,
    timeout: float,
    verify_ssl: bool,
    max_redirects: int,
) -> Dict[str, Any]:
    """Fetch a single URL with a given user-agent and record the result.

    Network failures (DNS, connection, timeout, SSL) are caught and returned as a
    record with ``error`` set and ``status_code`` of None — never raised.

    Args:
        url: The absolute URL to fetch.
        user_agent: The User-Agent header value to send.
        timeout: Per-request timeout in seconds.
        verify_ssl: Whether to verify TLS certificates.
        max_redirects: Maximum redirects to follow.

    Returns:
        A structured result dict (JSON-serialisable).
    """
    headers = {"User-Agent": user_agent, "Accept": "*/*"}
    session = requests.Session()
    session.max_redirects = max_redirects

    result: Dict[str, Any] = {
        "url": url,
        "status_code": None,
        "response_time_ms": None,
        "ttfb_ms": None,
        "final_url": None,
        "redirect_chain": [],
        "content_length": None,
        "body_sha256": None,
        "error": None,
    }

    start = time.perf_counter()
    try:
        # stream=True lets us measure time-to-first-byte separately from the full
        # body download.
        resp = session.get(
            url,
            headers=headers,
            timeout=timeout,
            verify=verify_ssl,
            allow_redirects=True,
            stream=True,
        )
        ttfb = time.perf_counter()
        body = resp.content  # forces the full download
        end = time.perf_counter()

        result["status_code"] = resp.status_code
        result["ttfb_ms"] = round((ttfb - start) * 1000, 1)
        result["response_time_ms"] = round((end - start) * 1000, 1)
        result["final_url"] = resp.url
        result["redirect_chain"] = [
            {"url": r.url, "status_code": r.status_code} for r in resp.history
        ]
        result["content_length"] = len(body)
        result["body_sha256"] = hashlib.sha256(body).hexdigest()
        resp.close()
    except requests.exceptions.RequestException as exc:
        end = time.perf_counter()
        result["response_time_ms"] = round((end - start) * 1000, 1)
        result["error"] = f"{type(exc).__name__}: {exc}"

    return result


def run(config: Dict[str, Any], logger=None) -> Dict[str, Any]:
    """Run all HTTP checks and detect crawler-blocking findings.

    Args:
        config: The loaded configuration.
        logger: Optional logger; one is created if not supplied.

    Returns:
        A dict with keys ``timestamp_utc``, ``checks`` (list of per-request
        records) and ``findings`` (CRAWLER_BLOCKED / MOBILE_FETCH_FAIL).
    """
    log = logger or setup_logging()
    req_cfg = config["request"]
    timeout = float(req_cfg.get("timeout_seconds", 20))
    delay = float(req_cfg.get("delay_seconds", 1.5))
    verify_ssl = bool(req_cfg.get("verify_ssl", True))
    max_redirects = int(req_cfg.get("max_redirects", 10))

    user_agents: Dict[str, str] = config["user_agents"]
    crawler_labels: List[str] = config.get("crawler_user_agents", [])
    browser_label: str = config.get("browser_user_agent", "browser")

    checks: List[Dict[str, Any]] = []
    targets = iter_site_paths(config)

    log.info(
        "monitor_response: %d paths x %d user-agents = %d requests",
        len(targets),
        len(user_agents),
        len(targets) * len(user_agents),
    )

    for target in targets:
        for ua_label, ua_string in user_agents.items():
            url = target["url"]
            log.debug("GET %s as %s", url, ua_label)
            record = fetch_once(url, ua_string, timeout, verify_ssl, max_redirects)
            record["site"] = target["domain"]
            record["path"] = target["path"]
            record["user_agent_label"] = ua_label
            status = record["status_code"] if record["error"] is None else record["error"]
            log.info(
                "  %-18s %-7s -> %s (%sms)",
                ua_label,
                target["path"],
                status,
                record["response_time_ms"],
            )
            checks.append(record)
            time.sleep(delay)  # polite delay between requests

    findings = _detect_findings(checks, crawler_labels, browser_label, config)

    return {
        "timestamp_utc": utc_now_iso(),
        "checks": checks,
        "findings": findings,
    }


def _index_checks(checks: List[Dict[str, Any]]) -> Dict[tuple, Dict[str, Any]]:
    """Index check records by (site, path, user_agent_label) for lookups."""
    return {(c["site"], c["path"], c["user_agent_label"]): c for c in checks}


def _detect_findings(
    checks: List[Dict[str, Any]],
    crawler_labels: List[str],
    browser_label: str,
    config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Detect CRAWLER_BLOCKED and MOBILE_FETCH_FAIL across the check records.

    A request is "ok" when it returned HTTP 200 with no error. CRAWLER_BLOCKED is
    raised per (site, path) when the browser is ok but a Googlebot UA is not.
    MOBILE_FETCH_FAIL is raised when mobile Googlebot is not ok but desktop is.

    Args:
        checks: All per-request records from this run.
        crawler_labels: User-agent labels treated as Googlebot.
        browser_label: The reference browser user-agent label.
        config: The loaded configuration (for timestamps).

    Returns:
        A list of finding dicts.
    """
    indexed = _index_checks(checks)
    findings: List[Dict[str, Any]] = []
    now = utc_now_iso()

    # Unique (site, path) pairs.
    site_paths = sorted({(c["site"], c["path"]) for c in checks})

    def is_ok(rec: Dict[str, Any] | None) -> bool:
        return bool(rec) and rec.get("error") is None and rec.get("status_code") == 200

    for site, path in site_paths:
        browser_rec = indexed.get((site, path, browser_label))
        browser_ok = is_ok(browser_rec)

        for crawler_label in crawler_labels:
            crawler_rec = indexed.get((site, path, crawler_label))
            if crawler_rec is None:
                continue
            if browser_ok and not is_ok(crawler_rec):
                status = (
                    crawler_rec.get("status_code")
                    if crawler_rec.get("error") is None
                    else crawler_rec.get("error")
                )
                severity = "critical"
                findings.append(
                    {
                        "type": "CRAWLER_BLOCKED",
                        "severity": severity,
                        "site": site,
                        "path": path,
                        "message": (
                            f"{crawler_label} got '{status}' on {path} while "
                            f"browser got 200 — Googlebot may be blocked."
                        ),
                        "details": {
                            "crawler_user_agent": crawler_label,
                            "crawler_status": crawler_rec.get("status_code"),
                            "crawler_error": crawler_rec.get("error"),
                            "browser_status": 200,
                        },
                        "timestamp": now,
                    }
                )

        # MOBILE_FETCH_FAIL: mobile Googlebot fails but desktop Googlebot is ok.
        desktop_rec = indexed.get((site, path, "googlebot_desktop"))
        mobile_rec = indexed.get((site, path, "googlebot_mobile"))
        if is_ok(desktop_rec) and mobile_rec is not None and not is_ok(mobile_rec):
            status = (
                mobile_rec.get("status_code")
                if mobile_rec.get("error") is None
                else mobile_rec.get("error")
            )
            findings.append(
                {
                    "type": "MOBILE_FETCH_FAIL",
                    "severity": "warning",
                    "site": site,
                    "path": path,
                    "message": (
                        f"googlebot_mobile got '{status}' on {path} while "
                        f"googlebot_desktop got 200 — mobile crawl may fail."
                    ),
                    "details": {
                        "mobile_status": mobile_rec.get("status_code"),
                        "mobile_error": mobile_rec.get("error"),
                    },
                    "timestamp": now,
                }
            )

    return findings


def main() -> None:
    """CLI entry point: run checks for the configured sites and print JSON."""
    logger = setup_logging()
    config = load_config()
    result = run(config, logger)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
