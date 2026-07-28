#!/usr/bin/env python3
"""Turn data/latest.json into docs/index.html — the weekly Halifax deals report.

Pipeline: dedupe flyer items → match watchlist (include/exclude terms) →
unit prices → Walmart everyday anchor → price history badges → health tier
(rules first, Open Food Facts cache second) → per-store verdict → HTML.
"""
import html
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

ROOT = Path(__file__).parent
UA = "HalifaxFlyerBot/1.0 (personal use; +github.com/gibranramia-rgb/halifax-grocery-deals)"
OFF_BUDGET = 15  # max new Open Food Facts lookups per run (results are cached forever)

GROCERS = {"Sobeys", "Atlantic Superstore", "Costco", "Walmart", "No Frills",
           "Giant Tiger", "Wholesale Club and Club Entrepôt", "Freshmart",
           "M&M Food Market", "Proxi"}

# ---------------------------------------------------------------- watchlist

def load_watchlist():
    rows = []
    for line in (ROOT / "watchlist.txt").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, weight, match, excl, cat = [f.strip() for f in line.split("|")]
        rows.append({
            "name": name, "weight": int(weight), "cat": cat,
            "match": [t.strip().lower() for t in match.split(",") if t.strip()],
            "excl": [t.strip().lower() for t in excl.split(",") if t.strip()],
        })
    return rows


def matches(entry, product_name):
    n = product_name.lower()
    return any(t in n for t in entry["match"]) and not any(t in n for t in entry["excl"])

# ---------------------------------------------------------------- unit price

UNIT_G = {"kg": 1000, "g": 1, "lb": 453.6, "lbs": 453.6, "oz": 28.35}
UNIT_ML = {"l": 1000, "ml": 1}
SIZE_RE = re.compile(r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(kg|g|lbs|lb|oz|ml|l)\b", re.I)
COUNT_RE = re.compile(r"(\d+)\s*(?:=\s*\d+\s*)?(?:-?\s*)(rolls?|count|ct|pack|pk|pods?|pairs?|sheets?)\b", re.I)


def unit_price(name, price, post_text=""):
    """Return (domain, per_unit, label) — per 100 g / 100 ml / per count-unit."""
    if price is None:
        return None
    post = (post_text or "").lower().replace("/", "").strip()
    if post in ("lb", "lbs"):
        return ("g", price / 453.6 * 100, f"${price / 453.6 * 100:.2f}/100g")
    if post == "kg":
        return ("g", price / 10, f"${price / 10:.2f}/100g")
    if post == "100g":
        return ("g", price, f"${price:.2f}/100g")
    m = SIZE_RE.search(name)
    if m:
        mult = int(m.group(1)) if m.group(1) else 1
        qty = float(m.group(2).replace(",", "."))
        unit = m.group(3).lower()
        if unit in UNIT_G:
            g = mult * qty * UNIT_G[unit]
            if g > 0:
                return ("g", price / g * 100, f"${price / g * 100:.2f}/100g")
        if unit in UNIT_ML:
            ml = mult * qty * UNIT_ML[unit]
            if ml > 0:
                return ("ml", price / ml * 100, f"${price / ml * 100:.2f}/100ml")
    m = COUNT_RE.search(name)
    if m:
        n = int(m.group(1))
        if n > 0:
            label = m.group(2).lower().rstrip("s")
            return ("unit", price / n, f"${price / n:.2f}/{label}")
    return None

# ---------------------------------------------------------------- health

REAL = ["egg", "milk", "cream", "butter", "potato", "onion", "garlic", "banana",
        "grape", "strawberr", "blueberr", "raspberr", "blackberr", "apple", "orange",
        "tomato", "lettuce", "carrot", "broccoli", "pepper", "avocado", "honey",
        "rice", "oats", "oatmeal", "flour", "yogurt", "yoghurt", "cheese",
        "chicken breast", "chicken thigh", "chicken drumstick", "whole chicken",
        "chicken leg", "ground beef", "steak", "pork loin", "pork chop", "salmon",
        "trout", "haddock", "shrimp", "mussel", "clam", "beans", "lentil",
        "chickpea", "quinoa", "broth", "stock", "peanut butter", "olive oil", "tofu"]
UPF = ["breaded", "nugget", "bites", "strips", "burger", "hot dog", "wiener",
       "bologna", "deli", "pop", "cola", "soda", "candy", "gummy", "chips",
       "crisps", "snack", "instant", "ramen", "noodle", "cookie", "cake", "donut",
       "muffin", "ice cream", "dessert", "drink", "punch", "cocktail",
       "kraft dinner", "mac & cheese", "mac and cheese", "pizza", "lasagna",
       "entree", "entrée", "dinner", "marshmallow", "chocolate", "wafer",
       "cereal bar", "granola bar", "slush", "frozen yogurt", "pudding"]
PROCESSED = ["bacon", "ham", "sausage", "bread", "cereal", "granola", "cracker",
             "jam", "jelly", "sauce", "dressing", "mayonnaise", "mayo", "ketchup",
             "canned", "juice", "pasta", "tortilla", "bagel", "bun"]


def health_rules(name):
    n = name.lower()
    if any(k in n for k in UPF):
        return "ultra"
    if any(k in n for k in REAL):
        return "real"
    if any(k in n for k in PROCESSED):
        return "ok"
    return None


class HealthDB:
    def __init__(self):
        self.path = ROOT / "health_cache.json"
        self.cache = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.budget = OFF_BUDGET

    def key(self, name):
        return re.sub(r"\s+", " ", name.lower().strip())[:80]

    def lookup(self, name, cat):
        if cat != "food":
            return {"tier": "na"}
        rule = health_rules(name)
        if rule:
            return {"tier": rule}
        k = self.key(name)
        if k in self.cache:
            return self.cache[k]
        if self.budget <= 0:
            return {"tier": "unknown"}
        self.budget -= 1
        result = {"tier": "unknown"}
        try:
            url = ("https://world.openfoodfacts.org/cgi/search.pl?search_simple=1"
                   "&action=process&json=1&page_size=1"
                   "&fields=product_name,nutriscore_grade,nova_group"
                   "&search_terms=" + urllib.parse.quote(name[:60]))
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                d = json.loads(r.read())
            time.sleep(1.2)
            if d.get("products"):
                p = d["products"][0]
                nova = p.get("nova_group")
                tier = {1: "real", 2: "real", 3: "ok", 4: "ultra"}.get(nova, "unknown")
                result = {"tier": tier, "nutri": p.get("nutriscore_grade"), "nova": nova}
        except Exception:
            pass
        self.cache[k] = result
        return result

    def save(self):
        self.path.write_text(json.dumps(self.cache))


BADGE = {"real": ("🟢", "real food"), "ok": ("🟡", "processed"),
         "ultra": ("🟠", "ultra-processed"), "unknown": ("", ""), "na": ("", "")}

# ---------------------------------------------------------------- main

def main():
    snap = json.loads((ROOT / "data" / "latest.json").read_text())
    history = json.loads((ROOT / "data" / "history.json").read_text())
    watch = load_watchlist()
    hdb = HealthDB()
    today = snap["run_date"]
    weeks = sorted({r["w"] for r in history})

    # per-lb / member-pricing hints from the search endpoint
    hints = {}
    for i in snap.get("search_flyer_items", []):
        k = (i.get("merchant_name"), (i.get("name") or "").lower())
        hints[k] = {"post": i.get("post_price_text") or "", "pre": i.get("pre_price_text") or "",
                    "story": i.get("sale_story") or ""}

    # dedupe flyer items
    items, seen = [], set()
    for it in snap["flyer_items"]:
        name = (it.get("name") or "").strip()
        if not name:
            continue
        try:
            price = float(it.get("price")) if it.get("price") else None
        except ValueError:
            price = None
        key = (it["_merchant"], name.lower())
        if key in seen:
            continue
        seen.add(key)
        h = hints.get(key, {})
        post = (h.get("post") or "").strip()
        # only keep sane unit suffixes; financing text like "$10.42/24 mo" gets dropped
        if post.lstrip("/").lower() not in ("lb", "lbs", "kg", "ea", "each", "100g", "100 g"):
            post = ""
        items.append({"m": it["_merchant"], "n": name, "p": price,
                      "post": post, "pre": h.get("pre", ""),
                      "story": h.get("story", "")})

    # history lookback per (merchant, name)
    hist_by_key = {}
    for r in history:
        hist_by_key.setdefault((r["m"], r["n"].lower()), []).append(r)

    def history_badge(m, n, p):
        rows = hist_by_key.get((m, n.lower()), [])
        wks = {r["w"] for r in rows}
        if len(wks) < 2 or p is None:
            return None
        prior = [r["p"] for r in rows if r["w"] != today]
        if prior and p <= min(prior):
            return f"lowest in {len(weeks)} wks of tracking"
        return None

    # match watchlist
    deals = []
    for w in watch:
        cands = []
        for it in items:
            if it["p"] is None or not matches(w, it["n"]):
                continue
            up = unit_price(it["n"], it["p"], it["post"])
            cands.append({**it, "up": up})
        # Walmart everyday anchor, same include/exclude filters
        anchors = []
        for row in snap["ecom"].get(w["name"], []):
            if row["merchant"] == "Walmart" and row["name"] and matches(w, row["name"]):
                up = unit_price(row["name"], row["price"])
                anchors.append({"n": row["name"], "p": row["price"], "up": up})
        best_anchor = None
        with_units = [a for a in anchors if a["up"]]
        if with_units:
            dom = max({a["up"][0] for a in with_units},
                      key=lambda d: sum(1 for a in with_units if a["up"][0] == d))
            same = [a for a in with_units if a["up"][0] == dom]
            best_anchor = min(same, key=lambda a: a["up"][1])
        elif anchors:
            best_anchor = min(anchors, key=lambda a: a["p"])
        if not cands:
            continue
        # rank candidates: unit price within dominant domain, else absolute
        cu = [c for c in cands if c["up"]]
        if cu:
            dom = max({c["up"][0] for c in cu}, key=lambda d: sum(1 for c in cu if c["up"][0] == d))
            ranked = sorted([c for c in cu if c["up"][0] == dom], key=lambda c: c["up"][1])
            ranked += sorted([c for c in cands if c not in ranked], key=lambda c: c["p"])
        else:
            ranked = sorted(cands, key=lambda c: c["p"])
        # don't headline restaurant-size bulk (a $78 16L mayo pail can win on unit
        # price) — demote anything costing >3x the median candidate to the alts
        med = sorted(c["p"] for c in cands)[len(cands) // 2]
        sane = [c for c in ranked if c["p"] <= max(3 * med, 12)]
        if sane:
            ranked = sane + [c for c in ranked if c not in sane]
        best = ranked[0]
        beats = beaten = None
        if best_anchor and best["up"] and best_anchor["up"] and best["up"][0] == best_anchor["up"][0]:
            if best["up"][1] <= best_anchor["up"][1]:
                beats = best_anchor
            else:
                beaten = best_anchor
        health = hdb.lookup(best["n"], w["cat"])
        deals.append({"w": w, "best": best, "alts": ranked[1:4], "anchor": best_anchor,
                      "beats": beats, "beaten": beaten, "health": health,
                      "low": history_badge(best["m"], best["n"], best["p"])})
    hdb.save()

    # store scores (grocery stores only, weighted by how often you buy the item)
    scores, reasons = {}, {}
    for d in deals:
        m = d["best"]["m"]
        if m not in GROCERS:
            continue
        q = 1.0 + (1.0 if d["beats"] else 0) + (1.0 if d["low"] else 0) - (1.5 if d["beaten"] else 0)
        scores[m] = scores.get(m, 0) + d["w"]["weight"] * max(q, 0.25)
        reasons.setdefault(m, []).append(d["w"]["name"])
    ranking = sorted(scores.items(), key=lambda kv: -kv[1])

    render(today, snap, deals, ranking, reasons, len(items), weeks)


# ---------------------------------------------------------------- html (template.html + window.DATA)

MYLIST = ROOT / "data" / "mylist.txt"


def on_my_list(w):
    """Is this watchlist entry on the committed snapshot of the live Reminders list?"""
    if not MYLIST.exists():
        return False
    for line in MYLIST.read_text().splitlines():
        l = line.strip().lower()
        if not l:
            continue
        if any(t in l or l in t for t in [w["name"].lower()] + w["match"]):
            return True
    return False


def item_history(w, history, weeks):
    """Best matching price per week for this watchlist entry — the sparkline series."""
    series = []
    for wk in weeks:
        prices = [r["p"] for r in history
                  if r["w"] == wk and r["m"] != "walmart.ca" and matches(w, r["n"])]
        if prices:
            series.append(round(min(prices), 2))
    return series


def render(today, snap, deals, ranking, reasons, n_items, weeks):
    history = json.loads((ROOT / "data" / "history.json").read_text())
    maxs = ranking[0][1] if ranking else 1
    tier = {"real": "real", "ok": "ok", "ultra": "ultra", "na": "na", "unknown": "na"}

    data = {
        "week": today,
        "flyerCount": len(snap["flyers"]),
        "pricesChecked": n_items,
        "weeksOfHistory": len(weeks),
        "repo": "https://github.com/gibranramia-rgb/halifax-grocery-deals",
        "verdict": {
            "store": ranking[0][0] if ranking else "—",
            "ranking": [{"store": s, "dealCount": len(reasons[s]),
                         "score": round(sc / maxs * 100)} for s, sc in ranking[:8]],
        },
        "deals": [], "traps": [], "history": {},
    }

    good = [d for d in deals if not d["beaten"]]
    good.sort(key=lambda d: (-bool(d["low"]), -bool(d["beats"]), -d["w"]["weight"]))
    for d in good:
        b, w = d["best"], d["w"]
        badges = []
        if d["beats"]:
            badges.append(f'beats Walmart everyday (${d["beats"]["p"]:.2f} {d["beats"]["n"][:30]})')
        if d["low"]:
            badges.append(d["low"])
        if b["story"]:
            badges.append(b["story"][:48])
        data["deals"].append({
            "item": w["name"], "price": b["p"], "product": b["n"][:70], "store": b["m"],
            "unitPrice": b["up"][2] if b["up"] else "",
            "health": tier.get(d["health"]["tier"], "na"),
            "onMyList": on_my_list(w),
            "multiItem": bool(re.search(r"\bor\b|,", b["n"], re.I) and len(b["n"]) > 40),
            "badges": badges,
            "alts": [{"store": a["m"], "price": a["p"],
                      "unitPrice": a["up"][2] if a["up"] else ""} for a in d["alts"][:3]],
        })
        series = item_history(w, history, weeks)
        if len(series) >= 3:
            data["history"][w["name"]] = series

    for d in [x for x in deals if x["beaten"]]:
        b, a = d["best"], d["beaten"]
        data["traps"].append({
            "item": d["w"]["name"], "price": b["p"], "product": b["n"][:60], "store": b["m"],
            "reason": f'Walmart everyday is cheaper: ${a["p"]:.2f} {a["n"][:40]}',
        })

    tpl = (ROOT / "template.html").read_text()
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "index.html").write_text(
        tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    print(f"report: docs/index.html — {len(data['deals'])} staple deals, "
          f"{len(data['traps'])} traps, top store: {data['verdict']['store']}")


if __name__ == "__main__":
    main()
