"""Ad-hoc GSC traffic snapshot for the live totalsportek front-ends.

Dispatched manually (gsc-traffic.yml). Prints a readable 28-day + 7-day summary
per property: totals, top queries, top pages, top countries. Read-only scope.
"""
import json
import os
from datetime import date, timedelta

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]

# Live mirrors + the migration source (.dog -> .cat) to see the bleed-over.
PROPS = [
    ("totalsportek.cat", "sc-domain:totalsportek.cat"),
    ("totalsporteke.com", "sc-domain:totalsporteke.com"),
    ("totalsporteks.tech", "sc-domain:totalsporteks.tech"),
    ("totalsportek.dog", "sc-domain:totalsportek.dog"),
]


def make_service():
    raw = os.environ.get("GSC_SERVICE_ACCOUNT_JSON")
    path = os.environ.get("GSC_SERVICE_ACCOUNT_FILE")
    if raw:
        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=SCOPES)
    elif path:
        creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
    else:
        raise SystemExit("no GSC creds")
    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def totals(rows):
    c = sum(r.get("clicks", 0) for r in rows)
    i = sum(r.get("impressions", 0) for r in rows)
    w = sum(r.get("position", 0) * r.get("impressions", 0) for r in rows)
    pos = round(w / i, 1) if i else None
    ctr = round(100 * c / i, 2) if i else None
    return c, i, ctr, pos


def q(svc, prop, days, dims, limit=1000):
    end = date.today()
    start = end - timedelta(days=days)
    body = {"startDate": start.isoformat(), "endDate": end.isoformat(), "dimensions": dims, "rowLimit": limit}
    try:
        return svc.searchanalytics().query(siteUrl=prop, body=body).execute().get("rows", [])
    except Exception as e:
        print(f"    ERROR querying {prop} {dims}: {type(e).__name__}: {e}")
        return []


def main():
    svc = make_service()
    print("=== available properties (sites the SA can see) ===")
    try:
        for s in svc.sites().list().execute().get("siteEntry", []):
            print(f"  {s.get('siteUrl')}  [{s.get('permissionLevel')}]")
    except Exception as e:
        print("  sites.list error:", e)

    for name, prop in PROPS:
        print(f"\n########## {name}  ({prop}) ##########")
        for days in (28, 7):
            rows = q(svc, prop, days, ["date"])
            c, i, ctr, pos = totals(rows)
            print(f"  [{days}d] clicks={c}  impressions={i}  ctr={ctr}%  avg_pos={pos}")
        # 28-day top queries / pages / countries
        for dim, label in (["query"], "QUERIES"), (["page"], "PAGES"), (["country"], "COUNTRIES"):
            rows = q(svc, prop, 28, dim, 15)
            rows.sort(key=lambda r: r.get("clicks", 0), reverse=True)
            print(f"  --- top {label} (28d) ---")
            if not rows:
                print("      (none)")
            for r in rows[:12]:
                k = (r.get("keys") or ["?"])[0]
                print(f"      {r.get('clicks',0):>5} clk  {r.get('impressions',0):>7} imp  "
                      f"pos {round(r.get('position',0),1):>5}  {k}")


if __name__ == "__main__":
    main()
