# INFORM DREF Prioritization Tool

Queries the [INFORM Risk Score API](https://go-risk.northeurope.cloudapp.azure.com/api/v1/risk-score/)
and classifies countries into DREF risk tiers for any region and any 3-month window.

---

## Setup

```bash
pip install -r requirements.txt
```

---

## Usage

```
python dref_analysis.py --region REGION --months M1 M2 M3 [--output FORMAT]
```

### Arguments

| Argument | Default | Description |
|---|---|---|
| `--region` / `-r` | `0` | Region code (see below) |
| `--months` / `-m` | `3 4 5` | Three months (1–12) |
| `--hazards` | `DR FL TC` | Hazard types to include |
| `--output` / `-o` | `console` | Output: `console`, `csv`, `excel`, `all` |
| `--output-dir` | `.` | Folder for saved files |
| `--show-medium` | off | Also show Medium-risk countries |

### Region Codes

| Code | Region |
|---|---|
| `0` | Africa |
| `1` | Americas |
| `2` | Asia-Pacific |
| `3` | Europe |
| `4` | MENA |

---

## INFORM Risk Classification

| Score | Class | DREF Implication |
|---|---|---|
| ≥ 6.5 | 🔴 Very High | **Strong case for DREF** |
| 5.0–6.4 | 🟠 High | **DREF probable** |
| 3.0–4.9 | 🟡 Medium | Monitor closely |
| < 3.0 | 🟢 Low | Unlikely |

Classification is based on the **peak monthly score** within the chosen 3-month window.
The MAM average is also shown for trend context.

---

## Examples

### Africa — March / April / May (console)
```bash
python dref_analysis.py --region 0 --months 3 4 5
```

### Africa — June / July / August (save to Excel)
```bash
python dref_analysis.py --region 0 --months 6 7 8 --output excel
```

### Americas — September / October / November (all outputs)
```bash
python dref_analysis.py --region 1 --months 9 10 11 --output all
```

### Asia-Pacific — December / January / February (floods + TC only)
```bash
python dref_analysis.py --region 2 --months 12 1 2 --hazards FL TC
```

### MENA — Drought only, March–May, show Medium too
```bash
python dref_analysis.py --region 4 --months 3 4 5 --hazards DR --show-medium
```

---

## Output Files

When `--output excel` or `--output all` is used, an `.xlsx` file is saved with two sheets:
- **DREF Candidates** — only Very High and High countries
- **All Countries** — complete dataset

When `--output csv` or `--output all`, two `.csv` files are saved:
- `dref_candidates_<region>_<months>.csv`
- `all_countries_<region>_<months>.csv`

Files are saved to `--output-dir` (default: current directory).

---

## Hazard Notes

- **DR** = Drought (also proxy for Wildfire — no standalone wildfire index in INFORM)
- **FL** = Flood
- **TC** = Tropical Cyclone
