# Halifax Grocery Deals

Personal weekly grocery deal checker for Halifax, NS. Every Thursday morning a GitHub
Action fetches all ~17 Halifax-area flyers (Sobeys, Atlantic Superstore, Costco, Walmart,
No Frills, Giant Tiger, Wholesale Club, Freshmart, NSLC, Shoppers, Lawtons, Canadian
Tire, Dollarama…) from Flipp's app backend, matches them against a personal staples
watchlist, sanity-checks every "sale" against Walmart's everyday online price, tags a
health tier (NOVA rules + Open Food Facts), and publishes a report to GitHub Pages.

**Report:** `docs/index.html` → GitHub Pages.

## Tuning

- `watchlist.txt` — the staples to track. `name | weight | match terms | exclude terms | category`.
  Weight ranks importance (mined from purchase frequency). Edit from your phone on github.com.
- `data/history.json` — accumulating price history; enables "lowest in N weeks" badges
  after a few weeks of runs.
- `health_cache.json` — cached Open Food Facts lookups.

## Run locally

```
python3 fetch.py && python3 report.py && open docs/index.html
```

No API keys. Be polite: the fetcher rate-limits itself and runs once a week.
