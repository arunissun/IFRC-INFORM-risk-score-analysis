# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.28",
#   "pandas>=2.0",
#   "openpyxl>=3.1",
# ]
# ///
"""Build a four-hazard seasonal DREF watchlist from current IFRC GO APIs.

The watchlist is intentionally not a combined INFORM index. It keeps every
eligible country in the audit data, then selects at most four distinct countries
per region, hazard group, and colour for the presentation:

* Red: every Very High country, falling back to High if none is Very High.
* Orange: High after a Very High Red; otherwise Medium.

Run, for example:
    uv run dref_analysis.py --region all --months 6 7 8 --output all
    uv run dref_analysis.py --region 0 --months 7 8 9 --output excel
    uv run dref_analysis.py --self-check
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests


RISK_SCORE_URL = "https://go-risk-api.ifrc.org/api/v1/risk-score/"
SEASONAL_URL = "https://go-risk-api.ifrc.org/api/v1/seasonal/"
M49_LOOKUP_PATH = Path(__file__).with_name("un_m49_subregions.csv")

REGION_NAMES = {
    0: "Africa",
    1: "Americas",
    2: "Asia-Pacific",
    3: "Europe",
    4: "MENA",
}

MONTH_NAMES = {
    1: "January", 2: "February", 3: "March", 4: "April",
    5: "May", 6: "June", 7: "July", 8: "August",
    9: "September", 10: "October", 11: "November", 12: "December",
}

HAZARD_LABELS = {
    "DR": "Drought",
    "WF": "Wildfire",
    "FL": "Flood",
    "TC": "Tropical Cyclone",
}

HAZARD_GROUPS = {
    "DR": "Drought/Wildfire",
    "WF": "Drought/Wildfire",
    "FL": "Floods/TC",
    "TC": "Floods/TC",
}

GROUP_ORDER = {"Drought/Wildfire": 0, "Floods/TC": 1}
PRESENTATION_COUNTRY_LIMIT = 4
CATEGORY_LEVEL = {
    "Very Low": 1,
    "Low": 2,
    "Medium": 3,
    "High": 4,
    "Very High": 5,
}
AGGREGATE_REGION_NAMES = set(REGION_NAMES.values()) | {
    f"{name} Region" for name in REGION_NAMES.values()
}


def month_key(month: int) -> str:
    """Return the lower-case API field name for a month number."""
    return MONTH_NAMES[month].lower()


def rolling_months(analysis_date: date) -> list[int]:
    """Return the three-month window ending in the analysis month."""
    return [((analysis_date.month - offset - 1) % 12) + 1 for offset in (2, 1, 0)]


def classify_score(hazard: str, score: float) -> str:
    """Return the IFRC GO display category for one hazard's raw score."""
    if hazard == "WF":
        if score <= 2:
            return "Very Low"
        if score <= 5:
            return "Low"
        if score <= 9:
            return "Medium"
        if score <= 17:
            return "High"
        return "Very High"

    if score <= 2:
        return "Very Low"
    if score <= 3.5:
        return "Low"
    if score <= 5:
        return "Medium"
    if score <= 6.5:
        return "High"
    return "Very High"


def score_value(record: dict, key: str) -> float:
    """Safely read one monthly value from an API record."""
    return float(record.get(key, 0.0) or 0.0)


def selected_window(record: dict, months: list[int]) -> tuple[list[float], float, str]:
    """Return values, highest value, and its first month in the selected window."""
    scores = [score_value(record, month_key(month)) for month in months]
    peak = max(scores)
    return scores, peak, MONTH_NAMES[months[scores.index(peak)]]


def country_details(record: dict) -> tuple[str, str]:
    """Read country name and ISO3 from either current API response shape."""
    details = record.get("country_details") or {}
    country = details.get("name")
    iso3 = details.get("iso3", "")

    if not country:
        country_field = record.get("country")
        if isinstance(country_field, dict):
            country = country_field.get("name")
            iso3 = iso3 or country_field.get("iso3", "")

    return country or "Unknown", str(iso3 or "").upper()


def load_un_m49_lookup() -> pd.DataFrame:
    """Load the versioned UN M49 ISO3-to-geographic-group lookup."""
    required_columns = {"ISO3", "UN Subregion", "UN Intermediate Region"}
    try:
        lookup = pd.read_csv(M49_LOOKUP_PATH, dtype=str, keep_default_na=False)
    except OSError as exc:
        raise RuntimeError(f"Cannot read UN M49 lookup: {exc}") from exc

    if not required_columns.issubset(lookup.columns):
        raise RuntimeError(f"UN M49 lookup is missing columns: {required_columns}")

    lookup = lookup[["ISO3", "UN Subregion", "UN Intermediate Region"]].copy()
    lookup["ISO3"] = lookup["ISO3"].str.upper()
    if lookup["ISO3"].duplicated().any():
        raise RuntimeError("UN M49 lookup has duplicate ISO3 codes.")

    lookup["UN Regional Group"] = lookup["UN Intermediate Region"].where(
        lookup["UN Intermediate Region"].ne(""), lookup["UN Subregion"]
    )
    return lookup


def add_un_m49_groups(hazard_peaks: pd.DataFrame) -> pd.DataFrame:
    """Attach the most specific available UN M49 geographic group to each row."""
    if hazard_peaks.empty:
        return hazard_peaks

    result = hazard_peaks.merge(load_un_m49_lookup(), on="ISO3", how="left", validate="many_to_one")
    unmapped = result["UN Regional Group"].isna()
    if unmapped.any():
        codes = ", ".join(sorted(result.loc[unmapped, "ISO3"].unique()))
        print(f"Warning: no UN M49 mapping for {codes}.", file=sys.stderr)
        result.loc[unmapped, "UN Regional Group"] = "Unmapped in UN M49"
        result.loc[unmapped, ["UN Subregion", "UN Intermediate Region"]] = ""
    return result


def fetch_json(url: str, params: dict) -> object:
    """Fetch one IFRC GO JSON response or exit with a useful error."""
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"API request failed: {exc}", file=sys.stderr)
        sys.exit(1)
    return response.json()


def fetch_region_records(region: int) -> tuple[list[dict], list[dict]]:
    """Fetch INFORM DR/FL/TC and GWIS wildfire records for one region."""
    print(f"Fetching {REGION_NAMES[region]} data...")
    risk_payload = fetch_json(RISK_SCORE_URL, {"region": region, "limit": 9999})
    seasonal_payload = fetch_json(SEASONAL_URL, {"region": region})

    risk_records = risk_payload.get("results", []) if isinstance(risk_payload, dict) else []
    if isinstance(seasonal_payload, list):
        seasonal_payload = seasonal_payload[0] if seasonal_payload else {}
    wildfire_records = (
        seasonal_payload.get("gwis_seasonal", [])
        if isinstance(seasonal_payload, dict)
        else []
    )

    print(f"  {len(risk_records)} INFORM records; {len(wildfire_records)} GWIS wildfire records.")
    return risk_records, wildfire_records


def build_hazard_peaks(
    regional_records: list[tuple[int, list[dict], list[dict]]], months: list[int]
) -> pd.DataFrame:
    """Create one peak-within-window row per country and hazard."""
    rows = []
    seen: set[tuple[int, str, str]] = set()

    for region, risk_records, wildfire_records in regional_records:
        sources = (
            (risk_records, "INFORM risk-score API"),
            (wildfire_records, "GWIS seasonal API"),
        )
        for records, source in sources:
            for record in records:
                hazard = str(record.get("hazard_type", "")).upper()
                if hazard not in HAZARD_LABELS:
                    continue

                country, iso3 = country_details(record)
                if (
                    country in AGGREGATE_REGION_NAMES
                    or country == "Unknown"
                    or country.endswith("Country Cluster")
                ):
                    continue

                key = (region, iso3 or country, hazard)
                if key in seen:
                    continue
                seen.add(key)

                scores, peak, peak_month = selected_window(record, months)
                categories = [classify_score(hazard, score) for score in scores]
                peak_category = classify_score(hazard, peak)
                high_months = sum(CATEGORY_LEVEL[category] >= 4 for category in categories)
                row = {
                    "Region Code": region,
                    "Region": REGION_NAMES[region],
                    "Group": HAZARD_GROUPS[hazard],
                    "Country": country,
                    "ISO3": iso3,
                    "Hazard": hazard,
                    "Hazard Label": HAZARD_LABELS[hazard],
                    "Source": source,
                    "Peak Score": round(peak, 2),
                    "Peak Month": peak_month,
                    "Risk Category": peak_category,
                    "Months in Risk Category": categories.count(peak_category),
                    "High or Very High Months": high_months,
                }
                row.update({MONTH_NAMES[month]: score for month, score in zip(months, scores)})
                rows.append(row)

    month_columns = [MONTH_NAMES[month] for month in months]
    columns = [
        "Region Code", "Region", "Group", "Country", "ISO3", "Hazard",
        "Hazard Label", "Source", "Peak Score", "Peak Month", "Risk Category",
        "Months in Risk Category", "High or Very High Months", *month_columns,
    ]
    if not rows:
        return pd.DataFrame(columns=columns)

    df = pd.DataFrame(rows)
    df["_category_order"] = df["Risk Category"].map(CATEGORY_LEVEL)
    df = (
        df.sort_values(
            ["Region Code", "Hazard", "_category_order", "Peak Score", "High or Very High Months", "Country"],
            ascending=[True, True, False, False, False, True],
        )
        .drop(columns="_category_order")
        .reset_index(drop=True)
    )
    return df[columns]


def ranked_in_category(subset: pd.DataFrame, category: str) -> pd.DataFrame:
    """Rank all countries within one region, hazard, and risk category."""
    candidates = subset[subset["Risk Category"] == category]
    candidates = candidates.sort_values(
        ["Peak Score", "Months in Risk Category", "Country"],
        ascending=[False, False, True],
    ).copy()
    candidates["Hazard Tier Rank"] = range(1, len(candidates) + 1)
    return candidates


def selected_row(row: pd.Series, colour: str, rule: str) -> dict:
    """Attach the presentation selection metadata to one country-hazard row."""
    result = row.to_dict()
    result["Colour"] = colour
    result["DREF Status"] = (
        "DREF highly probable" if colour == "Red" else "DREF uncertain/probable"
    )
    result["Selection Rule"] = rule
    return result


def select_hazard_watchlist(hazard_peaks: pd.DataFrame) -> pd.DataFrame:
    """Select all Red/Orange-tier countries for every region and hazard."""
    selected = []
    if hazard_peaks.empty:
        return pd.DataFrame()

    for (_, _, hazard), subset in hazard_peaks.groupby(
        ["Region Code", "Region", "Hazard"], sort=True
    ):
        very_high = ranked_in_category(subset, "Very High")
        high = ranked_in_category(subset, "High")
        medium = ranked_in_category(subset, "Medium")

        if not very_high.empty:
            selected.extend(
                selected_row(row, "Red", "Very High risk tier")
                for _, row in very_high.iterrows()
            )
            if not high.empty:
                selected.extend(
                    selected_row(row, "Orange", "High risk tier")
                    for _, row in high.iterrows()
                )
        elif not high.empty:
            selected.extend(
                selected_row(row, "Red", "High risk tier (no Very High country)")
                for _, row in high.iterrows()
            )
            if not medium.empty:
                selected.extend(
                    selected_row(row, "Orange", "Medium risk tier")
                    for _, row in medium.iterrows()
                )

    if not selected:
        return pd.DataFrame()

    result = pd.DataFrame(selected)
    result["_group_order"] = result["Group"].map(GROUP_ORDER)
    result["_colour_order"] = result["Colour"].map({"Red": 0, "Orange": 1})
    result = (
        result.sort_values(
            ["Region Code", "_group_order", "_colour_order", "Hazard", "Hazard Tier Rank"],
            ascending=[True, True, True, True, True],
        )
        .drop(columns=["_group_order", "_colour_order"])
        .reset_index(drop=True)
    )
    return result


def presentation_options(candidates: pd.DataFrame, group_candidates: pd.DataFrame) -> list[dict]:
    """Rank distinct countries without comparing scores from different hazards."""
    options = []
    for (country, iso3, un_group), candidate_rows in candidates.groupby(
        ["Country", "ISO3", "UN Regional Group"], sort=False
    ):
        supporting_rows = group_candidates[group_candidates["ISO3"] == iso3]
        options.append({
            "Country": country,
            "ISO3": iso3,
            "UN Regional Group": un_group,
            "Candidate Hazards": set(candidate_rows["Hazard"]),
            "Hazard Count": supporting_rows["Hazard"].nunique(),
            "Best Hazard Tier Rank": int(candidate_rows["Hazard Tier Rank"].min()),
            "Qualifying Months": int(candidate_rows["Months in Risk Category"].sum()),
            "Supporting Rows": supporting_rows,
        })

    return sorted(
        options,
        key=lambda option: (
            -option["Hazard Count"],
            option["Best Hazard Tier Rank"],
            -option["Qualifying Months"],
            option["Country"],
        ),
    )


def choose_presentation_options(candidates: pd.DataFrame, group_candidates: pd.DataFrame) -> list[dict]:
    """Choose up to four countries while covering every available hazard."""
    options = presentation_options(candidates, group_candidates)
    if not options:
        return []

    chosen = []
    chosen_iso3 = set()
    uncovered_hazards = set(candidates["Hazard"])
    while uncovered_hazards:
        available = [item for item in options if item["ISO3"] not in chosen_iso3]
        option = max(
            available,
            key=lambda item: len(item["Candidate Hazards"] & uncovered_hazards),
            default=None,
        )
        if option is None or not option["Candidate Hazards"] & uncovered_hazards:
            break
        chosen.append(option)
        chosen_iso3.add(option["ISO3"])
        uncovered_hazards -= option["Candidate Hazards"]

    for option in options:
        if len(chosen) == PRESENTATION_COUNTRY_LIMIT:
            break
        if option["ISO3"] not in chosen_iso3:
            chosen.append(option)
            chosen_iso3.add(option["ISO3"])

    return sorted(
        chosen,
        key=lambda option: (
            -option["Hazard Count"],
            option["Best Hazard Tier Rank"],
            -option["Qualifying Months"],
            option["Country"],
        ),
    )[:PRESENTATION_COUNTRY_LIMIT]


def build_presentation_watchlist(selected: pd.DataFrame) -> pd.DataFrame:
    """Build the capped four-country Red/Orange presentation shortlist."""
    columns = [
        "Region Code", "Region", "Group", "UN Regional Group", "Colour",
        "DREF Status", "Presentation Rank", "Country", "ISO3", "Hazard Count",
        "Highest Risk Category", "Hazard Reasons", "Selection Basis",
    ]
    if selected.empty:
        return pd.DataFrame(columns=columns)

    rows = []
    for (region_code, region, group), group_candidates in selected.groupby(
        ["Region Code", "Region", "Group"], sort=True
    ):
        red_candidates = group_candidates[group_candidates["Colour"] == "Red"]
        red_iso3 = set(red_candidates["ISO3"])
        colour_candidates = {
            "Red": red_candidates,
            "Orange": group_candidates[
                (group_candidates["Colour"] == "Orange")
                & ~group_candidates["ISO3"].isin(red_iso3)
            ],
        }

        for colour in ("Red", "Orange"):
            chosen = choose_presentation_options(colour_candidates[colour], group_candidates)
            for rank, option in enumerate(chosen, start=1):
                supporting = option["Supporting Rows"].copy()
                supporting["_category_order"] = supporting["Risk Category"].map(CATEGORY_LEVEL)
                supporting = supporting.sort_values(
                    ["_category_order", "Hazard Tier Rank"], ascending=[False, True]
                )
                reasons = "; ".join(
                    f"{item['Hazard Label']}, {item['Risk Category']}, "
                    f"{item['Peak Score']:.1f} ({item['Peak Month']})"
                    for _, item in supporting.iterrows()
                )
                highest = max(supporting["Risk Category"], key=CATEGORY_LEVEL.get)
                rows.append({
                    "Region Code": region_code,
                    "Region": region,
                    "Group": group,
                    "UN Regional Group": option["UN Regional Group"],
                    "Colour": colour,
                    "DREF Status": (
                        "DREF highly probable" if colour == "Red" else "DREF uncertain/probable"
                    ),
                    "Presentation Rank": rank,
                    "Country": option["Country"],
                    "ISO3": option["ISO3"],
                    "Hazard Count": option["Hazard Count"],
                    "Highest Risk Category": highest,
                    "Hazard Reasons": reasons,
                    "Selection Basis": (
                        "Qualifies for both hazards"
                        if option["Hazard Count"] > 1
                        else "Best available hazard-tier rank"
                    ),
                })

    result = pd.DataFrame(rows, columns=columns)
    if result.empty:
        return result
    result["_group_order"] = result["Group"].map(GROUP_ORDER)
    result["_colour_order"] = result["Colour"].map({"Red": 0, "Orange": 1})
    return (
        result.sort_values(
            ["Region Code", "_group_order", "_colour_order", "Presentation Rank"],
            ascending=True,
        )
        .drop(columns=["_group_order", "_colour_order"])
        .reset_index(drop=True)
    )


def presentation_display_lines(colour_subset: pd.DataFrame, markdown: bool = False) -> list[str]:
    """Group two or more selected countries from the same UN M49 subregion."""
    ordered = colour_subset.sort_values("Presentation Rank")
    lines = []
    for un_group in ordered["UN Regional Group"].drop_duplicates():
        regional = ordered[ordered["UN Regional Group"] == un_group]
        details = []
        for _, row in regional.iterrows():
            country = f"**{row['Country']}**" if markdown else row["Country"]
            details.append(f"{country} — {row['Hazard Reasons']}")
        if len(regional) > 1:
            lines.append(f"{un_group} ({' | '.join(details)})")
        else:
            lines.append(f"{details[0]} — {un_group}")
    return lines


def print_report(presentation: pd.DataFrame, months: list[int]) -> None:
    """Print the capped presentation shortlist."""
    month_text = " / ".join(MONTH_NAMES[month] for month in months)
    print(f"\n{'=' * 74}\nIFRC GO FOUR-HAZARD DREF WATCHLIST\nPeriod: {month_text}\nGenerated: {datetime.now():%d %b %Y %H:%M}\n{'=' * 74}")
    print("Red: DREF highly probable | Orange: DREF uncertain/probable")
    print("Selection is a seasonal watchlist, not a combined INFORM index.\n")

    if presentation.empty:
        print("No Very High, High, or Medium hazard selections were found.")
        return

    for region_code in sorted(presentation["Region Code"].unique()):
        region_subset = presentation[presentation["Region Code"] == region_code]
        print(f"{REGION_NAMES[region_code].upper()}")
        for group in GROUP_ORDER:
            group_subset = region_subset[region_subset["Group"] == group]
            if group_subset.empty:
                continue
            print(f"  {group}")
            for colour in ("Red", "Orange"):
                colour_subset = group_subset[group_subset["Colour"] == colour]
                if colour_subset.empty:
                    continue
                print(f"    {colour} — {colour_subset.iloc[0]['DREF Status']}")
                for rank, line in enumerate(presentation_display_lines(colour_subset), start=1):
                    print(f"      {rank}. {line}")
        print()


def autosize_excel(writer: pd.ExcelWriter, sheet_name: str) -> None:
    """Make exported sheets readable without adding a formatting dependency."""
    worksheet = writer.sheets[sheet_name]
    for column in worksheet.columns:
        width = max((len(str(cell.value)) for cell in column if cell.value is not None), default=10)
        worksheet.column_dimensions[column[0].column_letter].width = min(width + 2, 55)


def file_stem(regions: list[int], months: list[int]) -> str:
    """Build a stable output filename stem."""
    region_part = "all_regions" if len(regions) == len(REGION_NAMES) else REGION_NAMES[regions[0]].lower().replace("-", "_")
    month_part = "_".join(MONTH_NAMES[month][:3] for month in months)
    return f"dref_watchlist_{region_part}_{month_part}"


def export_excel(
    presentation: pd.DataFrame,
    selected: pd.DataFrame,
    peaks: pd.DataFrame,
    regions: list[int],
    months: list[int],
    output_dir: Path,
) -> None:
    """Export presentation rows, exact selections, and their audit data."""
    path = output_dir / f"{file_stem(regions, months)}.xlsx"
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for sheet_name, dataframe in (
            ("Presentation Watchlist", presentation),
            ("Hazard Selections", selected),
            ("All Hazard Peaks", peaks),
        ):
            dataframe.to_excel(writer, sheet_name=sheet_name, index=False)
            autosize_excel(writer, sheet_name)
    print(f"Excel written: {path}")


def export_csv(
    peaks: pd.DataFrame,
    regions: list[int],
    months: list[int],
    output_dir: Path,
) -> None:
    """Export one machine-readable audit table; other views remain in Excel."""
    path = output_dir / f"{file_stem(regions, months)}_all_hazard_peaks.csv"
    peaks.to_csv(path, index=False)
    print(f"CSV written: {path}")


def export_markdown(
    presentation: pd.DataFrame,
    regions: list[int],
    months: list[int],
    output_dir: Path,
) -> None:
    """Write grouped Red/Orange country lists for presentation drafting."""
    month_text = " / ".join(MONTH_NAMES[month] for month in months)
    lines = [
        "# IFRC GO four-hazard DREF watchlist",
        "",
        f"**Period:** {month_text}",
        "",
        "- Red: DREF highly probable",
        "- Orange: DREF uncertain/probable",
        "- This is a seasonal watchlist, not a combined INFORM index.",
    ]
    for region_code in regions:
        region_subset = presentation[presentation["Region Code"] == region_code]
        if region_subset.empty:
            continue
        lines.extend(["", f"## {REGION_NAMES[region_code]}"])
        for group in GROUP_ORDER:
            group_subset = region_subset[region_subset["Group"] == group]
            if group_subset.empty:
                continue
            lines.extend(["", f"### {group}"])
            for colour in ("Red", "Orange"):
                colour_subset = group_subset[group_subset["Colour"] == colour]
                if colour_subset.empty:
                    continue
                status = colour_subset.iloc[0]["DREF Status"]
                lines.extend(["", f"**{colour} — {status}**"])
                for rank, line in enumerate(
                    presentation_display_lines(colour_subset, markdown=True), start=1
                ):
                    lines.append(f"{rank}. {line}")

    path = output_dir / f"{file_stem(regions, months)}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdown written: {path}")


def run_self_check() -> None:
    """Small runnable check for thresholds, window peaks, and fallback selection."""
    assert classify_score("DR", 6.5) == "High"
    assert classify_score("DR", 6.51) == "Very High"
    assert classify_score("WF", 17) == "High"
    assert classify_score("WF", 17.01) == "Very High"
    lookup = load_un_m49_lookup().set_index("ISO3")
    assert lookup.loc["MLI", "UN Regional Group"] == "Western Africa"
    assert lookup.loc["SDN", "UN Regional Group"] == "Northern Africa"

    scores, peak, peak_month = selected_window(
        {"june": 6.6, "july": 2.0, "august": 2.0}, [6, 7, 8]
    )
    assert scores == [6.6, 2.0, 2.0] and peak == 6.6 and peak_month == "June"
    cluster = {
        "hazard_type": "WF",
        "country_details": {"name": "Test Country Cluster", "iso3": "TST"},
        "june": 20.0,
        "july": 20.0,
        "august": 20.0,
    }
    assert build_hazard_peaks([(0, [], [cluster])], [6, 7, 8]).empty

    sample = pd.DataFrame([
        {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Western Africa", "Country": "A", "ISO3": "AAA", "Hazard": "DR", "Hazard Label": "Drought", "Peak Score": 7.0, "Peak Month": "June", "Risk Category": "Very High", "Months in Risk Category": 1, "High or Very High Months": 1},
        {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Western Africa", "Country": "D", "ISO3": "DDD", "Hazard": "DR", "Hazard Label": "Drought", "Peak Score": 6.8, "Peak Month": "June", "Risk Category": "Very High", "Months in Risk Category": 1, "High or Very High Months": 1},
        {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Western Africa", "Country": "E", "ISO3": "EEE", "Hazard": "DR", "Hazard Label": "Drought", "Peak Score": 6.7, "Peak Month": "August", "Risk Category": "Very High", "Months in Risk Category": 1, "High or Very High Months": 1},
        {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Western Africa", "Country": "B", "ISO3": "BBB", "Hazard": "DR", "Hazard Label": "Drought", "Peak Score": 6.0, "Peak Month": "July", "Risk Category": "High", "Months in Risk Category": 2, "High or Very High Months": 2},
        {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Western Africa", "Country": "C", "ISO3": "CCC", "Hazard": "DR", "Hazard Label": "Drought", "Peak Score": 4.0, "Peak Month": "August", "Risk Category": "Medium", "Months in Risk Category": 3, "High or Very High Months": 0},
    ])
    selection = select_hazard_watchlist(sample)
    assert list(selection["Country"]) == ["A", "D", "E", "B"]
    presentation = build_presentation_watchlist(selection)
    assert list(presentation[presentation["Colour"] == "Red"]["Country"]) == ["A", "D", "E"]
    assert list(presentation[presentation["Colour"] == "Orange"]["Country"]) == ["B"]
    assert len(presentation_display_lines(presentation[presentation["Colour"] == "Red"])) == 1

    multi_hazard = pd.concat([
        sample,
        pd.DataFrame([
            {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Eastern Africa", "Country": "F", "ISO3": "FFF", "Hazard": "WF", "Hazard Label": "Wildfire", "Peak Score": 40.0, "Peak Month": "June", "Risk Category": "Very High", "Months in Risk Category": 1, "High or Very High Months": 1},
            {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Eastern Africa", "Country": "G", "ISO3": "GGG", "Hazard": "WF", "Hazard Label": "Wildfire", "Peak Score": 30.0, "Peak Month": "July", "Risk Category": "Very High", "Months in Risk Category": 1, "High or Very High Months": 1},
            {"Region Code": 0, "Region": "Africa", "Group": "Drought/Wildfire", "UN Regional Group": "Eastern Africa", "Country": "H", "ISO3": "HHH", "Hazard": "WF", "Hazard Label": "Wildfire", "Peak Score": 20.0, "Peak Month": "August", "Risk Category": "Very High", "Months in Risk Category": 1, "High or Very High Months": 1},
        ]),
    ], ignore_index=True)
    presentation = build_presentation_watchlist(select_hazard_watchlist(multi_hazard))
    red = presentation[presentation["Colour"] == "Red"]
    assert len(red) == PRESENTATION_COUNTRY_LIMIT
    assert set(red["Country"]) & {"A", "D", "E"}
    assert set(red["Country"]) & {"F", "G", "H"}

    no_very_high = sample[~sample["Country"].isin(["A", "D", "E"])]
    selection = select_hazard_watchlist(no_very_high)
    assert list(selection["Country"]) == ["B", "C"]
    assert list(selection["Risk Category"]) == ["High", "Medium"]
    print("Self-check passed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an IFRC GO four-hazard seasonal DREF watchlist.",
    )
    parser.add_argument(
        "--region", "-r", default="all", choices=["all", "0", "1", "2", "3", "4"],
        help="all (default), 0=Africa, 1=Americas, 2=Asia-Pacific, 3=Europe, 4=MENA",
    )
    parser.add_argument(
        "--months", "-m", type=int, nargs=3,
        metavar=("M1", "M2", "M3"),
        help="Three-month watch window; defaults to the window ending in --analysis-date",
    )
    parser.add_argument(
        "--analysis-date",
        default=date.today().isoformat(),
        help="Analysis date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--output", "-o", default="console", choices=["console", "excel", "csv", "markdown", "all"],
        help="console (default), excel, csv, markdown, or all",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/risk_scores",
        help="Folder for saved files (default: outputs/risk_scores)",
    )
    parser.add_argument(
        "--monthly-folder",
        action="store_true",
        help="Write files inside a YYYY-MM_Month folder based on --analysis-date",
    )
    parser.add_argument("--self-check", action="store_true", help="Run checks without calling the APIs")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_check:
        run_self_check()
        return

    try:
        analysis_date = date.fromisoformat(args.analysis_date)
    except ValueError:
        print("--analysis-date must use YYYY-MM-DD format.", file=sys.stderr)
        sys.exit(2)

    months = args.months or rolling_months(analysis_date)
    if any(not 1 <= month <= 12 for month in months):
        print("Months must be integers from 1 to 12.", file=sys.stderr)
        sys.exit(2)

    regions = list(REGION_NAMES) if args.region == "all" else [int(args.region)]
    regional_records = []
    for region in regions:
        risk_records, wildfire_records = fetch_region_records(region)
        regional_records.append((region, risk_records, wildfire_records))

    peaks = add_un_m49_groups(build_hazard_peaks(regional_records, months))
    selected = select_hazard_watchlist(peaks)
    presentation = build_presentation_watchlist(selected)
    print_report(presentation, months)

    if args.output in ("excel", "all", "csv", "markdown"):
        output_dir = Path(args.output_dir)
        if args.monthly_folder:
            output_dir /= analysis_date.strftime("%Y-%m_%B")
        output_dir.mkdir(parents=True, exist_ok=True)
        if args.output in ("excel", "all"):
            export_excel(presentation, selected, peaks, regions, months, output_dir)
        if args.output in ("csv", "all"):
            export_csv(peaks, regions, months, output_dir)
        if args.output in ("markdown", "all"):
            export_markdown(presentation, regions, months, output_dir)


if __name__ == "__main__":
    main()
