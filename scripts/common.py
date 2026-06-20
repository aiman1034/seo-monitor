"""Shared helpers for the SEO monitoring scripts.

Centralises config loading, logging setup, URL helpers and timestamp helpers so
the individual check scripts (monitor_response, compare_duplicates, analyze,
report) stay small and consistent. Designed to be imported both as a module and
when scripts are run directly from the repo root.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List

import yaml

# Repo root is the parent of the directory containing this file (scripts/).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(REPO_ROOT, "config.yaml")

# Where run results (JSON/markdown) are read from and written to. Overridable via
# SEO_MONITOR_DATA_DIR so GitHub Actions can point it at a `data`-branch worktree,
# keeping results off `main`. Defaults to ``<repo>/data`` for local runs.
DATA_DIR = os.environ.get(
    "SEO_MONITOR_DATA_DIR", os.path.join(REPO_ROOT, "data")
)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """Load and lightly validate the YAML config.

    Args:
        path: Path to the YAML config file.

    Returns:
        The parsed configuration as a dictionary.

    Raises:
        FileNotFoundError: If the config file does not exist.
        ValueError: If required top-level keys are missing.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path, "r", encoding="utf-8") as fh:
        config: Dict[str, Any] = yaml.safe_load(fh) or {}

    required = ["sites", "user_agents", "thresholds", "request"]
    missing = [key for key in required if key not in config]
    if missing:
        raise ValueError(f"Config is missing required keys: {', '.join(missing)}")

    return config


def setup_logging(verbose: bool = True) -> logging.Logger:
    """Configure root logging to stdout and return a module logger.

    Args:
        verbose: If True use DEBUG level, otherwise INFO.

    Returns:
        A configured logger named ``seo-monitor``.
    """
    # On Windows the console defaults to a legacy code page; force UTF-8 so
    # report text (em-dashes, %, etc.) never triggers a UnicodeEncodeError.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    return logging.getLogger("seo-monitor")


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now_compact() -> str:
    """Return current UTC time as a filename-safe stamp, e.g. 20260620T143000Z."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_url(domain: str, path: str, scheme: str = "https") -> str:
    """Join a domain and path into a full URL.

    Args:
        domain: Bare domain, e.g. ``example.com``.
        path: Path beginning with ``/``.
        scheme: URL scheme, defaults to ``https``.

    Returns:
        A full URL string.
    """
    path = path if path.startswith("/") else f"/{path}"
    return f"{scheme}://{domain}{path}"


def iter_site_paths(config: Dict[str, Any]) -> List[Dict[str, str]]:
    """Flatten the config sites into a list of {domain, path, url} dicts.

    Args:
        config: The loaded configuration.

    Returns:
        One entry per (site, path) pair.
    """
    out: List[Dict[str, str]] = []
    for site in config.get("sites", []):
        domain = site["domain"]
        for path in site.get("paths", ["/"]):
            out.append({"domain": domain, "path": path, "url": build_url(domain, path)})
    return out


def ensure_data_dir() -> str:
    """Ensure the local data directory exists and return its path."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR
