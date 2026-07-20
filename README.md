# RFP Screener — self-sufficient edition (v3)

A daily, unattended pipeline: **collect → score → publish → alert**, configured
entirely by JSON. The system finds and fit-scores opportunities; bid decisions
stay human (there is deliberately no go/no-go anywhere).

## Files
| File | Role |
|---|---|
| `sources.json` | The runbook as config: Tier-1 sources & protocols, clusters, keywords, rules, GIZ country loop. **Edit this to change the sweep.** |
| `scoring.json` | The fit model: 4 dimensions, weights (35/25/20/20), eligibility flag rules, calibration anchors. **Edit this to re-tune scoring.** |
| `scraper.py` | Collector v3 — reads `sources.json`; every source isolated; RSS auto-discovery handles monthly page rotations. |
| `score_new.py` | Scoring brain — Anthropic API applies the rubric at temperature 0; arithmetic computed in code; failures flagged, never guessed. |
| `.github/workflows/sweep.yml` | Daily 05:00 UTC run + manual button + acceptance-test toggle. |
| `data/opportunities.json` | The backend your Base44 app reads. Schema: items with 4 dimension scores + rationales, weighted fit score (0–100), eligibility flag, deadlines. Legacy hand-scores preserved under `legacy`. |
| `data/backend_seed.csv`, `issuers.json`, `sweeplog.json` | Entity seeds / directory / history for the app. |

## Setup (~30 minutes, once)
1. Create a **public** repo. Upload: `scraper.py`, `score_new.py`, `requirements.txt`,
   `sources.json`, `scoring.json`, `README.md`, and the `data/` files
   (`opportunities.json`, `issuers.json`, `sweeplog.json`, `backend_seed.csv`).
2. Add file → Create new file → path `.github/workflows/sweep.yml` → paste `sweep.yml`.
3. Settings → Secrets and variables → Actions → **New repository secret**:
   - `ANTHROPIC_API_KEY` (from console.anthropic.com → API keys)
   - later, when UNGM approves: `UNGM_CLIENT_ID`, `UNGM_CLIENT_SECRET` (the UNGM
     module switches itself on the moment they exist).
4. Actions tab → enable → open "Daily sweep + scoring" → **Run workflow** (shakedown).
   Paste any red log lines into Claude for a one-pass patch.
5. Watch the repo (Watch → Custom → Issues) so finds and health alerts email you.

## Acceptance test (before trusting the scorer)
Run the workflow manually with **"Also re-score legacy items" = true** a few times
(25 items per run) until all 84 historical items carry new scores. Then hand the
resulting `opportunities.json` to Claude to compare new-model ranking against your
legacy hand scores and tune `scoring.json`'s anchors until the ordering agrees.
That comparison is the go-live gate.

## Base44 wiring (read-only, Builder plan or higher)
Create one backend function in your Base44 app:

```ts
Deno.serve(async () => {
  const r = await fetch(
    "https://raw.githubusercontent.com/YOUR-USER/YOUR-REPO/main/data/opportunities.json");
  return Response.json(await r.json());
});
```

Then tell Base44's chat: *"Fetch the pipeline via this function. Each item has
title, issuer, country, deadline, link, eligible (Y/N/?), score (0–100), and
dims with per-dimension rationales. Show days-to-deadline computed client-side;
treat past-deadline as closed; sort by deadline; no recommendation field exists —
display fit score and let the user judge."*

## Self-healing behaviour
- New finds → GitHub issue → email. No finds → no email.
- A source failing **twice consecutively** → "Source health alert" issue naming it.
- A page that changed but parsed zero items → PARSER ALERT item, never silence.
- GIZ country tabs vanishing = "no active tenders", by design — never a failure.
- Daily bot commits keep the scheduled workflow alive past GitHub's inactivity pause.

## Honest limitations
- First live run is the real test of the newer parsers (Impact Funding, Terra Viva,
  Coefficient, AERC, GIZ URL pattern, EU payload, UNGM endpoints) — built defensively,
  not network-tested from the build sandbox. Expect a patch cycle in week one.
- Impact Funding's ~250 sector-page items are paid-subscriber-only; the scraper reads
  the free roundup, your email subscription covers the rest (15-min monthly cross-check).
- fundsforNGOs is feed-based here; if its gate ever blocks feeds from Actions IPs,
  the run continues and you get a health alert.
- Scoring costs: ~25 items/run on a small model budget — cents per day.
