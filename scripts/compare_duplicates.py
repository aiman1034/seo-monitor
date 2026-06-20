"""Cross-domain duplicate-content comparison.

For each configured pair of domains (e.g. totalsportek.tech vs totalsportek.bio)
this fetches the matching key paths from both, extracts visible text (stripping
script/style/nav/header/footer), and computes two similarity measures per page:

* ``difflib.SequenceMatcher`` ratio — character/sequence level.
* token-based **Jaccard** similarity on word shingles (n-grams) — robust to
  reordering and small edits.

It reports per-page similarity and an overall average, and flags
``DUPLICATE_CONTENT`` (critical) when the overall similarity exceeds the
configured threshold — the second hypothesis for the ranking volatility.

**Only page pairs where BOTH sides returned HTTP 200 are compared.** A 403/error
page still has body text, so comparing on text-presence alone would "compare"
two block pages and report a meaningless similarity. When either side is non-200
or errored, the page is recorded with its statuses but its similarity fields are
null, it is excluded from the overall average and from any DUPLICATE_CONTENT /
DUPLICATE_PAGE finding, and a ``DUPLICATE_CHECK_SKIPPED`` (info) finding is
emitted explaining why. A page that fails to fetch is recorded, never raised.

Run directly to print JSON:

    python scripts/compare_duplicates.py
"""

from __future__ import annotations

import json
import re
import time
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from bs4 import BeautifulSoup

try:
    from .common import build_url, load_config, path_str, setup_logging, utc_now_iso
except ImportError:  # allow running as a plain script
    from common import build_url, load_config, path_str, setup_logging, utc_now_iso  # type: ignore

# Tags whose text is boilerplate/navigation rather than page content.
_STRIP_TAGS = ["script", "style", "noscript", "nav", "header", "footer", "svg"]
_SHINGLE_SIZE = 3  # word n-gram size for Jaccard


def extract_visible_text(html: bytes) -> str:
    """Extract normalised visible text from an HTML document.

    Strips script/style/nav/header/footer, collapses whitespace and lowercases.

    Args:
        html: Raw HTML bytes.

    Returns:
        A single normalised text string (may be empty).
    """
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(_STRIP_TAGS):
        tag.decompose()
    text = soup.get_text(separator=" ")
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _shingles(text: str, size: int = _SHINGLE_SIZE) -> Set[str]:
    """Return the set of word n-gram shingles for a text.

    Args:
        text: Normalised text.
        size: Number of words per shingle.

    Returns:
        A set of shingle strings (empty if the text is too short).
    """
    words = text.split()
    if len(words) < size:
        return {text} if text else set()
    return {" ".join(words[i : i + size]) for i in range(len(words) - size + 1)}


def jaccard_similarity(text_a: str, text_b: str, size: int = _SHINGLE_SIZE) -> float:
    """Compute token-based Jaccard similarity on word shingles.

    Args:
        text_a: First text.
        text_b: Second text.
        size: Shingle size.

    Returns:
        Jaccard similarity in [0, 1]; 0 if either text is empty.
    """
    sa, sb = _shingles(text_a, size), _shingles(text_b, size)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def sequence_similarity(text_a: str, text_b: str) -> float:
    """Compute difflib SequenceMatcher ratio in [0, 1]."""
    if not text_a or not text_b:
        return 0.0
    return SequenceMatcher(None, text_a, text_b).ratio()


def _fetch_text(
    url: str, user_agent: str, timeout: float, verify_ssl: bool
) -> Tuple[Optional[str], Optional[int], Optional[str]]:
    """Fetch a URL and return (visible_text, status_code, error).

    Network errors are returned as the error string, never raised.
    """
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            verify=verify_ssl,
        )
        return extract_visible_text(resp.content), resp.status_code, None
    except requests.exceptions.RequestException as exc:
        return None, None, f"{type(exc).__name__}: {exc}"


def _common_paths(config: Dict[str, Any], domain_a: str, domain_b: str) -> List[str]:
    """Return paths present in both domains' config (preserving a's order)."""
    sites = {
        s["domain"]: [path_str(e) for e in s.get("paths", ["/"])]
        for s in config.get("sites", [])
    }
    paths_a = sites.get(domain_a, [])
    paths_b = set(sites.get(domain_b, []))
    return [p for p in paths_a if p in paths_b]


def run(config: Dict[str, Any], logger=None) -> Dict[str, Any]:
    """Compute duplicate-content similarity for each configured domain pair.

    Args:
        config: The loaded configuration.
        logger: Optional logger.

    Returns:
        A dict with ``timestamp_utc``, ``pairs`` (per-pair page-level results +
        overall average) and ``findings`` (DUPLICATE_CONTENT).
    """
    log = logger or setup_logging()
    req_cfg = config["request"]
    timeout = float(req_cfg.get("timeout_seconds", 20))
    delay = float(req_cfg.get("delay_seconds", 1.5))
    verify_ssl = bool(req_cfg.get("verify_ssl", True))
    # Use the real browser UA for content comparison (what a user/Google sees).
    ua = config["user_agents"].get(config.get("browser_user_agent", "browser"))
    threshold = float(config["thresholds"].get("duplicate_similarity", 0.8))
    page_threshold = float(config["thresholds"].get("duplicate_page_similarity", 0.9))

    pairs_cfg = config.get("duplicate_compare", {}).get("pairs", [])
    now = utc_now_iso()
    pair_results: List[Dict[str, Any]] = []
    findings: List[Dict[str, Any]] = []

    for pair in pairs_cfg:
        domain_a, domain_b = pair[0], pair[1]
        paths = _common_paths(config, domain_a, domain_b)
        log.info("compare_duplicates: %s vs %s on %d paths", domain_a, domain_b, len(paths))

        pages: List[Dict[str, Any]] = []
        scored: List[float] = []

        for path in paths:
            url_a = build_url(domain_a, path)
            url_b = build_url(domain_b, path)
            text_a, status_a, err_a = _fetch_text(url_a, ua, timeout, verify_ssl)
            time.sleep(delay)
            text_b, status_b, err_b = _fetch_text(url_b, ua, timeout, verify_ssl)
            time.sleep(delay)

            page: Dict[str, Any] = {
                "path": path,
                "status_a": status_a,
                "status_b": status_b,
                "error_a": err_a,
                "error_b": err_b,
                "sequence_ratio": None,
                "jaccard": None,
                "combined": None,
            }

            # Only compare when BOTH sides returned HTTP 200. A 403/error page
            # still has body text, so comparing on text-presence alone would
            # "compare" two block pages and report a meaningless similarity.
            both_ok = (
                status_a == 200
                and status_b == 200
                and err_a is None
                and err_b is None
                and text_a is not None
                and text_b is not None
            )
            if both_ok:
                seq = round(sequence_similarity(text_a, text_b), 4)
                jac = round(jaccard_similarity(text_a, text_b), 4)
                combined = round((seq + jac) / 2, 4)
                page.update(sequence_ratio=seq, jaccard=jac, combined=combined)
                scored.append(combined)
                log.info(
                    "  %-8s seq=%.3f jaccard=%.3f combined=%.3f",
                    path,
                    seq,
                    jac,
                    combined,
                )
            else:
                # Cannot compare — record why so the report explains the missing
                # similarity number instead of silently showing a bogus one.
                desc_a = err_a if err_a is not None else status_a
                desc_b = err_b if err_b is not None else status_b
                log.info(
                    "  %-8s skipped (%s=%s, %s=%s — not both 200)",
                    path,
                    domain_a,
                    desc_a,
                    domain_b,
                    desc_b,
                )
                findings.append(
                    {
                        "type": "DUPLICATE_CHECK_SKIPPED",
                        "severity": "info",
                        "site": f"{domain_a} / {domain_b}",
                        "path": path,
                        "message": (
                            f"Skipped {path}: {domain_a}={desc_a}, "
                            f"{domain_b}={desc_b} — cannot compare non-200 pages."
                        ),
                        "details": {
                            "status_a": status_a,
                            "status_b": status_b,
                            "error_a": err_a,
                            "error_b": err_b,
                        },
                        "timestamp": now,
                    }
                )

            pages.append(page)

        overall = round(sum(scored) / len(scored), 4) if scored else None
        pair_results.append(
            {
                "domain_a": domain_a,
                "domain_b": domain_b,
                "pages": pages,
                "overall_similarity": overall,
                "pages_compared": len(scored),
            }
        )

        if overall is not None and overall >= threshold:
            findings.append(
                {
                    "type": "DUPLICATE_CONTENT",
                    "severity": "critical",
                    "site": f"{domain_a} / {domain_b}",
                    "message": (
                        f"{domain_a} and {domain_b} are {overall:.0%} similar across "
                        f"{len(scored)} page(s) (threshold {threshold:.0%}) — the "
                        f"domains may be cannibalising each other in search."
                    ),
                    "details": {
                        "overall_similarity": overall,
                        "threshold": threshold,
                        "pages_compared": len(scored),
                    },
                    "timestamp": now,
                }
            )

        # Per-page near-duplicates: a single page that is ~identical across the
        # pair is a cannibalisation signal even when the overall average is low
        # (e.g. boilerplate /dmca pages). Warning, not critical — it's a
        # persistent structural fact, so it belongs in the report/dashboard
        # rather than opening a perpetually-open alert Issue.
        dup_pages = [
            p
            for p in pages
            if isinstance(p.get("combined"), (int, float))
            and p["combined"] >= page_threshold
        ]
        for p in dup_pages:
            findings.append(
                {
                    "type": "DUPLICATE_PAGE",
                    "severity": "warning",
                    "site": f"{domain_a} / {domain_b}",
                    "path": p["path"],
                    "message": (
                        f"{p['path']} is {p['combined']:.0%} identical between "
                        f"{domain_a} and {domain_b} (threshold {page_threshold:.0%}) "
                        f"— likely duplicate content competing in search."
                    ),
                    "details": {
                        "combined": p["combined"],
                        "sequence_ratio": p.get("sequence_ratio"),
                        "jaccard": p.get("jaccard"),
                        "page_threshold": page_threshold,
                    },
                    "timestamp": now,
                }
            )

    return {
        "timestamp_utc": now,
        "pairs": pair_results,
        "findings": findings,
    }


def main() -> None:
    """CLI entry point: run comparison for configured pairs and print JSON."""
    logger = setup_logging()
    config = load_config()
    result = run(config, logger)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
