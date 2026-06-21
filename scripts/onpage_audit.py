"""On-page & technical SEO audit (Phase 8) — the page-level issue-finding layer.

For each monitored page this fetches the HTML (with headers + redirect history)
and checks: title, meta description, H1 count, canonical (incl. cross-domain),
meta-robots / X-Robots-Tag noindex, structured data, image alt coverage, and
mixed content. Once per domain it checks robots.txt, sitemap.xml, the redirect
chain, the SSL certificate expiry, and — only if ``PSI_API_KEY`` is set — Core
Web Vitals via the PageSpeed Insights API.

Every issue becomes a finding (severity + later a ``fix`` from fixes.py). All
findings are warning/info except an already-expired SSL cert, so the audit never
triggers auto-alert Issues — it's a page-quality backlog surfaced on the
dashboard, not a paging signal.

Errors are recorded as data, never raised: a page that doesn't return 200 HTML
(blocked / redirecting off-domain / down) is skipped for page-level checks so the
audit can't emit bogus findings for an unreachable page.

Run directly:

    python scripts/onpage_audit.py
"""

from __future__ import annotations

import json
import os
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

try:
    from .common import build_url, load_config, setup_logging, utc_now_iso
except ImportError:  # allow running as a plain script
    from common import build_url, load_config, setup_logging, utc_now_iso  # type: ignore

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _host(url: Optional[str]) -> str:
    return (urlparse(url or "").hostname or "").lower().lstrip("www.")


def _finding(ftype: str, severity: str, site: str, path: str, message: str,
             details: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {
        "type": ftype, "severity": severity, "site": site, "path": path,
        "message": message, "details": details or {}, "timestamp": utc_now_iso(),
    }


def _fetch(url: str, timeout: float) -> Dict[str, Any]:
    """Fetch a URL (follow redirects); return body/headers/history. Errors-as-data."""
    out: Dict[str, Any] = {
        "url": url, "status_code": None, "final_url": None, "html": None,
        "headers": {}, "redirect_chain": [], "error": None,
    }
    try:
        resp = requests.get(
            url, headers={"User-Agent": BROWSER_UA, "Accept": "text/html,*/*"},
            timeout=timeout, allow_redirects=True,
        )
        out["status_code"] = resp.status_code
        out["final_url"] = resp.url
        out["headers"] = {k.lower(): v for k, v in resp.headers.items()}
        out["redirect_chain"] = [{"url": r.url, "status_code": r.status_code} for r in resp.history]
        ctype = out["headers"].get("content-type", "")
        if resp.status_code == 200 and "html" in ctype:
            out["html"] = resp.text
    except requests.exceptions.RequestException as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


# --------------------------------------------------------------------------- #
# Per-page checks
# --------------------------------------------------------------------------- #
def audit_page(
    domain: str, path: str, fetched: Dict[str, Any], soup: BeautifulSoup,
    config: Dict[str, Any], own_domains: set,
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Run page-level checks; return (findings, title, meta_description)."""
    o = config.get("onpage", {})
    t_min, t_max = int(o.get("title_min", 30)), int(o.get("title_max", 60))
    m_min, m_max = int(o.get("meta_min", 120)), int(o.get("meta_max", 160))
    findings: List[Dict[str, Any]] = []
    page_host = _host(fetched["final_url"] or build_url(domain, path))

    # Title
    title_tag = soup.find("title")
    title = title_tag.get_text(strip=True) if title_tag else None
    if not title:
        findings.append(_finding("TITLE_MISSING", "warning", domain, path, "Page has no <title> tag."))
    elif not (t_min <= len(title) <= t_max):
        findings.append(_finding("TITLE_LENGTH", "info", domain, path,
            f"Title is {len(title)} chars (target {t_min}-{t_max}): {title[:70]!r}."))

    # Meta description
    md = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    meta = (md.get("content") or "").strip() if md else None
    if not meta:
        findings.append(_finding("META_DESC_MISSING", "info", domain, path, "Page has no meta description."))
    elif not (m_min <= len(meta) <= m_max):
        findings.append(_finding("META_DESC_LENGTH", "info", domain, path,
            f"Meta description is {len(meta)} chars (target {m_min}-{m_max})."))

    # H1
    h1s = soup.find_all("h1")
    if len(h1s) == 0:
        findings.append(_finding("H1_MISSING", "warning", domain, path, "Page has no <h1>."))
    elif len(h1s) > 1:
        findings.append(_finding("H1_MULTIPLE", "info", domain, path, f"Page has {len(h1s)} <h1> tags (expected 1)."))

    # Canonical
    can = soup.find("link", rel=lambda v: v and "canonical" in [x.lower() for x in (v if isinstance(v, list) else [v])])
    if not can or not can.get("href"):
        findings.append(_finding("CANONICAL_MISSING", "warning", domain, path, "Page has no rel=canonical link."))
    else:
        chref = urljoin(fetched["final_url"] or build_url(domain, path), can["href"])
        chost = _host(chref)
        if chost and chost != page_host:
            to_own = chost in own_domains
            findings.append(_finding(
                "CANONICAL_CROSS_DOMAIN", "warning", domain, path,
                f"Canonical points to a different domain ({chost})"
                + (" — another of your own monitored domains; this hands it ranking." if to_own else ".")
                + f" canonical={chref}",
                {"canonical": chref, "canonical_host": chost, "to_own_domain": to_own}))

    # Meta robots / X-Robots-Tag noindex
    robots_meta = soup.find("meta", attrs={"name": re.compile(r"^robots$", re.I)})
    robots_val = (robots_meta.get("content") or "").lower() if robots_meta else ""
    xrobots = fetched["headers"].get("x-robots-tag", "").lower()
    if ("noindex" in robots_val or "noindex" in xrobots) and path == "/":
        # Only the homepage is unambiguously a "should rank" page. Noindex on
        # secondary paths (dmca/privacy/etc.) is usually intentional and is already
        # surfaced by the GSC layer (NOINDEX_PAGE), so we don't double-flag it here.
        src = "meta robots" if "noindex" in robots_val else "X-Robots-Tag"
        findings.append(_finding("ROBOTS_NOINDEX", "warning", domain, path,
            f"Homepage is noindex via {src} — it will not rank. Remove the noindex."))

    # Structured data
    has_jsonld = bool(soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}))
    has_microdata = bool(soup.find(attrs={"itemtype": True}))
    if not has_jsonld and not has_microdata:
        findings.append(_finding("STRUCTURED_DATA_MISSING", "info", domain, path,
            "No structured data (JSON-LD / schema.org) found on the page."))

    # Image alt coverage
    imgs = soup.find_all("img")
    if imgs:
        missing = [i for i in imgs if not (i.get("alt") or "").strip()]
        if missing:
            pct = round(len(missing) / len(imgs) * 100)
            sev = "warning" if pct >= 50 else "info"
            findings.append(_finding("IMAGE_ALT_MISSING", sev, domain, path,
                f"{pct}% of images ({len(missing)}/{len(imgs)}) are missing alt text.",
                {"missing": len(missing), "total": len(imgs), "pct": pct}))

    # Mixed content (http resources on an https page)
    if (fetched["final_url"] or "").startswith("https://"):
        http_assets = []
        for tag, attr in (("img", "src"), ("script", "src"), ("link", "href"), ("iframe", "src")):
            for el in soup.find_all(tag):
                v = el.get(attr) or ""
                if v.startswith("http://"):
                    http_assets.append(v)
        if http_assets:
            findings.append(_finding("MIXED_CONTENT", "warning", domain, path,
                f"{len(http_assets)} insecure http resource(s) referenced on an https page.",
                {"count": len(http_assets), "examples": http_assets[:3]}))

    return findings, title, meta


# --------------------------------------------------------------------------- #
# Per-domain checks
# --------------------------------------------------------------------------- #
def _check_robots_txt(domain: str, monitored_paths: List[str], timeout: float) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    res = _fetch(f"https://{domain}/robots.txt", timeout)
    if res["error"] or res["status_code"] != 200 or not res.get("html") and res["status_code"] != 200:
        return findings  # no robots.txt (or unreachable) — nothing to flag
    text = res["html"] if res["html"] is not None else ""
    if not text:
        # content-type may not be html; refetch raw not needed — treat empty as none
        return findings
    disallows = [m.group(1).strip() for m in re.finditer(r"(?im)^\s*Disallow:\s*(\S*)", text)]
    blocked = []
    for p in monitored_paths:
        for d in disallows:
            if d and (p == d or p.startswith(d) or d == "/"):
                blocked.append((p, d))
                break
    for p, d in blocked:
        findings.append(_finding("ROBOTS_TXT_BLOCKS_PATH", "warning", domain, p,
            f"robots.txt Disallow '{d}' blocks the monitored path {p}.", {"disallow": d}))
    return findings


def _check_sitemap(domain: str, monitored_urls: List[str], timeout: float) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    res = _fetch(f"https://{domain}/sitemap.xml", timeout)
    if res["error"] or res["status_code"] != 200:
        findings.append(_finding("SITEMAP_MISSING", "warning", domain, "/sitemap.xml",
            f"No reachable sitemap.xml (status {res['status_code'] or res['error']})."))
        return findings
    body = res.get("html")
    if body is None:
        # content-type wasn't html; fetch text directly
        try:
            body = requests.get(f"https://{domain}/sitemap.xml",
                                headers={"User-Agent": BROWSER_UA}, timeout=timeout).text
        except requests.exceptions.RequestException:
            body = ""
    if "<urlset" not in (body or "") and "<sitemapindex" not in (body or ""):
        findings.append(_finding("SITEMAP_MISSING", "warning", domain, "/sitemap.xml",
            "sitemap.xml is not valid XML (<urlset>/<sitemapindex> not found)."))
        return findings
    listed = set(re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body or ""))
    listed_norm = {u.rstrip("/") for u in listed}
    for u in monitored_urls:
        if u.rstrip("/") not in listed_norm and "<sitemapindex" not in (body or ""):
            findings.append(_finding("SITEMAP_MISSING_PAGE", "info", domain, urlparse(u).path or "/",
                f"Monitored URL {u} is not listed in sitemap.xml."))
    return findings


def _check_ssl(domain: str, warn_days: int) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((domain, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as ssock:
                cert = ssock.getpeercert()
        not_after = cert.get("notAfter")
        expiry = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        days = (expiry - datetime.now(timezone.utc)).days
        if days < 0:
            findings.append(_finding("SSL_EXPIRED", "critical", domain, "/",
                f"TLS certificate EXPIRED {abs(days)} day(s) ago ({not_after}).", {"days": days}))
        elif days <= warn_days:
            findings.append(_finding("SSL_EXPIRING_SOON", "warning", domain, "/",
                f"TLS certificate expires in {days} day(s) ({not_after}).", {"days": days}))
    except Exception as exc:  # connection / cert / DNS error — record nothing fatal
        return []
    return findings


def _check_psi(domain: str, logger) -> List[Dict[str, Any]]:
    """Core Web Vitals via PageSpeed Insights — only if PSI_API_KEY is set."""
    key = os.environ.get("PSI_API_KEY")
    if not key:
        return []
    findings: List[Dict[str, Any]] = []
    try:
        url = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
        resp = requests.get(url, params={"url": f"https://{domain}/", "key": key,
                                         "strategy": "mobile"}, timeout=60)
        data = resp.json()
        metrics = (data.get("loadingExperience", {}) or {}).get("metrics", {})
        checks = {
            "LARGEST_CONTENTFUL_PAINT_MS": ("LCP", 2500, "ms"),
            "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("CLS", 10, ""),  # CrUX reports CLS*100
            "INTERACTION_TO_NEXT_PAINT": ("INP", 200, "ms"),
        }
        for k, (label, limit, unit) in checks.items():
            p = metrics.get(k, {})
            val = p.get("percentile")
            if val is not None and val > limit:
                findings.append(_finding("CWV_POOR", "info", domain, "/",
                    f"Poor {label}: {val}{unit} (threshold {limit}{unit}) per CrUX field data.",
                    {"metric": label, "value": val}))
    except Exception as exc:
        logger.warning("PSI check failed for %s: %s", domain, exc)
    return findings


def run(config: Dict[str, Any], logger=None) -> Dict[str, Any]:
    """Audit every monitored page + domain; return {timestamp_utc, findings}."""
    log = logger or setup_logging()
    now = utc_now_iso()
    ocfg = config.get("onpage", {}) or {}
    result: Dict[str, Any] = {"enabled": bool(ocfg.get("enabled", True)),
                              "timestamp_utc": now, "findings": []}
    if not result["enabled"]:
        log.info("onpage: disabled in config; skipping")
        return result

    timeout = float(config.get("request", {}).get("timeout_seconds", 20))
    delay = float(config.get("request", {}).get("delay_seconds", 1.0))
    warn_days = int(ocfg.get("ssl_warn_days", 30))
    own_domains = {_host("https://" + s["domain"]) for s in config.get("sites", [])}
    findings: List[Dict[str, Any]] = []

    for site in config.get("sites", []):
        domain = site["domain"]
        paths = [p if isinstance(p, str) else p.get("path", "/") for p in site.get("paths", ["/"])]
        titles: Dict[str, List[str]] = {}
        metas: Dict[str, List[str]] = {}

        for path in paths:
            url = build_url(domain, path)
            fetched = _fetch(url, timeout)
            time.sleep(delay)
            # Only audit a real 200 HTML page served by THIS domain (skip blocked,
            # redirecting-off-domain, or non-HTML responses — avoids bogus findings).
            if fetched["html"] is None or _host(fetched["final_url"]) != _host(url):
                log.info("  %s%s: skipped page checks (status=%s, final=%s)",
                         domain, path, fetched["status_code"], fetched["final_url"])
            else:
                soup = BeautifulSoup(fetched["html"], "lxml")
                page_findings, title, meta = audit_page(domain, path, fetched, soup, config, own_domains)
                findings.extend(page_findings)
                if title:
                    titles.setdefault(title, []).append(path)
                if meta:
                    metas.setdefault(meta, []).append(path)

            # Redirect chain > 1 hop (leaks equity).
            if len(fetched["redirect_chain"]) > 1:
                findings.append(_finding("REDIRECT_CHAIN", "warning", domain, path,
                    f"{len(fetched['redirect_chain'])}-hop redirect chain to {fetched['final_url']}.",
                    {"hops": len(fetched["redirect_chain"])}))

        # Duplicate titles / metas across this site's pages.
        for title, ps in titles.items():
            if len(ps) > 1:
                findings.append(_finding("TITLE_DUPLICATE", "warning", domain, ", ".join(ps),
                    f"Duplicate <title> across {len(ps)} pages: {title[:60]!r}."))
        for meta, ps in metas.items():
            if len(ps) > 1:
                findings.append(_finding("META_DESC_DUPLICATE", "info", domain, ", ".join(ps),
                    f"Duplicate meta description across {len(ps)} pages."))

        # Per-domain technical checks.
        monitored_urls = [build_url(domain, p) for p in paths]
        try:
            findings.extend(_check_robots_txt(domain, paths, timeout))
            findings.extend(_check_sitemap(domain, monitored_urls, timeout))
            findings.extend(_check_ssl(domain, warn_days))
            findings.extend(_check_psi(domain, log))
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("domain checks failed for %s: %s", domain, exc)
        time.sleep(delay)

    result["findings"] = findings
    sev = {s: sum(1 for f in findings if f["severity"] == s) for s in ("critical", "warning", "info")}
    log.info("onpage: %d findings (critical=%d warning=%d info=%d)",
             len(findings), sev["critical"], sev["warning"], sev["info"])
    return result


def main() -> None:
    """CLI entry point: run the audit and print findings JSON."""
    logger = setup_logging()
    config = load_config()
    print(json.dumps(run(config, logger), indent=2))


if __name__ == "__main__":
    main()
