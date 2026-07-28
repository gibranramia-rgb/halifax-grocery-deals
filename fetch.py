#!/usr/bin/env python3
"""Fetch Halifax weekly flyer data from Flipp's app backend.

Writes:
  data/latest.json   — full snapshot for this run (flyers, items, ecom anchors)
  data/history.json  — compact price rows appended each week (the deal baseline)

Fails loudly on schema drift so a break is a traceback, not a silently wrong report.
"""
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # ubuntu runners have system CAs
    SSL_CTX = ssl.create_default_context()

ROOT = Path(__file__).parent
BASE = "https://backflipp.wishabi.com/flipp"
POSTAL = "B3H1A1"
UA = "HalifaxFlyerBot/1.0 (personal use; +github.com/gibranramia-rgb/halifax-grocery-deals)"

# Grocery-category flyers are taken automatically; these extras cover
# pharmacy staples, NSLC, and household/batteries.
EXTRA_MERCHANTS = {"Shoppers Drug Mart", "Lawtons Drugs", "NSLC", "Canadian Tire", "Dollarama"}
DELAY = 0.4  # politeness between requests


def get(path, **params):
    params.setdefault("locale", "en-ca")
    params.setdefault("postal_code", POSTAL)
    url = f"{BASE}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30, context=SSL_CTX) as r:
        return json.loads(r.read())


def watchlist_terms():
    """First match term of each watchlist line — used for the Walmart ecom anchor."""
    terms = []
    for line in (ROOT / "watchlist.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in line.split("|")]
        if len(fields) != 5:
            sys.exit(f"watchlist.txt: expected 5 fields, got {len(fields)}: {line!r}")
        terms.append((fields[0], fields[2].split(",")[0].strip()))
    return terms


def main():
    today = date.today().isoformat()

    # 1. Which flyers exist for this postal code right now
    listing = get("flyers")
    flyers = listing["flyers"]  # KeyError here = schema drift, fail loudly
    wanted = [
        f for f in flyers
        if "Groceries" in (f.get("categories") or []) or f["merchant"] in EXTRA_MERCHANTS
    ]
    print(f"{len(flyers)} flyers available; fetching {len(wanted)}:")

    # 2. Full item sweep per flyer (search alone misses items)
    flyer_items = []
    for f in wanted:
        time.sleep(DELAY)
        d = get(f"flyers/{f['id']}")
        items = d["items"]
        print(f"  {f['merchant']:<35} {len(items):>4} items  ({f['valid_from'][:10]} → {f['valid_to'][:10]})")
        for it in items:
            it["_merchant"] = f["merchant"]
            it["_flyer_id"] = f["id"]
        flyer_items.extend(items)

    # 3. Walmart everyday-online anchor + search enrichment per watchlist item
    ecom, search_flyer_items = {}, []
    for name, term in watchlist_terms():
        time.sleep(DELAY)
        try:
            d = get("items/search", q=term)
        except Exception as e:
            print(f"  search {term!r} failed: {e}", file=sys.stderr)
            continue
        ecom[name] = [
            {"merchant": i.get("merchant"), "name": i.get("name"),
             "price": i.get("current_price"), "original_price": i.get("original_price")}
            for i in d.get("ecom_items", []) if i.get("current_price")
        ]
        # search results carry pre/post price text (/lb etc.) the flyer endpoint lacks
        for i in d.get("items", []):
            i["_watch_hint"] = name
        search_flyer_items.extend(d.get("items", []))

    # 4. Snapshot
    snapshot = {
        "run_date": today,
        "postal_code": POSTAL,
        "flyers": [{k: f.get(k) for k in ("id", "merchant", "valid_from", "valid_to")} for f in wanted],
        "flyer_items": flyer_items,
        "search_flyer_items": search_flyer_items,
        "ecom": ecom,
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "latest.json").write_text(json.dumps(snapshot))
    print(f"snapshot: {len(flyer_items)} flyer items, {sum(len(v) for v in ecom.values())} ecom prices")

    # 5. Append compact rows to history (skip if this week already recorded)
    hist_path = ROOT / "data" / "history.json"
    history = json.loads(hist_path.read_text()) if hist_path.exists() else []
    if any(r["w"] == today for r in history):
        print("history: this run date already recorded, skipping append")
    else:
        seen = set()
        for it in flyer_items:
            price = it.get("price")
            name = (it.get("name") or "").strip()
            if not price or not name:
                continue
            try:
                p = float(price)
            except ValueError:
                continue
            key = (it["_merchant"], re.sub(r"\s+", " ", name.lower()))
            if key in seen:
                continue
            seen.add(key)
            history.append({"w": today, "m": it["_merchant"], "n": name, "p": p})
        # Walmart everyday prices are history too — they anchor "was that ever a deal"
        for wname, rows in ecom.items():
            for r in rows:
                if r["merchant"] == "Walmart":
                    key = ("walmart.ca", (r["name"] or "").lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    history.append({"w": today, "m": "walmart.ca", "n": r["name"], "p": r["price"]})
        hist_path.write_text(json.dumps(history))
        print(f"history: {len(history)} total rows across {len({r['w'] for r in history})} week(s)")


if __name__ == "__main__":
    main()
