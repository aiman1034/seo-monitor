"""Submit sitemaps to Google Search Console for the LIVE front-end domains.

Runs headless in GitHub Actions (the service-account key lives in the
GSC_SERVICE_ACCOUNT_JSON secret). Uses the WRITE scope
(``https://www.googleapis.com/auth/webmasters``) — the monitor's gsc.py is
read-only, so sitemap submission can't reuse it.

The service account must be a **Full** user on each GSC property (Restricted
users can't submit). If you see a 403, upgrade the SA to Full in GSC →
Settings → Users and permissions.
"""
import os
import sys
import json

import yaml
from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/webmasters"]  # WRITE (not readonly)

# The currently-live public front-ends (the ones whose sitemaps we submit).
# .dog/.com are redirected away, so we don't submit their sitemaps.
LIVE_DOMAINS = ["totalsportek.cat", "totalsporteks.tech"]


def make_client():
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    path = os.environ.get("GSC_SERVICE_ACCOUNT_FILE")
    if raw:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(raw), scopes=SCOPES
        )
    elif path:
        creds = service_account.Credentials.from_service_account_file(
            path, scopes=SCOPES
        )
    else:
        print("ERROR: no GSC credentials (set GSC_SERVICE_ACCOUNT_JSON/FILE)")
        sys.exit(1)
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def main():
    cfg = yaml.safe_load(open("config.yaml"))
    props = {s["domain"]: s.get("gsc_property", f"sc-domain:{s['domain']}")
             for s in cfg.get("sites", [])}
    svc = make_client()
    failures = 0
    for dom in LIVE_DOMAINS:
        prop = props.get(dom, f"sc-domain:{dom}")
        feed = f"https://{dom}/sitemap.xml"
        try:
            svc.sitemaps().submit(siteUrl=prop, feedpath=feed).execute()
            print(f"SUBMITTED  {dom}  ->  {feed}   (property {prop})")
        except Exception as e:
            failures += 1
            print(f"FAILED     {dom}  ->  {feed}   ({type(e).__name__}: {e})")
            continue
        # verify — read back the sitemap's processing state
        try:
            r = svc.sitemaps().get(siteUrl=prop, feedpath=feed).execute()
            print(f"   verify: lastSubmitted={r.get('lastSubmitted')} "
                  f"isPending={r.get('isPending')} isSitemapsIndex={r.get('isSitemapsIndex')} "
                  f"warnings={r.get('warnings')} errors={r.get('errors')} "
                  f"contents={r.get('contents')}")
        except Exception as e:
            print(f"   verify get failed: {type(e).__name__}: {e}")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
