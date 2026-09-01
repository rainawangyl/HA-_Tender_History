# HA Tender — GitHub Auto Update

## Recommended architecture

HA website → GitHub Actions daily scraper → JSON database → GitHub Pages frontend.

The HTML is frontend-only and does not scrape HA directly.

## Files

- `index.html` — search interface
- `data/awards.json` — unique historical contract award lines
- `data/notices.json` — tender notice history
- `data/meta.json` — last checked / update counters
- `scripts/update_ha.py` — crawler + normalization + deduplication
- `.github/workflows/update-ha.yml` — daily GitHub Action
- `requirements.txt`

## Award deduplication

Unique key:

`Tender Reference + Award Date + canonical Contractor company name + Item`

If Item is blank / dash / N/A:

`Tender Reference + Award Date + canonical Contractor company name + Product / Tender Object`

Contractor address is NOT part of the key, so formatting/address changes do not create false duplicates.

## One-time setup

1. Upload all files/folders to the root of your `HA-Tender` repo.
2. GitHub repo → Settings → Pages → Build and deployment → Source → choose **GitHub Actions**.
3. Repo → Actions → **Daily HA Tender Update** → **Run workflow**.
4. Check the run log.
5. Your Pages site will deploy from the generated artifact.

## Schedule

`17 0 * * *` = 08:17 Hong Kong time every day.

The workflow also supports manual runs.

## Fail-safe

If HA is unreachable or the parser suddenly gets zero contract award rows, the action fails instead of replacing the existing database with an empty file.
