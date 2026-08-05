# IFRC GO four-hazard DREF watchlist

`dref_analysis.py` creates a presentation-ready seasonal watchlist for Drought
(DR), Wildfire (WF), Flood (FL), and Tropical Cyclone (TC). Disease outbreaks
are handled separately by `disease_outbreak_analysis.py` because they are
current events rather than monthly INFORM/GWIS scores.

It uses the current IFRC GO sources:

- DR, FL, TC: `https://go-risk-api.ifrc.org/api/v1/risk-score/`
- WF: `https://go-risk-api.ifrc.org/api/v1/seasonal/` → `gwis_seasonal`
- Geographic groups: [UN Statistics Division M49](https://unstats.un.org/unsd/methodology/m49/overview/), stored in the versioned `un_m49_subregions.csv` lookup (retrieved 2026-08-05).

Wildfire is GWIS seasonal data, not an INFORM score. The script therefore does
not calculate an average, sum, or geometric mean across hazards. It creates a
transparent multi-hazard watchlist instead.

## Setup with uv

```powershell
uv sync
```

This creates and uses the repository-local `.venv` environment. Run the script
through uv so its declared dependencies are used.

```powershell
uv run dref_analysis.py --self-check
```

## Create the current presentation watchlist

June-July-August, all five regions, with Excel, CSV, and Markdown outputs:

```powershell
uv run dref_analysis.py --region all --months 6 7 8 --output all
```

For the next rolling window, July-August-September:

```powershell
uv run dref_analysis.py --region all --months 7 8 9 --output all
```

Use one region when needed:

```powershell
uv run dref_analysis.py --region 0 --months 6 7 8 --output excel
```

| Code | Region |
|---|---|
| `0` | Africa |
| `1` | Americas |
| `2` | Asia-Pacific |
| `3` | Europe |
| `4` | MENA |

## Risk-score methodology

For each country and hazard, the script takes the highest monthly value in the
selected three-month window and records the month in which it occurs. This
represents the strongest seasonal signal during the presentation period.

The peak is converted to the IFRC GO five-point risk category. DR, FL and TC use
the INFORM scale; WF uses the separate GWIS scale:

| Category | DR / FL / TC | Wildfire |
|---|---:|---:|
| Very Low | up to 2 | up to 2 |
| Low | above 2 to 3.5 | above 2 to 5 |
| Medium | above 3.5 to 5 | above 5 to 9 |
| High | above 5 to 6.5 | above 9 to 17 |
| Very High | above 6.5 | above 17 |

Red and Orange are assigned separately for each IFRC region and hazard. Red
uses Very High countries, falling back to High only when no Very High country
exists. Orange then uses High countries, or Medium when Red had to fall back to
High. A Red country is not repeated in Orange.

For the presentation, hazards are displayed as **Drought/Wildfire** and
**Floods/TC**. Up to four countries per region, group and colour are retained.
The shortlist first covers both hazards where possible, then prefers the
stronger category and countries that remain in that category for more selected
months. Countries in the same UN M49 subregion are grouped under the subregion
name.

The Excel workbook contains the capped `Presentation Watchlist`, every eligible
country in `Hazard Selections`, and the auditable `All Hazard Peaks` data.
Markdown and console show only the capped presentation shortlist. Each country
shows the supporting hazard, risk category, peak score, and peak month. Raw
wildfire scores are never compared directly with DR/FL/TC scores because the
scales differ.

The Red/Orange labels are seasonal-risk screening labels, not a prediction that
a DREF will necessarily be approved or activated.

Risk-score outputs are written to `outputs/risk_scores/`. They are kept separate
from disease-outbreak outputs.

## Current disease-outbreak watch

Run the separate WHO/CDC analysis as of 5 August 2026:

```powershell
uv run disease_outbreak_analysis.py --as-of 2026-08-05 --output all
```

It writes exactly three files under `outputs/disease_outbreaks/`:

- `disease_outbreak_raw_2026-08-05.json`: unclassified WHO reports and CDC RSS notices.
- `disease_outbreak_processed_2026-08-05.csv`: eligible country-disease candidates and shortlist status.
- `disease_outbreak_watch_2026-08-05.md`: presentation-ready Red/Orange shortlist.

The disease watch uses these public sources:

- WHO Disease Outbreak News: `https://www.who.int/api/emergencies/diseaseoutbreaknews`
- WHO country reference: `https://www.who.int/api/whoreference/countries`
- CDC Travel Health Notices RSS: `https://wwwnc.cdc.gov/travel/rss/notices.xml`
- IFRC GO countries and regions: `https://goadmin.ifrc.org/api/v2/country/`

### Disease-outbreak methodology

The script collects WHO Disease Outbreak News published during the previous
year and the current CDC Travel Health Notices. It keeps country-specific events
that can be mapped by ISO3 to an IFRC region and UN M49 subregion. Broad global
or multi-country notices are not automatically assigned to individual countries.

WHO events must have been updated within 120 days and must not contain an
explicit closure statement. CDC events must remain in the current notice feed.
The script retains the risk or notice level published by WHO or CDC rather than
creating a new numerical disease score.

Candidates are ordered using the published severity, confirmation by both
sources where available, and report recency. At most two Red and two Orange
countries are shown for each IFRC region; the processed CSV retains all eligible
candidates for review.

The Markdown heading is **Current and emerging disease outbreak watch - as of
5 August 2026**. This is an outbreak-priority screening product, not an INFORM
index, a numerical disease score, or a forecast that a DREF request will occur.
