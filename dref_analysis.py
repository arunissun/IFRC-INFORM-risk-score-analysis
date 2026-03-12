# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.28",
#   "pandas>=2.0",
#   "openpyxl>=3.1",
# ]
# ///
"""
INFORM Risk Score — DREF Prioritization Tool
=============================================
Queries the INFORM risk score API and classifies countries by
DREF likelihood for a chosen region and three-month window.

SCORING MODES:
  individual   — results per individual hazard (DR, FL, TC separately)
  combined     — combined score: [DR+WF] and [FL+TC]
  both         — runs individual + combined sections (default)

COMBINATION METHODOLOGY:
  Drought + Wildfire (DR+WF):
    The INFORM API provides Drought (DR) scores but NOT a dedicated
    Wildfire (WF) index for Africa/global regions. Therefore:
      - If only DR is available:  combined = DR score
      - WF availability is flagged in the output so you know which
        component was missing.

  Flood + Tropical Cyclone (FL+TC):
    Both FL and TC are available in INFORM. The combined score uses
    the INFORM geometric mean formula:
      combined_monthly = sqrt(FL_score * TC_score)
    Geometric mean is the standard INFORM aggregation method — it is
    sensitive to both components (unlike max which ignores the lower
    value) but dampens outliers (unlike sum which double-counts).
    If one hazard has zero score, the geometric mean collapses to 0,
    which is intentional: a country with zero flood risk and TC risk
    should not show a combined flood+TC threat just from the TC alone.
    In that case, the individual TC or FL score is also shown.
    FALLBACK — if one component is missing/zero for all months:
      combined = the available non-zero component score.

USAGE EXAMPLES:
  uv run dref_analysis.py --region 0 --months 3 4 5
  uv run dref_analysis.py --region 0 --months 6 7 8 --output excel
  uv run dref_analysis.py --region 1 --months 9 10 11 --mode combined
  uv run dref_analysis.py --region 2 --months 12 1 2 --mode both --output all
  uv run dref_analysis.py --region 0 --months 3 4 5 --show-medium

REGION CODES:
  0 = Africa  |  1 = Americas  |  2 = Asia-Pacific  |  3 = Europe  |  4 = MENA
"""

import argparse
import math
import sys
import requests
import pandas as pd
from datetime import datetime
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

API_BASE = "https://go-risk.northeurope.cloudapp.azure.com/api/v1/risk-score/"

REGION_NAMES = {
    0: "Africa",
    1: "Americas",
    2: "Asia-Pacific",
    3: "Europe",
    4: "MENA",
}

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March",    4: "April",
    5: "May",     6: "June",     7: "July",      8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

MONTH_KEYS = {
    1: "january",   2: "february",  3: "march",     4: "april",
    5: "may",       6: "june",      7: "july",       8: "august",
    9: "september", 10: "october",  11: "november",  12: "december",
}

# INFORM risk classification thresholds
THRESHOLDS = {
    "Very High": 6.5,  # ≥ 6.5  → strong case for DREF
    "High":      5.0,  # ≥ 5.0  → DREF probable
    "Medium":    3.0,  # ≥ 3.0  → monitor
}

TIER_EMOJI = {
    "Very High": "🔴",
    "High":      "🟠",
    "Medium":    "🟡",
    "Low":       "🟢",
}

AGGREGATE_REGION_NAMES = {
    "Africa Region", "Americas Region", "Asia-Pacific Region",
    "Europe Region", "MENA Region",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def classify_score(score: float) -> str:
    """Return the INFORM risk class for a given score (0–10 scale)."""
    if score >= THRESHOLDS["Very High"]:
        return "Very High"
    elif score >= THRESHOLDS["High"]:
        return "High"
    elif score >= THRESHOLDS["Medium"]:
        return "Medium"
    return "Low"


def dref_implication(tier: str) -> str:
    return {
        "Very High": "Strong case for DREF",
        "High":      "DREF probable",
        "Medium":    "Monitor closely",
        "Low":       "Unlikely",
    }.get(tier, "")


def geom_mean(a: float, b: float) -> float:
    """
    INFORM geometric mean of two scores.
    If either is zero, falls back to the non-zero value (rather than
    forcing the product to zero when one hazard is genuinely absent).
    """
    if a > 0 and b > 0:
        return round(math.sqrt(a * b), 2)
    elif a > 0:
        return round(a, 2)
    elif b > 0:
        return round(b, 2)
    return 0.0


def month_score(record: dict, month_key: str) -> float:
    """Safely extract a monthly score from a record."""
    return float(record.get(month_key, 0.0) or 0.0)


# ── API ───────────────────────────────────────────────────────────────────────

def fetch_scores(region: int) -> list[dict]:
    """Fetch all risk score records for a given region from the INFORM API."""
    url = f"{API_BASE}?region={region}&limit=9999"
    print(f"\n  Fetching data — region={region} ({REGION_NAMES.get(region, 'Unknown')})…")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ❌ API request failed: {exc}")
        sys.exit(1)
    records = resp.json().get("results", [])
    print(f"  ✅ {len(records)} records received.")
    return records


# ── Individual hazard analysis ────────────────────────────────────────────────

def build_individual_df(records: list[dict], months: list[int]) -> pd.DataFrame:
    """
    One row per (country, hazard_type).
    Risk class is based on the PEAK monthly score within the window.
    """
    mkeys  = [MONTH_KEYS[m]  for m in months]
    mlabels = [MONTH_NAMES[m] for m in months]
    rows = []

    for r in records:
        cd = r.get("country_details", {})
        country = cd.get("name", "Unknown")
        if country in AGGREGATE_REGION_NAMES:
            continue
        hazard = r.get("hazard_type", "")
        if hazard not in ("DR", "FL", "TC"):
            continue

        scores  = [month_score(r, k) for k in mkeys]
        avg     = round(sum(scores) / len(scores), 2)
        peak    = max(scores)
        peak_m  = mlabels[scores.index(peak)]

        row = {
            "Country":          country,
            "ISO3":             cd.get("iso3", ""),
            "Hazard":           hazard,
            "Hazard Label":     {"DR": "Drought", "FL": "Flood", "TC": "Tropical Cyclone"}[hazard],
            "LCC":              r.get("lcc",           0.0),
            "Vulnerability":    r.get("vulnerability",  0.0),
            "Population (k)":   round(r.get("population_in_thousands", 0.0), 1),
            "Window Avg":       avg,
            "Peak Score":       round(peak, 2),
            "Peak Month":       peak_m,
            "Risk Class":       classify_score(peak),
            "DREF Implication": dref_implication(classify_score(peak)),
        }
        for lbl, sc in zip(mlabels, scores):
            row[lbl] = sc
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    cls_order = {"Very High": 0, "High": 1, "Medium": 2, "Low": 3}
    df["_o"] = df["Risk Class"].map(cls_order)
    df = (df.sort_values(["Hazard", "_o", "Window Avg"], ascending=[True, True, False])
            .drop(columns=["_o"])
            .reset_index(drop=True))
    return df


# ── Combined hazard analysis ──────────────────────────────────────────────────

def build_combined_df(records: list[dict], months: list[int]) -> pd.DataFrame:
    """
    Returns one row per country per combined category:
      • Drought + Wildfire  (DR+WF)  — WF not in INFORM API; uses DR only, flagged
      • Flood + Cyclone     (FL+TC)  — geometric mean per month, then peak classified

    METHODOLOGY for FL+TC per month:
        combined_m = geom_mean(FL_m, TC_m)
        where geom_mean(a,b) = sqrt(a*b)  if both > 0
                             = max(a,b)   if one is zero (fallback)
    """
    mkeys   = [MONTH_KEYS[m]  for m in months]
    mlabels = [MONTH_NAMES[m] for m in months]

    # Index records by (country_name, hazard_type)
    index: dict[tuple[str, str], dict] = {}
    meta:  dict[str, dict] = {}  # country_name -> country metadata

    for r in records:
        cd      = r.get("country_details", {})
        country = cd.get("name", "Unknown")
        if country in AGGREGATE_REGION_NAMES:
            continue
        hazard = r.get("hazard_type", "")
        if hazard not in ("DR", "FL", "TC"):
            continue
        index[(country, hazard)] = r
        if country not in meta:
            meta[country] = {
                "ISO3":           cd.get("iso3", ""),
                "LCC":            r.get("lcc",           0.0),
                "Vulnerability":  r.get("vulnerability",  0.0),
                "Population (k)": round(r.get("population_in_thousands", 0.0), 1),
            }

    rows = []
    countries = sorted(meta.keys())

    for country in countries:
        m = meta[country]

        # ── Drought + Wildfire ────────────────────────────────────────────────
        dr_rec = index.get((country, "DR"))
        # WF is not present in the INFORM API for any region
        wf_available = False

        if dr_rec is not None:
            dr_scores = [month_score(dr_rec, k) for k in mkeys]
        else:
            dr_scores = [0.0] * len(months)

        # Combined DR+WF = DR score (WF absent); flag it
        drwf_scores = dr_scores  # placeholder — if WF ever added, use geom_mean here
        drwf_avg    = round(sum(drwf_scores) / len(drwf_scores), 2)
        drwf_peak   = max(drwf_scores) if drwf_scores else 0.0
        drwf_peak_m = mlabels[drwf_scores.index(drwf_peak)] if drwf_peak > 0 else "—"
        drwf_class  = classify_score(drwf_peak)

        drwf_row = {
            "Country":         country,
            "Category":        "Drought + Wildfire",
            "Category Code":   "DR+WF",
            "DR available":    dr_rec is not None,
            "WF available":    wf_available,
            "Combination":     "DR only (WF not in INFORM API)",
            **m,
            "Window Avg":      drwf_avg,
            "Peak Score":      round(drwf_peak, 2),
            "Peak Month":      drwf_peak_m,
            "Risk Class":      drwf_class,
            "DREF Implication": dref_implication(drwf_class),
        }
        for lbl, sc in zip(mlabels, drwf_scores):
            drwf_row[lbl] = sc
        rows.append(drwf_row)

        # ── Flood + Tropical Cyclone ──────────────────────────────────────────
        fl_rec = index.get((country, "FL"))
        tc_rec = index.get((country, "TC"))

        fl_scores = [month_score(fl_rec, k) for k in mkeys] if fl_rec else [0.0] * len(months)
        tc_scores = [month_score(tc_rec, k) for k in mkeys] if tc_rec else [0.0] * len(months)

        # Determine which components are actually present (non-zero across all months)
        fl_present = fl_rec is not None and any(s > 0 for s in fl_scores)
        tc_present = tc_rec is not None and any(s > 0 for s in tc_scores)

        if fl_present and tc_present:
            combination_desc = "Geometric mean: √(FL × TC)"
        elif fl_present:
            combination_desc = "FL only (TC = 0 for this country)"
        elif tc_present:
            combination_desc = "TC only (FL = 0 for this country)"
        else:
            combination_desc = "No FL or TC data"

        fltc_scores = [geom_mean(f, t) for f, t in zip(fl_scores, tc_scores)]
        fltc_avg    = round(sum(fltc_scores) / len(fltc_scores), 2)
        fltc_peak   = max(fltc_scores) if fltc_scores else 0.0
        fltc_peak_m = mlabels[fltc_scores.index(fltc_peak)] if fltc_peak > 0 else "—"
        fltc_class  = classify_score(fltc_peak)

        fltc_row = {
            "Country":          country,
            "Category":         "Flood + Tropical Cyclone",
            "Category Code":    "FL+TC",
            "FL available":     fl_present,
            "TC available":     tc_present,
            "Combination":      combination_desc,
            **m,
            "Window Avg":       fltc_avg,
            "Peak Score":       round(fltc_peak, 2),
            "Peak Month":       fltc_peak_m,
            "Risk Class":       fltc_class,
            "DREF Implication": dref_implication(fltc_class),
        }
        # Also store raw FL and TC per month for transparency
        for lbl, fs, ts, cs in zip(mlabels, fl_scores, tc_scores, fltc_scores):
            fltc_row[f"FL_{lbl}"] = fs
            fltc_row[f"TC_{lbl}"] = ts
            fltc_row[lbl]         = cs   # combined column

        rows.append(fltc_row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    cls_order = {"Very High": 0, "High": 1, "Medium": 2, "Low": 3}
    df["_o"] = df["Risk Class"].map(cls_order)
    df = (df.sort_values(["Category Code", "_o", "Window Avg"], ascending=[True, True, False])
            .drop(columns=["_o"])
            .reset_index(drop=True))
    return df


# ── Display helpers ───────────────────────────────────────────────────────────

def print_banner(region: int, months: list[int], mode: str) -> None:
    region_name = REGION_NAMES.get(region, f"Region {region}")
    month_str   = " / ".join(MONTH_NAMES[m] for m in months)
    print(f"\n{'='*76}")
    print(f"  INFORM DREF PRIORITIZATION REPORT")
    print(f"  Region : {region_name}  |  Period : {month_str}  |  Mode : {mode}")
    print(f"  Generated : {datetime.now().strftime('%d %b %Y %H:%M')}")
    print(f"{'='*76}")


def _table_header(mlabels: list[str]) -> None:
    cols = "  ".join(f"{m[:3]:>5}" for m in mlabels)
    print(f"  {'Country':<28} {'Win Avg':>8}  {'Peak':>6}  {'Peak Month':<12}"
          f"  {'LCC':>5}  {'Vuln':>5}  {cols}")
    print(f"  {'-'*28}  {'-------':>8}  {'-----':>6}  {'----------':<12}"
          f"  {'-----':>5}  {'-----':>5}  " + "  ".join(f"{'-----':>5}" for _ in mlabels))


def _table_row(row: pd.Series, mlabels: list[str]) -> None:
    scores_str = "  ".join(f"{row.get(m, 0.0):>5.1f}" for m in mlabels)
    print(f"  {row['Country']:<28} {row['Window Avg']:>8.2f}  {row['Peak Score']:>6.2f}"
          f"  {row['Peak Month']:<12}  {row['LCC']:>5.1f}  {row['Vulnerability']:>5.1f}  {scores_str}")


def print_individual_section(df: pd.DataFrame, hazard: str,
                             months: list[int], show_medium: bool) -> None:
    mlabels = [MONTH_NAMES[m] for m in months]
    label_map = {"DR": "Drought", "FL": "Flood", "TC": "Tropical Cyclone"}
    label = label_map.get(hazard, hazard)

    sub = df[df["Hazard"] == hazard].copy()
    tiers = ["Very High", "High"] + (["Medium"] if show_medium else [])
    sub = sub[sub["Risk Class"].isin(tiers)]

    print(f"\n{'─'*76}")
    print(f"  INDIVIDUAL HAZARD — {label} ({hazard})")
    print(f"{'─'*76}")

    if sub.empty:
        print("  No countries qualify at Very High or High level for this window.")
        return

    for tier in tiers:
        subset = sub[sub["Risk Class"] == tier]
        if subset.empty:
            continue
        emoji = TIER_EMOJI[tier]
        print(f"\n  {emoji} {tier.upper()}  ·  {dref_implication(tier)}")
        _table_header(mlabels)
        for _, row in subset.iterrows():
            _table_row(row, mlabels)


def print_combined_section(df: pd.DataFrame, category_code: str,
                           months: list[int], show_medium: bool) -> None:
    mlabels = [MONTH_NAMES[m] for m in months]
    label_map = {
        "DR+WF": "Drought + Wildfire  (combined)",
        "FL+TC": "Flood + Tropical Cyclone  (combined, geometric mean)",
    }
    label = label_map.get(category_code, category_code)

    sub = df[df["Category Code"] == category_code].copy()
    tiers = ["Very High", "High"] + (["Medium"] if show_medium else [])
    sub = sub[sub["Risk Class"].isin(tiers)]

    print(f"\n{'─'*76}")
    print(f"  COMBINED — {label}")
    if category_code == "DR+WF":
        print(f"  NOTE: Wildfire (WF) is not available in the INFORM API.")
        print(f"        Combined score = DR score. WF column flagged N/A.")
    elif category_code == "FL+TC":
        print(f"  METHOD: combined_month = √(FL_score × TC_score)")
        print(f"          If one component is 0, fallback = the non-zero component.")
    print(f"{'─'*76}")

    if sub.empty:
        print("  No countries qualify at Very High or High level for this window.")
        return

    for tier in tiers:
        subset = sub[sub["Risk Class"] == tier]
        if subset.empty:
            continue
        emoji = TIER_EMOJI[tier]
        print(f"\n  {emoji} {tier.upper()}  ·  {dref_implication(tier)}")
        _table_header(mlabels)
        for _, row in subset.iterrows():
            _table_row(row, mlabels)
            # Show combination note inline
            combo = row.get("Combination", "")
            if combo:
                print(f"    ↳ {combo}")


def print_summary(ind_df: pd.DataFrame, comb_df: pd.DataFrame,
                  months: list[int]) -> None:
    month_str = " / ".join(MONTH_NAMES[m] for m in months)
    print(f"\n{'='*76}")
    print(f"  SUMMARY — DREF Candidates (Very High + High) · {month_str}")
    print(f"{'='*76}")
    print(f"  {'#':<4} {'Country':<28} {'Category':<28} {'Class':<12} {'Win Avg':>8}  Implication")
    print(f"  {'─'*4} {'─'*28} {'─'*28} {'─'*12} {'─'*8}  {'─'*22}")

    rows_ind  = ind_df[ind_df["Risk Class"].isin(["Very High", "High"])]  if not ind_df.empty  else pd.DataFrame()
    rows_comb = comb_df[comb_df["Risk Class"].isin(["Very High", "High"])] if not comb_df.empty else pd.DataFrame()

    i = 1
    for _, row in rows_ind.iterrows():
        emoji = TIER_EMOJI.get(row["Risk Class"], "")
        label = f"{row['Hazard Label']} [{row['Hazard']}]"
        print(f"  {i:<4} {row['Country']:<28} {label:<28} "
              f"{emoji} {row['Risk Class']:<10} {row['Window Avg']:>8.2f}  {row['DREF Implication']}")
        i += 1

    for _, row in rows_comb.iterrows():
        emoji = TIER_EMOJI.get(row["Risk Class"], "")
        label = f"{row['Category']} [{row['Category Code']}]"
        print(f"  {i:<4} {row['Country']:<28} {label:<28} "
              f"{emoji} {row['Risk Class']:<10} {row['Window Avg']:>8.2f}  {row['DREF Implication']}")
        i += 1

    print(f"\n  Total DREF candidates shown: {i - 1}")


# ── Export ────────────────────────────────────────────────────────────────────

def _autosize_excel(ws) -> None:
    for col in ws.columns:
        max_len = max((len(str(c.value)) for c in col if c.value), default=10)
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 45)


def export_excel(ind_df: pd.DataFrame, comb_df: pd.DataFrame,
                 region: int, months: list[int], output_dir: Path) -> None:
    rname     = REGION_NAMES.get(region, f"region{region}").lower().replace("-", "_")
    month_tag = "_".join(MONTH_NAMES[m][:3] for m in months)
    fname     = output_dir / f"dref_{rname}_{month_tag}.xlsx"

    with pd.ExcelWriter(fname, engine="openpyxl") as writer:
        # Individual hazards
        if not ind_df.empty:
            for hazard, label in [("DR", "Drought"), ("FL", "Flood"), ("TC", "TC")]:
                sub = ind_df[ind_df["Hazard"] == hazard]
                if not sub.empty:
                    sub.to_excel(writer, sheet_name=f"Individual_{label}", index=False)
                    _autosize_excel(writer.sheets[f"Individual_{label}"])

        # Combined
        if not comb_df.empty:
            for code, label in [("DR+WF", "Drought+WF"), ("FL+TC", "Flood+TC")]:
                sub = comb_df[comb_df["Category Code"] == code]
                if not sub.empty:
                    # DREF only sheet
                    dref_sub = sub[sub["Risk Class"].isin(["Very High", "High"])]
                    sheet    = f"DREF_{label}"
                    dref_sub.to_excel(writer, sheet_name=sheet, index=False)
                    _autosize_excel(writer.sheets[sheet])
                    # Full sheet
                    full_sheet = f"All_{label}"
                    sub.to_excel(writer, sheet_name=full_sheet, index=False)
                    _autosize_excel(writer.sheets[full_sheet])

    print(f"\n  💾 Excel → {fname}")


def export_csv(ind_df: pd.DataFrame, comb_df: pd.DataFrame,
               region: int, months: list[int], output_dir: Path) -> None:
    rname     = REGION_NAMES.get(region, f"region{region}").lower().replace("-", "_")
    month_tag = "_".join(MONTH_NAMES[m][:3] for m in months)

    if not ind_df.empty:
        p = output_dir / f"individual_{rname}_{month_tag}.csv"
        ind_df.to_csv(p, index=False)
        print(f"  💾 CSV  → {p}")

    if not comb_df.empty:
        for code, label in [("DR+WF", "drwf"), ("FL+TC", "fltc")]:
            sub = comb_df[comb_df["Category Code"] == code]
            if not sub.empty:
                p = output_dir / f"combined_{label}_{rname}_{month_tag}.csv"
                sub.to_csv(p, index=False)
                print(f"  💾 CSV  → {p}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="INFORM DREF Prioritization Tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--region", "-r", type=int, default=0, choices=[0, 1, 2, 3, 4],
        help="0=Africa 1=Americas 2=Asia-Pacific 3=Europe 4=MENA  (default: 0)",
    )
    parser.add_argument(
        "--months", "-m", type=int, nargs=3, default=[3, 4, 5],
        metavar=("M1", "M2", "M3"),
        help="Three months 1–12  e.g. --months 3 4 5  (default: March April May)",
    )
    parser.add_argument(
        "--mode", type=str, default="both",
        choices=["individual", "combined", "both"],
        help="individual = per-hazard; combined = DR+WF and FL+TC; both = all  (default: both)",
    )
    parser.add_argument(
        "--output", "-o", type=str, default="console",
        choices=["console", "excel", "csv", "all"],
        help="Output format  (default: console)",
    )
    parser.add_argument(
        "--output-dir", type=str, default=".",
        help="Folder for saved files  (default: current directory)",
    )
    parser.add_argument(
        "--show-medium", action="store_true",
        help="Also show Medium-risk countries (score 3.0–4.9)",
    )
    return parser.parse_args()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()

    for m in args.months:
        if not 1 <= m <= 12:
            print(f"  ❌ Invalid month {m}. Must be 1–12.")
            sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    records = fetch_scores(args.region)
    print_banner(args.region, args.months, args.mode)

    ind_df   = pd.DataFrame()
    comb_df  = pd.DataFrame()

    # ── Individual mode ───────────────────────────────────────────────────────
    if args.mode in ("individual", "both"):
        ind_df = build_individual_df(records, args.months)
        for hazard in ("DR", "FL", "TC"):
            print_individual_section(ind_df, hazard, args.months, args.show_medium)

    # ── Combined mode ─────────────────────────────────────────────────────────
    if args.mode in ("combined", "both"):
        comb_df = build_combined_df(records, args.months)
        for cat in ("DR+WF", "FL+TC"):
            print_combined_section(comb_df, cat, args.months, args.show_medium)

    # ── Summary ───────────────────────────────────────────────────────────────
    print_summary(ind_df, comb_df, args.months)

    # ── File outputs ──────────────────────────────────────────────────────────
    if args.output in ("excel", "all"):
        try:
            export_excel(ind_df, comb_df, args.region, args.months, output_dir)
        except Exception as exc:
            print(f"  ⚠️  Excel export failed: {exc}")

    if args.output in ("csv", "all"):
        export_csv(ind_df, comb_df, args.region, args.months, output_dir)

    print()


if __name__ == "__main__":
    main()
