# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "requests>=2.28",
#   "pandas>=2.0",
#   "openpyxl>=3.1",
# ]
# ///
"""Build a current WHO/CDC disease-outbreak watchlist for DREF discussion.

This is an event watch as of a stated date, not an INFORM score or a forecast.
WHO Very High / CDC Level 4 form the first Red tier. WHO High / CDC Level 3
form the Red fallback and the normal Orange tier. WHO Moderate, explicit
sub-national WHO risk, and CDC Level 2 are Orange-only fallbacks. CDC Level 1
never qualifies for the presentation.

Run, for example:
    uv run disease_outbreak_analysis.py --as-of 2026-08-05 --output all
    uv run disease_outbreak_analysis.py --self-check
"""

import argparse
import json
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

import pandas as pd
import requests

from dref_analysis import REGION_NAMES, load_un_m49_lookup


WHO_DON_URL = "https://www.who.int/api/emergencies/diseaseoutbreaknews"
WHO_COUNTRY_URL = "https://www.who.int/api/whoreference/countries"
CDC_RSS_URL = "https://wwwnc.cdc.gov/travel/rss/notices.xml"
IFRC_COUNTRY_URL = "https://goadmin.ifrc.org/api/v2/country/"
WHO_ITEM_BASE_URL = "https://www.who.int/emergencies/disease-outbreak-news/item"
WHO_LOOKBACK_DAYS = 365
WHO_ACTIVE_RECENCY_DAYS = 120
PRESENTATION_COUNTRY_LIMIT = 2

BAND_ORDER = {"A": 1, "B": 2, "C": 3, "D": 4}
WHO_RISK_BAND = {"Very High": "A", "High": "B", "Moderate": "C", "Low": "D"}
WHO_RISK_ORDER = {"": 0, "Not stated": 0, "Low": 1, "Moderate": 2, "High": 3, "Very High": 4}
CDC_LEVEL_BAND = {4: "A", 3: "B", 2: "C", 1: "D"}
INACTIVE_PHRASES = (
    "no longer poses a public health risk",
    "no further related transmission is expected",
    "outbreak has been declared over",
    "outbreak was declared over",
    "declared the end of the outbreak",
)

# Only aliases needed when CDC wording differs from IFRC country names.
COUNTRY_ALIASES = {
    "BOL": ("bolivia",),
    "COD": ("democratic republic of the congo", "dr congo", "drc"),
    "COG": ("republic of the congo", "congo brazzaville"),
    "CIV": ("cote d ivoire", "ivory coast"),
    "GBR": ("united kingdom", "uk"),
    "IRN": ("iran",),
    "KOR": ("south korea", "republic of korea"),
    "LAO": ("laos", "lao people s democratic republic"),
    "PRK": ("north korea", "democratic people s republic of korea"),
    "RUS": ("russia", "russian federation"),
    "SWZ": ("eswatini", "swaziland"),
    "SYR": ("syria", "syrian arab republic"),
    "TZA": ("tanzania", "united republic of tanzania"),
    "USA": ("united states", "usa", "u s"),
    "VEN": ("venezuela",),
    "VNM": ("vietnam", "viet nam"),
}

DISEASE_KEYWORDS = (
    "ebola", "cholera", "measles", "dengue", "yellow fever", "chikungunya",
    "malaria", "mpox", "nipah", "polio", "diphtheria", "meningococcal",
    "hepatitis a", "typhoid", "hantavirus", "marburg", "lassa fever",
    "avian influenza", "influenza", "zika", "anthrax", "ciguatera",
)

SOURCE_COLUMNS = [
    "Source", "Source Event ID", "Report Date", "Disease", "Disease Key",
    "Country", "ISO3", "Region Code", "Region", "UN Regional Group",
    "WHO Risk", "WHO Risk Scope", "CDC Level", "Active Status", "Priority Band",
    "Presentation Eligible", "Evidence", "Source URL", "Source Record Title",
    "Retrieved At",
]


def normalize_text(value: str) -> str:
    """Return lower-case, accent-free words for conservative text matching."""
    ascii_text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_text.lower()).strip()


def html_to_text(value: str | None) -> str:
    """Remove simple WHO HTML markup and normalize whitespace."""
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def canonical_disease(value: str) -> str:
    """Return a small stable key used only to match WHO and CDC reports."""
    normalized = normalize_text(value)
    for keyword in DISEASE_KEYWORDS:
        if normalize_text(keyword) in normalized:
            return keyword.title()
    return normalized[:100] or "Unspecified event"


def fetch_response(url: str, params: dict | None = None) -> requests.Response:
    """Fetch one required public source and fail without producing partial results."""
    try:
        response = requests.get(url, params=params, timeout=60)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        raise RuntimeError(f"Source request failed for {url}: {exc}") from exc


def fetch_json(url: str, params: dict | None = None) -> object:
    """Fetch and decode one JSON source."""
    try:
        return fetch_response(url, params).json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(f"Source returned invalid JSON: {url}") from exc


def fetch_ifrc_countries() -> pd.DataFrame:
    """Return current ISO3-to-IFRC-region reference data."""
    payload = fetch_json(IFRC_COUNTRY_URL, {"limit": 9999})
    records = payload.get("results", []) if isinstance(payload, dict) else []
    rows = [
        {
            "ISO3": str(item.get("iso3") or "").upper(),
            "Country": str(item.get("name") or "").strip(),
            "Region Code": item.get("region"),
        }
        for item in records
        if item.get("iso3") and item.get("region") in REGION_NAMES and not item.get("is_deprecated")
    ]
    result = pd.DataFrame(rows).drop_duplicates("ISO3")
    if result.empty:
        raise RuntimeError("IFRC country endpoint returned no usable country records.")
    result["Region"] = result["Region Code"].map(REGION_NAMES)
    return result


def fetch_who_country_lookup() -> dict[str, dict]:
    """Map WHO regionscountries taxonomy IDs to country name and ISO3."""
    payload = fetch_json(
        WHO_COUNTRY_URL,
        {
            "$expand": "WhoRegion($select=Title)",
            "$select": "Title,Code,regionscountries",
            "$filter": "regionscountries/Any()",
            "sf_culture": "en",
        },
    )
    records = payload.get("value", []) if isinstance(payload, dict) else []
    lookup = {}
    for item in records:
        iso3 = str(item.get("Code") or "").upper()
        country = str(item.get("Title") or "").strip()
        for taxonomy_id in item.get("regionscountries") or []:
            if iso3 and country:
                lookup[str(taxonomy_id)] = {"ISO3": iso3, "Country": country}
    if not lookup:
        raise RuntimeError("WHO country endpoint returned no usable taxonomy mappings.")
    return lookup


def fetch_who_reports(as_of: date) -> tuple[list[dict], list[dict]]:
    """Fetch recent WHO DON records and retain the latest update per event."""
    start = as_of - timedelta(days=WHO_LOOKBACK_DAYS)
    end = as_of + timedelta(days=1)
    base_params = {
            "sf_provider": "dynamicProvider372",
            "sf_culture": "en",
            "$orderby": "PublicationDateAndTime desc",
            "$expand": "EmergencyEvent",
            "$select": (
                "Title,OverrideTitle,regionscountries,ItemDefaultUrl,"
                "PublicationDateAndTime,Assessment,Summary,Overview,Response,EmergencyEvent"
            ),
            "$filter": (
                f"PublicationDateAndTime ge {start.isoformat()}T00:00:00Z and "
                f"PublicationDateAndTime lt {end.isoformat()}T00:00:00Z"
            ),
            "$top": 100,
    }
    records = []
    skip = 0
    while True:
        payload = fetch_json(WHO_DON_URL, {**base_params, "$skip": skip})
        page = payload.get("value", []) if isinstance(payload, dict) else []
        records.extend(page)
        if len(page) < base_params["$top"]:
            break
        skip += len(page)

    latest = {}
    for item in records:
        event = item.get("EmergencyEvent") or {}
        event_id = str(event.get("EventId") or "").strip()
        fallback = f"{canonical_disease(event.get('Title') or item.get('Title') or '')}|{','.join(sorted(item.get('regionscountries') or []))}"
        key = event_id or fallback
        report_date = str(item.get("PublicationDateAndTime") or "")
        if key not in latest or report_date > str(latest[key].get("PublicationDateAndTime") or ""):
            latest[key] = item
    return records, list(latest.values())


def risk_matches(text: str) -> list[tuple[str, str]]:
    """Find explicit risk-level phrases and return the level with its sentence."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    matches = []
    level_pattern = r"(very high|high|moderate|low)"
    patterns = (
        rf"\brisk\b.{{0,180}}?\b(?:assessed|assessment|remains|is|was|to be)\b.{{0,60}}?\b{level_pattern}\b",
        rf"\b(?:assessed|assessment)\b.{{0,80}}?\brisk\b.{{0,80}}?\b{level_pattern}\b",
        rf"\b{level_pattern}\b.{{0,60}}?\b(?:public health )?risk\b",
    )
    for sentence in sentences:
        lowered = normalize_text(sentence)
        if "risk" not in lowered:
            continue
        for pattern in patterns:
            match = re.search(pattern, lowered)
            if match:
                level = next(group for group in match.groups() if group in {"very high", "high", "moderate", "low"})
                matches.append((level.title(), sentence.strip()))
                break
    return matches


def extract_who_risk(assessment: str, country: str, country_count: int) -> tuple[str, str, str]:
    """Extract an explicit WHO risk level together with its geographic scope."""
    text = html_to_text(assessment)
    if not text:
        return "Not stated", "", "WHO assessment did not state a usable risk level."

    country_name = normalize_text(country)
    candidates = []
    for level, sentence in risk_matches(text):
        normalized = normalize_text(sentence)
        mentions_country = country_name in normalized
        mentions_national = "national level" in normalized or "national risk" in normalized
        mentions_subnational = "sub national" in normalized
        regional_only = ("regional level" in normalized or "global level" in normalized) and not mentions_national
        if mentions_subnational and (mentions_country or country_count == 1):
            candidates.append((2, WHO_RISK_ORDER[level], level, "Sub-national", sentence))
        elif mentions_national and (mentions_country or country_count == 1):
            candidates.append((4, WHO_RISK_ORDER[level], level, "National", sentence))
        elif mentions_country and not regional_only:
            candidates.append((3, WHO_RISK_ORDER[level], level, "Country-specific", sentence))
        elif country_count == 1 and "overall public health risk" in normalized and not regional_only:
            candidates.append((1, WHO_RISK_ORDER[level], level, "Overall", sentence))

    if not candidates:
        return "Not stated", "", "WHO assessment requires manual country-level review."
    _, _, level, scope, sentence = max(candidates)
    return level, scope, sentence


def who_priority_band(risk: str, scope: str) -> str:
    """Keep explicit sub-national concern Orange-only while preserving national tiers."""
    if scope == "Sub-national":
        return "C" if risk in {"Very High", "High", "Moderate"} else "D"
    return WHO_RISK_BAND.get(risk, "")


def who_active_status(item: dict, as_of: date) -> tuple[str, str]:
    """Exclude reports whose latest text explicitly says the event is over."""
    text = normalize_text(" ".join(
        html_to_text(item.get(field))
        for field in ("Assessment", "Summary", "Overview", "Response")
    ))
    for phrase in INACTIVE_PHRASES:
        if normalize_text(phrase) in text:
            return "Inactive/resolved", phrase
    try:
        report_date = date.fromisoformat(str(item.get("PublicationDateAndTime") or "")[:10])
    except ValueError:
        return "Needs current confirmation", "WHO report date is missing or invalid."
    age_days = (as_of - report_date).days
    if age_days > WHO_ACTIVE_RECENCY_DAYS:
        return (
            "Needs current confirmation",
            f"Latest WHO report is {age_days} days old; automatic limit is {WHO_ACTIVE_RECENCY_DAYS} days.",
        )
    return "Active/ongoing", "Latest WHO report contains no explicit closure statement."


def who_source_rows(
    reports: list[dict], who_countries: dict[str, dict], ifrc_countries: pd.DataFrame,
    as_of: date, retrieved_at: str,
) -> list[dict]:
    """Create one WHO source row per mapped report-country."""
    ifrc_iso3 = set(ifrc_countries["ISO3"])
    rows = []
    for item in reports:
        mapped = [who_countries[key] for key in item.get("regionscountries") or [] if key in who_countries]
        mapped = [country for country in mapped if country["ISO3"] in ifrc_iso3]
        event = item.get("EmergencyEvent") or {}
        disease = str(event.get("Title") or item.get("OverrideTitle") or item.get("Title") or "Unspecified event")
        record_title = str(item.get("Title") or "")
        broad_event = bool(re.search(r"\bglobal\b|\bmulti (?:countries|country|locations|location)", normalize_text(record_title)))
        report_date = str(item.get("PublicationDateAndTime") or "")[:10]
        event_id = str(event.get("EventId") or item.get("ItemDefaultUrl") or "")
        active_status, status_basis = who_active_status(item, as_of)
        item_path = str(item.get("ItemDefaultUrl") or "")
        source_url = f"{WHO_ITEM_BASE_URL}{item_path}" if item_path else WHO_DON_URL

        if not mapped:
            rows.append({
                "Source": "WHO DON", "Source Event ID": event_id, "Report Date": report_date,
                "Disease": disease, "Disease Key": canonical_disease(disease), "Country": "",
                "ISO3": "", "WHO Risk": "Not stated", "WHO Risk Scope": "", "CDC Level": pd.NA,
                "Active Status": active_status, "Priority Band": "",
                "Presentation Eligible": False,
                "Evidence": f"No IFRC country mapping. {status_basis}", "Source URL": source_url,
                "Source Record Title": str(item.get("Title") or ""), "Retrieved At": retrieved_at,
            })
            continue

        for country in mapped:
            risk, risk_scope, risk_evidence = extract_who_risk(
                item.get("Assessment") or "", country["Country"], len(mapped)
            )
            band = who_priority_band(risk, risk_scope)
            eligible = active_status == "Active/ongoing" and band in {"A", "B", "C"} and not broad_event
            rows.append({
                "Source": "WHO DON", "Source Event ID": event_id, "Report Date": report_date,
                "Disease": disease, "Disease Key": canonical_disease(disease),
                "Country": country["Country"], "ISO3": country["ISO3"], "WHO Risk": risk,
                "WHO Risk Scope": risk_scope, "CDC Level": pd.NA,
                "Active Status": active_status, "Priority Band": band,
                "Presentation Eligible": eligible,
                "Evidence": (
                    f"{risk_evidence} Status: {status_basis}"
                    + (" Broad global/multi-location report excluded from automatic selection." if broad_event else "")
                ),
                "Source URL": source_url, "Source Record Title": record_title, "Retrieved At": retrieved_at,
            })
    return rows


def country_aliases(ifrc_countries: pd.DataFrame) -> list[tuple[str, str, str]]:
    """Return longest-first aliases for CDC notice matching."""
    aliases = []
    for row in ifrc_countries.itertuples(index=False):
        iso3 = row.ISO3
        names = {normalize_text(row.Country), *(normalize_text(name) for name in COUNTRY_ALIASES.get(iso3, ()))}
        for name in names:
            if name and not (iso3 == "COG" and name == "congo"):
                aliases.append((name, iso3, row.Country))
    return sorted(aliases, key=lambda item: len(item[0]), reverse=True)


def extract_cdc_countries(text: str, aliases: list[tuple[str, str, str]]) -> list[dict]:
    """Find countries in a CDC notice without confusing Niger/Nigeria-style names."""
    normalized = f" {normalize_text(text)} "
    found = {}
    occupied: list[tuple[int, int]] = []
    for alias, iso3, country in aliases:
        for match in re.finditer(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", normalized):
            span = match.span()
            if any(span[0] >= start and span[1] <= end for start, end in occupied):
                continue
            found[iso3] = {"ISO3": iso3, "Country": country}
            occupied.append(span)
    return list(found.values())


def cdc_disease(title: str) -> str:
    """Remove the CDC level prefix and obvious country suffix from a title."""
    value = re.sub(r"^Level\s+[1-4]\s*[-–—]\s*", "", title, flags=re.IGNORECASE)
    value = re.split(r"\s+in\s+", value, maxsplit=1, flags=re.IGNORECASE)[0]
    return value.strip() or title


def cdc_source_rows(
    as_of: date, ifrc_countries: pd.DataFrame, retrieved_at: str,
) -> tuple[list[dict], list[dict]]:
    """Parse the current CDC Travel Health Notice RSS feed."""
    response = fetch_response(CDC_RSS_URL)
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        raise RuntimeError("CDC Travel Health Notice feed returned invalid XML.") from exc

    aliases = country_aliases(ifrc_countries)
    rows = []
    raw_notices = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        match = re.match(r"Level\s+([1-4])\s*[-–—]", title, flags=re.IGNORECASE)
        if not match:
            continue
        level = int(match.group(1))
        description = (item.findtext("description") or "").strip()
        source_url = (item.findtext("link") or "").strip()
        report_datetime = parsedate_to_datetime(item.findtext("pubDate") or "")
        report_date = report_datetime.date()
        if report_date > as_of:
            continue

        raw_notices.append({
            "title": title,
            "description": description,
            "link": source_url,
            "publication_date": report_datetime.isoformat(),
        })

        disease = cdc_disease(title)
        broad_notice = bool(re.search(r"\bglobal\b|\bsub saharan\b", normalize_text(title)))
        countries = extract_cdc_countries(f"{title} {description}", aliases) if level >= 2 and not broad_notice else []

        eligible = level in {2, 3, 4} and not broad_notice
        if not countries:
            rows.append({
                "Source": "CDC THN", "Source Event ID": source_url, "Report Date": report_date.isoformat(),
                "Disease": disease, "Disease Key": canonical_disease(disease), "Country": "", "ISO3": "",
                "WHO Risk": "", "WHO Risk Scope": "", "CDC Level": level,
                "Active Status": "Active/ongoing",
                "Priority Band": CDC_LEVEL_BAND[level], "Presentation Eligible": False,
                "Evidence": (
                    f"CDC Level {level}; broad/global or Level 1 notice excluded from presentation."
                    if broad_notice or level == 1 else f"CDC Level {level}; country could not be mapped."
                ),
                "Source URL": source_url, "Source Record Title": title, "Retrieved At": retrieved_at,
            })
            continue

        for country in countries:
            rows.append({
                "Source": "CDC THN", "Source Event ID": source_url, "Report Date": report_date.isoformat(),
                "Disease": disease, "Disease Key": canonical_disease(disease),
                "Country": country["Country"], "ISO3": country["ISO3"], "WHO Risk": "",
                "WHO Risk Scope": "", "CDC Level": level, "Active Status": "Active/ongoing",
                "Priority Band": CDC_LEVEL_BAND[level], "Presentation Eligible": eligible,
                "Evidence": f"CDC Level {level} current Travel Health Notice.",
                "Source URL": source_url, "Source Record Title": title, "Retrieved At": retrieved_at,
            })
    return rows, raw_notices


def attach_geography(source_records: pd.DataFrame, ifrc_countries: pd.DataFrame) -> pd.DataFrame:
    """Attach IFRC region and the existing versioned UN M49 grouping."""
    if source_records.empty:
        return pd.DataFrame(columns=SOURCE_COLUMNS)
    result = source_records.merge(
        ifrc_countries[["ISO3", "Region Code", "Region"]], on="ISO3", how="left", validate="many_to_one"
    )
    result = result.merge(
        load_un_m49_lookup()[["ISO3", "UN Regional Group"]], on="ISO3", how="left", validate="many_to_one"
    )
    result["UN Regional Group"] = result["UN Regional Group"].fillna("Unmapped in UN M49")
    for column in SOURCE_COLUMNS:
        if column not in result:
            result[column] = ""
    return result[SOURCE_COLUMNS].sort_values(
        ["Source", "Report Date", "Source Record Title", "Country"], ascending=[True, False, True, True]
    ).reset_index(drop=True)


def build_candidates(source_records: pd.DataFrame, regions: list[int]) -> pd.DataFrame:
    """Merge active WHO/CDC evidence without adding their category values."""
    active = source_records[
        source_records["Active Status"].eq("Active/ongoing")
        & source_records["ISO3"].ne("")
        & source_records["Region Code"].isin(regions)
    ].copy()
    eligible = active[active["Presentation Eligible"]]
    if eligible.empty:
        return pd.DataFrame()

    eligible_keys = set(zip(eligible["ISO3"], eligible["Disease Key"]))
    active = active[
        active.apply(lambda row: (row["ISO3"], row["Disease Key"]) in eligible_keys, axis=1)
    ]
    rows = []
    for (iso3, disease_key), group in active.groupby(["ISO3", "Disease Key"], sort=False):
        qualifying = group[group["Presentation Eligible"]]
        best_band = min(qualifying["Priority Band"], key=BAND_ORDER.get)
        who_rows = group[group["Source"] == "WHO DON"].copy()
        if who_rows.empty:
            who_risk = ""
            who_scope = ""
        else:
            who_rows["_who_order"] = who_rows["WHO Risk"].map(WHO_RISK_ORDER).fillna(0)
            best_who = who_rows.sort_values("_who_order", ascending=False).iloc[0]
            who_risk = best_who["WHO Risk"]
            who_scope = best_who["WHO Risk Scope"]
        cdc_values = pd.to_numeric(group["CDC Level"], errors="coerce").dropna()
        latest_date = max(group["Report Date"])
        rows.append({
            "Region Code": int(group.iloc[0]["Region Code"]),
            "Region": group.iloc[0]["Region"],
            "UN Regional Group": group.iloc[0]["UN Regional Group"],
            "Country": group.iloc[0]["Country"],
            "ISO3": iso3,
            "Disease": group.iloc[0]["Disease"],
            "Disease Key": disease_key,
            "Priority Band": best_band,
            "WHO Risk": who_risk,
            "WHO Risk Scope": who_scope,
            "CDC Level": int(cdc_values.max()) if not cdc_values.empty else pd.NA,
            "Sources": "; ".join(sorted(group["Source"].unique())),
            "Source Count": group["Source"].nunique(),
            "Latest Report Date": latest_date,
            "Evidence": " | ".join(dict.fromkeys(group["Evidence"])),
            "Source URLs": " | ".join(dict.fromkeys(group["Source URL"])),
        })
    return pd.DataFrame(rows)


def ranked_countries(candidates: pd.DataFrame) -> pd.DataFrame:
    """Rank one primary disease per country using source strength and freshness."""
    if candidates.empty:
        return candidates
    ranked = candidates.copy()
    ranked["_who_order"] = ranked["WHO Risk"].map(WHO_RISK_ORDER).fillna(0)
    ranked["_cdc_order"] = pd.to_numeric(ranked["CDC Level"], errors="coerce").fillna(0)
    ranked["_date"] = pd.to_datetime(ranked["Latest Report Date"], errors="coerce")
    ranked = ranked.sort_values(
        ["Source Count", "_who_order", "_cdc_order", "_date", "Country"],
        ascending=[False, False, False, False, True],
    ).drop_duplicates("ISO3")
    return ranked.drop(columns=["_who_order", "_cdc_order", "_date"])


def select_presentation(candidates: pd.DataFrame, regions: list[int]) -> pd.DataFrame:
    """Select at most two Red and two Orange countries per IFRC region."""
    if candidates.empty:
        return pd.DataFrame()
    rows = []
    for region_code in regions:
        regional = candidates[candidates["Region Code"] == region_code]
        bands = {band: regional[regional["Priority Band"] == band] for band in ("A", "B", "C")}
        if not bands["A"].empty:
            red_pool = bands["A"]
            orange_pool = bands["B"] if not bands["B"].empty else bands["C"]
            red_basis = "WHO Very High and/or CDC Level 4"
        elif not bands["B"].empty:
            red_pool = bands["B"]
            orange_pool = bands["C"]
            red_basis = "WHO High and/or CDC Level 3 (no Band A country)"
        else:
            red_pool = regional.iloc[0:0]
            orange_pool = bands["C"]
            red_basis = ""

        red = ranked_countries(red_pool).head(PRESENTATION_COUNTRY_LIMIT)
        red_iso3 = set(red["ISO3"])
        orange = ranked_countries(orange_pool[~orange_pool["ISO3"].isin(red_iso3)]).head(
            PRESENTATION_COUNTRY_LIMIT
        )
        for colour, selected, basis in (
            ("Red", red, red_basis),
            ("Orange", orange, "Next eligible WHO/CDC priority band"),
        ):
            for rank, (_, item) in enumerate(selected.iterrows(), start=1):
                country_events = candidates[candidates["ISO3"] == item["ISO3"]].sort_values(
                    "Priority Band", key=lambda values: values.map(BAND_ORDER)
                )
                diseases = "; ".join(dict.fromkeys(country_events["Disease"]))
                rows.append({
                    "Region Code": region_code,
                    "Region": REGION_NAMES[region_code],
                    "UN Regional Group": item["UN Regional Group"],
                    "Colour": colour,
                    "DREF Status": (
                        "DREF highly probable" if colour == "Red" else "DREF uncertain/probable"
                    ),
                    "Presentation Rank": rank,
                    "Country": item["Country"],
                    "ISO3": item["ISO3"],
                    "Disease": diseases,
                    "Priority Band": item["Priority Band"],
                    "WHO Risk": item["WHO Risk"],
                    "WHO Risk Scope": item["WHO Risk Scope"],
                    "CDC Level": item["CDC Level"],
                    "Sources": item["Sources"],
                    "Latest Report Date": item["Latest Report Date"],
                    "Evidence": item["Evidence"],
                    "Source URLs": item["Source URLs"],
                    "Selection Basis": basis,
                })
    return pd.DataFrame(rows)


def evidence_label(row: pd.Series) -> str:
    """Build a compact source label for console and Markdown."""
    parts = []
    if row.get("WHO Risk") and row["WHO Risk"] != "Not stated":
        scope = str(row.get("WHO Risk Scope") or "").lower()
        scope_text = f" ({scope})" if scope else ""
        parts.append(f"WHO {row['WHO Risk']}{scope_text}")
    if pd.notna(row.get("CDC Level")):
        parts.append(f"CDC Level {int(row['CDC Level'])}")
    return "; ".join(parts) or "eligible WHO evidence"


def presentation_lines(subset: pd.DataFrame, markdown: bool = False) -> list[str]:
    """Group selected countries that share the same UN M49 subregion."""
    lines = []
    ordered = subset.sort_values("Presentation Rank")
    for un_group in ordered["UN Regional Group"].drop_duplicates():
        regional = ordered[ordered["UN Regional Group"] == un_group]
        details = []
        for _, row in regional.iterrows():
            country = f"**{row['Country']}**" if markdown else row["Country"]
            date_text = pd.to_datetime(row["Latest Report Date"]).strftime("%d %B %Y").lstrip("0")
            details.append(
                f"{country} — {row['Disease']}; {evidence_label(row)} (updated {date_text})"
            )
        if len(regional) > 1:
            lines.append(f"{un_group} ({' | '.join(details)})")
        else:
            lines.append(f"{details[0]} — {un_group}")
    return lines


def print_report(presentation: pd.DataFrame, regions: list[int], as_of: date) -> None:
    """Print the current outbreak presentation shortlist."""
    print(f"\n{'=' * 78}")
    print("CURRENT AND EMERGING DISEASE OUTBREAK WATCH")
    print(f"As of: {as_of:%d %B %Y}")
    print(f"{'=' * 78}")
    print("Red: DREF highly probable | Orange: DREF uncertain/probable")
    print("WHO/CDC outbreak-priority screening; not an INFORM index or disease forecast.\n")
    for region_code in regions:
        print(REGION_NAMES[region_code].upper())
        regional = presentation[presentation["Region Code"] == region_code] if not presentation.empty else presentation
        for colour in ("Red", "Orange"):
            subset = regional[regional["Colour"] == colour] if not regional.empty else regional
            print(f"  {colour}")
            if subset.empty:
                print("    No eligible country.")
            else:
                for rank, line in enumerate(presentation_lines(subset), start=1):
                    print(f"    {rank}. {line}")
        print()


def build_processed_output(candidates: pd.DataFrame, presentation: pd.DataFrame) -> pd.DataFrame:
    """Add shortlist status to the complete processed candidate table."""
    if candidates.empty:
        return candidates
    selection_columns = ["ISO3", "Colour", "DREF Status", "Presentation Rank", "Selection Basis"]
    if presentation.empty:
        result = candidates.copy()
        for column in selection_columns[1:]:
            result[column] = ""
        return result
    selection = presentation[selection_columns].drop_duplicates("ISO3")
    return candidates.merge(selection, on="ISO3", how="left", validate="many_to_one").fillna({
        "Colour": "Not shortlisted",
        "DREF Status": "",
        "Presentation Rank": "",
        "Selection Basis": "",
    })


def export_markdown(presentation: pd.DataFrame, regions: list[int], as_of: date, output_dir: Path) -> Path:
    """Write the disease-outbreak presentation shortlist."""
    lines = [
        "# Current and emerging disease outbreak watch",
        "",
        f"**As of:** {as_of:%d %B %Y}",
        "",
        "- Red: DREF highly probable",
        "- Orange: DREF uncertain/probable",
        "- WHO/CDC outbreak-priority screening; not an INFORM index or disease forecast.",
        "- CDC Level 2 is Orange-only; CDC Level 1 is excluded from presentation selection.",
    ]
    for region_code in regions:
        lines.extend(["", f"## {REGION_NAMES[region_code]}"])
        regional = presentation[presentation["Region Code"] == region_code] if not presentation.empty else presentation
        for colour in ("Red", "Orange"):
            status = "DREF highly probable" if colour == "Red" else "DREF uncertain/probable"
            lines.extend(["", f"**{colour} — {status}**"])
            subset = regional[regional["Colour"] == colour] if not regional.empty else regional
            if subset.empty:
                lines.append("- No eligible country.")
            else:
                lines.extend(f"{rank}. {line}" for rank, line in enumerate(presentation_lines(subset, True), 1))
    path = output_dir / f"disease_outbreak_watch_{as_of.isoformat()}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Markdown written: {path}")
    return path


def export_processed_csv(
    candidates: pd.DataFrame, presentation: pd.DataFrame, as_of: date, output_dir: Path,
) -> Path:
    """Write one processed candidate table with its shortlist status."""
    path = output_dir / f"disease_outbreak_processed_{as_of.isoformat()}.csv"
    build_processed_output(candidates, presentation).to_csv(path, index=False)
    print(f"Processed CSV written: {path}")
    return path


def export_raw_snapshot(
    raw_who_reports: list[dict], raw_cdc_notices: list[dict], as_of: date,
    retrieved_at: str, output_dir: Path,
) -> Path:
    """Preserve the unclassified WHO reports and CDC RSS entries in one JSON file."""
    path = output_dir / f"disease_outbreak_raw_{as_of.isoformat()}.json"
    payload = {
        "as_of": as_of.isoformat(),
        "retrieved_at": retrieved_at,
        "sources": {"who": WHO_DON_URL, "cdc": CDC_RSS_URL},
        "who_reports": raw_who_reports,
        "cdc_notices": raw_cdc_notices,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Raw JSON written: {path}")
    return path


def run_self_check() -> None:
    """Exercise the agreed priority ladder and text guards without network calls."""
    risk, scope, _ = extract_who_risk(
        "The risk in Testland was assessed as very high at the national level, high regionally, and low globally.",
        "Testland", 1,
    )
    assert risk == "Very High" and scope == "National"
    assert extract_who_risk(
        "There is an ongoing moderate sub-national risk in Testland.", "Testland", 1
    )[:2] == ("Moderate", "Sub-national")
    assert who_active_status(
        {"Assessment": "This outbreak no longer poses a public health risk."}, date(2026, 8, 5)
    )[0] == "Inactive/resolved"
    assert who_active_status(
        {"Assessment": "The event continues.", "PublicationDateAndTime": "2025-08-20T00:00:00Z"},
        date(2026, 8, 5),
    )[0] == "Needs current confirmation"
    assert CDC_LEVEL_BAND[4] == "A" and CDC_LEVEL_BAND[3] == "B"
    assert 2 in {2, 3, 4} and CDC_LEVEL_BAND[2] == "C"
    matched = extract_cdc_countries(
        "Democratic Republic of the Congo and the Democratic Republic of the Congo",
        [("democratic republic of the congo", "COD", "Democratic Republic of Congo"),
         ("congo", "COG", "Congo")],
    )
    assert [item["ISO3"] for item in matched] == ["COD"]

    sample = pd.DataFrame([
        {"Region Code": 0, "Region": "Africa", "UN Regional Group": "Middle Africa", "Country": "A", "ISO3": "AAA", "Disease": "Ebola", "Disease Key": "Ebola", "Priority Band": "A", "WHO Risk": "Very High", "WHO Risk Scope": "National", "CDC Level": 4, "Sources": "WHO DON; CDC THN", "Source Count": 2, "Latest Report Date": "2026-08-04", "Evidence": "A", "Source URLs": "a"},
        {"Region Code": 0, "Region": "Africa", "UN Regional Group": "Eastern Africa", "Country": "B", "ISO3": "BBB", "Disease": "Cholera", "Disease Key": "Cholera", "Priority Band": "B", "WHO Risk": "High", "WHO Risk Scope": "National", "CDC Level": 3, "Sources": "WHO DON", "Source Count": 1, "Latest Report Date": "2026-08-03", "Evidence": "B", "Source URLs": "b"},
        {"Region Code": 1, "Region": "Americas", "UN Regional Group": "South America", "Country": "C", "ISO3": "CCC", "Disease": "Yellow fever", "Disease Key": "Yellow Fever", "Priority Band": "B", "WHO Risk": "High", "WHO Risk Scope": "National", "CDC Level": pd.NA, "Sources": "WHO DON", "Source Count": 1, "Latest Report Date": "2026-08-02", "Evidence": "C", "Source URLs": "c"},
        {"Region Code": 1, "Region": "Americas", "UN Regional Group": "Central America", "Country": "D", "ISO3": "DDD", "Disease": "Dengue", "Disease Key": "Dengue", "Priority Band": "C", "WHO Risk": "Moderate", "WHO Risk Scope": "Sub-national", "CDC Level": pd.NA, "Sources": "WHO DON", "Source Count": 1, "Latest Report Date": "2026-08-01", "Evidence": "D", "Source URLs": "d"},
    ])
    selected = select_presentation(sample, [0, 1])
    assert selected[(selected["Region Code"] == 0) & (selected["Colour"] == "Red")].iloc[0]["Country"] == "A"
    assert selected[(selected["Region Code"] == 0) & (selected["Colour"] == "Orange")].iloc[0]["Country"] == "B"
    assert selected[(selected["Region Code"] == 1) & (selected["Colour"] == "Red")].iloc[0]["Country"] == "C"
    assert selected[(selected["Region Code"] == 1) & (selected["Colour"] == "Orange")].iloc[0]["Country"] == "D"
    print("Self-check passed.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a WHO/CDC disease-outbreak DREF watchlist.")
    parser.add_argument(
        "--region", "-r", default="all", choices=["all", "0", "1", "2", "3", "4"],
        help="all (default), 0=Africa, 1=Americas, 2=Asia-Pacific, 3=Europe, 4=MENA",
    )
    parser.add_argument(
        "--as-of", default=date.today().isoformat(),
        help="Analysis cut-off date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--output", "-o", default="console", choices=["console", "raw", "csv", "markdown", "all"],
        help="console (default), raw JSON, processed CSV, Markdown, or all three files",
    )
    parser.add_argument(
        "--output-dir", default="outputs/disease_outbreaks",
        help="Folder for saved files (default: outputs/disease_outbreaks)",
    )
    parser.add_argument("--self-check", action="store_true", help="Run checks without calling the sources")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_check:
        run_self_check()
        return
    try:
        as_of = date.fromisoformat(args.as_of)
    except ValueError:
        print("--as-of must use YYYY-MM-DD format.", file=sys.stderr)
        sys.exit(2)

    regions = list(REGION_NAMES) if args.region == "all" else [int(args.region)]
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        print("Fetching IFRC and WHO country references...")
        ifrc_countries = fetch_ifrc_countries()
        who_countries = fetch_who_country_lookup()
        print("Fetching WHO Disease Outbreak News...")
        raw_who_reports, who_reports = fetch_who_reports(as_of)
        print("Fetching CDC Travel Health Notices...")
        rows = who_source_rows(who_reports, who_countries, ifrc_countries, as_of, retrieved_at)
        cdc_rows, raw_cdc_notices = cdc_source_rows(as_of, ifrc_countries, retrieved_at)
        rows.extend(cdc_rows)
        source_records = attach_geography(pd.DataFrame(rows), ifrc_countries)
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    candidates = build_candidates(source_records, regions)
    presentation = select_presentation(candidates, regions)
    print(
        f"Fetched {len(who_reports)} latest WHO events and "
        f"{(source_records['Source'] == 'CDC THN').sum()} CDC source rows."
    )
    print_report(presentation, regions, as_of)

    if args.output in {"raw", "csv", "markdown", "all"}:
        output_dir = Path(args.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        if args.output in {"raw", "all"}:
            export_raw_snapshot(raw_who_reports, raw_cdc_notices, as_of, retrieved_at, output_dir)
        if args.output in {"csv", "all"}:
            export_processed_csv(candidates, presentation, as_of, output_dir)
        if args.output in {"markdown", "all"}:
            export_markdown(presentation, regions, as_of, output_dir)


if __name__ == "__main__":
    main()
