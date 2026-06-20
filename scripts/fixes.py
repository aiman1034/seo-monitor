"""Recommended fixes for each finding type (from the SEO issue playbook).

Findings carry a ``message`` (what happened); this module adds the ``fix`` (what
to do about it). Keeping the remedies in one mapping means the report markdown and
the dashboard show actionable guidance next to every finding, and new finding
types only need one entry here.

The fix text mirrors the project's SEO issue playbook so the dashboard and the
playbook stay in sync.
"""

from __future__ import annotations

from typing import Any, Dict, List

# Finding type -> one/two-sentence recommended fix.
FIX_RECOMMENDATIONS: Dict[str, str] = {
    # --- Crawlability & access ---
    "CRAWLER_BLOCKED": (
        "Allowlist Google's verified crawler IPs in your CDN/WAF and confirm via "
        "reverse-DNS; never block by user-agent string."
    ),
    "RUNNER_IP_BLOCKED": (
        "Not a real outage — it's bot protection rejecting this datacenter IP. "
        "Ignore for uptime and rely on Search Console (URL Inspection) for "
        "Google's authoritative crawl view."
    ),
    "FORBIDDEN_403": (
        "Identify what returns 403 (WAF/CDN rule, rate limit, geo/IP block) and "
        "allowlist legitimate crawlers; confirm Googlebot specifically is allowed."
    ),
    "SERVER_ERROR_5XX": (
        "Check hosting capacity and application logs, fix the server error, and "
        "add caching/CDN to absorb load."
    ),
    "FETCH_ERROR": (
        "Check DNS records, server uptime, SSL and firewall; verify the domain "
        "resolves and responds from outside your network."
    ),
    "MOBILE_FETCH_FAIL": (
        "Fix mobile serving/redirects so googlebot_mobile gets the same 200 as "
        "desktop; ensure responsive delivery (mobile-first indexing)."
    ),
    # --- Stability / change signals ---
    "STATUS_CHANGE": (
        "Investigate why the HTTP status changed since the last run — correlate "
        "with deploys, CDN/WAF rule changes or outages around that timestamp."
    ),
    "LATENCY_SPIKE": (
        "Add server/CDN caching, move to faster hosting and remove redirect hops; "
        "check for a load spike or slow backend at that time."
    ),
    "CONTENT_CHANGE": (
        "Informational — the page body changed. Confirm the change was intended "
        "(content update vs. defacement, broken template, or a rotating token)."
    ),
    # --- Duplicate content & cannibalization ---
    "DUPLICATE_CONTENT": (
        "Consolidate the competing domains into the strongest one with 301 "
        "redirects, or genuinely differentiate the content."
    ),
    "DUPLICATE_PAGE": (
        "Canonicalize the duplicate page to the preferred URL (rel=canonical) or "
        "301-redirect it to the version you want to rank."
    ),
    "DUPLICATE_CHECK_SKIPPED": (
        "Informational — pages couldn't be compared because one/both weren't 200. "
        "Resolve the non-200 status first, then duplication can be assessed."
    ),
    # --- Search Console (Google's view) ---
    "GOOGLE_FETCH_FAIL": (
        "Authoritative: Google itself can't fetch the page. Allowlist Google's "
        "verified crawler IPs, fix robots/server errors, and re-test in URL "
        "Inspection until pageFetchState is SUCCESSFUL."
    ),
    "DEINDEXED": (
        "A page that should rank has left the index. Check content quality, "
        "crawlability and penalties; add internal links and request indexing in "
        "Search Console once fixed."
    ),
    "NOINDEX_PAGE": (
        "Expected — the page is intentionally noindex (e.g. /dmca). No action "
        "needed unless this page should actually rank, in which case remove its "
        "noindex meta tag / X-Robots-Tag header."
    ),
    "POSITION_DROP": (
        "Work the playbook: check crawlability, duplicate/cannibalization "
        "(consolidate competing domains with 301s), content changes and known "
        "algorithm updates around the drop."
    ),
    "IMPRESSIONS_DROP": (
        "Check indexing and rankings, then seasonality and algorithm updates; "
        "confirm key pages are still indexed and served correctly."
    ),
}

# Shown when a finding type has no specific entry (e.g. a future check type).
GENERIC_FIX = (
    "Review this finding against the SEO issue playbook and apply the "
    "corresponding remedy; no specific fix is mapped for this type yet."
)


def fix_for(finding_type: str) -> str:
    """Return the recommended fix text for a finding type (generic fallback)."""
    return FIX_RECOMMENDATIONS.get(finding_type, GENERIC_FIX)


def attach_fixes(findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Set a ``fix`` field on each finding from the mapping, in place.

    Args:
        findings: The list of finding dicts.

    Returns:
        The same list (mutated), for chaining.
    """
    for finding in findings:
        finding["fix"] = fix_for(finding.get("type", ""))
    return findings
