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


# ---------------------------------------------------------------- html

CSS = """
:root{--bg:#faf9f6;--card:#fff;--ink:#1a1a1a;--sub:#666;--line:#e8e5df;--accent:#1a7a4a;
--warn:#b45309;--bad:#b91c1c;font-size:16px}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--card:#1e1e1e;--ink:#eee;--sub:#999;
--line:#2e2e2e;--accent:#4ade80;--warn:#fbbf24;--bad:#f87171}}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,system-ui,sans-serif;
max-width:680px;margin:0 auto;padding:16px 14px 60px}
h1{font-size:1.35rem;margin:8px 0 2px}
.sub{color:var(--sub);font-size:.85rem;margin-bottom:18px}
h2{font-size:1.05rem;margin:26px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.verdict{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin:14px 0}
.verdict .go{font-size:1.15rem;font-weight:700;color:var(--accent)}
.bar{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:.9rem}
.bar .fill{height:8px;border-radius:4px;background:var(--accent);opacity:.75}
.bar .store{min-width:9.5em}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}
.card .top{display:flex;justify-content:space-between;gap:8px;align-items:baseline}
.card .item{font-weight:650}
.card .price{font-weight:700;white-space:nowrap}
.card .meta{color:var(--sub);font-size:.83rem;margin-top:2px}
.badge{display:inline-block;font-size:.74rem;padding:1px 8px;border-radius:99px;
border:1px solid var(--line);margin-right:4px;margin-top:6px;color:var(--sub)}
.badge.good{color:var(--accent);border-color:var(--accent)}
.badge.warn{color:var(--warn);border-color:var(--warn)}
.badge.bad{color:var(--bad);border-color:var(--bad)}
details{margin:8px 0}summary{cursor:pointer;color:var(--sub);font-size:.9rem}
.alt{color:var(--sub);font-size:.83rem;margin-top:4px}
footer{margin-top:36px;color:var(--sub);font-size:.78rem}
"""


def esc(s):
    return html.escape(str(s))


def render(today, snap, deals, ranking, reasons, n_items, weeks):
    top = ranking[0] if ranking else None
    maxs = ranking[0][1] if ranking else 1
    H = [f'<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
         f"<title>Halifax Grocery Deals — {today}</title><style>{CSS}</style>",
         f"<h1>🛒 Halifax grocery deals</h1>",
         f'<div class="sub">Week of {today} · {len(snap["flyers"])} flyers · '
         f"{n_items:,} prices checked · {len(weeks)} week(s) of history</div>"]

    if top:
        H.append('<div class="verdict">')
        H.append(f'<div class="go">This week: go to {esc(top[0])}</div>')
        H.append(f'<div class="meta" style="color:var(--sub);font-size:.85rem">'
                 f'Best on: {esc(", ".join(reasons[top[0]][:8]))}</div>')
        for store, sc in ranking[:6]:
            pct = int(sc / maxs * 100)
            H.append(f'<div class="bar"><span class="store">{esc(store)}</span>'
                     f'<span class="fill" style="width:{pct * 0.55}%"></span>'
                     f'<span style="color:var(--sub)">{len(reasons[store])} staple deal(s)</span></div>')
        H.append("</div>")

    good = [d for d in deals if not d["beaten"]]
    traps = [d for d in deals if d["beaten"]]
    good.sort(key=lambda d: (-bool(d["low"]), -bool(d["beats"]), -d["w"]["weight"]))

    H.append(f"<h2>Your staples on sale ({len(good)})</h2>")
    for d in good:
        H.append(card(d))

    if traps:
        H.append(f'<h2>Looks like a sale — isn’t ({len(traps)})</h2>')
        for d in traps:
            H.append(card(d))

    # everything else, by store, collapsed
    H.append("<h2>All flyers</h2>")
    for f in sorted(snap["flyers"], key=lambda f: f["merchant"]):
        H.append(f'<details><summary>{esc(f["merchant"])} '
                 f'({esc(f["valid_from"][:10])} → {esc(f["valid_to"][:10])})</summary>'
                 f'<div class="alt">Open the Flipp app or flipp.com for page images.</div></details>')

    H.append(f"<footer>Data: Flipp (postal {esc(snap['postal_code'])}) · Walmart everyday prices from walmart.ca "
             f"via Flipp · health tiers: NOVA rules + Open Food Facts · "
             f"generated {esc(today)} · edit <code>watchlist.txt</code> to tune matching.</footer>")
    (ROOT / "docs").mkdir(exist_ok=True)
    (ROOT / "docs" / "index.html").write_text("\n".join(H))
    print(f"report: docs/index.html — {len(good)} staple deals, {len(traps)} traps, "
          f"top store: {top[0] if top else 'n/a'}")


def card(d):
    b, w = d["best"], d["w"]
    up = f' · {b["up"][2]}' if b["up"] else ""
    unit_note = esc((b.get("post") or "").strip())
    unit_note = f"/{unit_note.lstrip('/')}" if unit_note else ""
    badges = []
    e, label = BADGE[d["health"]["tier"]]
    if label:
        cls = {"real": "good", "ok": "warn", "ultra": "bad"}[d["health"]["tier"]]
        badges.append(f'<span class="badge {cls}">{e} {label}</span>')
    if d["low"]:
        badges.append(f'<span class="badge good">📉 {esc(d["low"])}</span>')
    if d["beats"]:
        a = d["beats"]
        badges.append(f'<span class="badge good">beats Walmart everyday '
                      f'(${a["p"]:.2f} {esc(a["n"][:28])})</span>')
    if d["beaten"]:
        a = d["beaten"]
        badges.append(f'<span class="badge bad">Walmart everyday is cheaper: '
                      f'${a["p"]:.2f} {esc(a["n"][:34])}</span>')
    if b["story"]:
        badges.append(f'<span class="badge">{esc(b["story"][:44])}</span>')
    if re.search(r"\bor\b|,", b["n"], re.I) and len(b["n"]) > 40:
        badges.append('<span class="badge">multi-item listing — price may apply to a group</span>')
    alts = ""
    if d["alts"]:
        rows = " · ".join(f'{esc(a["m"])} ${a["p"]:.2f}' + (f' ({a["up"][2]})' if a["up"] else "")
                          for a in d["alts"])
        alts = f'<div class="alt">also: {rows}</div>'
    return (f'<div class="card"><div class="top"><span class="item">{esc(w["name"])}</span>'
            f'<span class="price">${b["p"]:.2f}{unit_note}</span></div>'
            f'<div class="meta">{esc(b["n"][:70])} — <b>{esc(b["m"])}</b>{up}</div>'
            f'{"".join(badges)}{alts}</div>')


if __name__ == "__main__":
    main()
