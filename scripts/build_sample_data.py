"""Generate the demo datasets in sample_data/ (deterministic values; re-run to rebuild them).

Usage: python scripts/build_sample_data.py

All issuers are fictional (D-006): "IRS" (Institutul Roman de Statistica) data is invented. The IRS consumer
price figures are derived from the *recorded* Eurostat HICP responses for Romania in tests/data/eurostat/
so the planted conflicts match the IRS Technical Note §3.1/§3.2 story:
- IPC (index, 2021=100) = HICP (2015=100) rebased on the 2021 average of the recorded series, plus small
  coverage noise. Against the Eurostat index on 2015=100 it differs only by the base year, so StatBrief
  must report a unit difference (I21 vs I15), never a value conflict (§3.2).
- IPC_VAR_AN (annual rate, %) = HICP annual rate within one rounding unit, except PLANTED_RATE_CONFLICTS,
  which differ by several tenths (weights/coverage, §3.1) and must surface as value conflicts.
Nothing invented is attributed to INS, Eurostat or the World Bank.
"""
import csv
import io
import json
import random
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "sample_data"
FIXTURES = ROOT / "tests" / "data" / "eurostat"
HICP_INDEX_FIXTURE = FIXTURES / "hicp_ro_i15_2021_2024.json"
HICP_RATE_FIXTURE = FIXTURES / "hicp_ro_rch_a_2023_2024.json"
SEED = 20241029
MONTHS = [f"{y}-{m:02d}" for y in (2023, 2024) for m in range(1, 13)]
QUARTERS = [f"{y}-Q{q}" for y in (2022, 2023, 2024) for q in range(1, 5)]
HEADER = ["luna", "judet", "indicator", "valoare", "um", "val_adj_x"]
NATIONAL = "Romania"  # REG_LUNAR writes TOTAL; StatBrief's region list resolves "Romania" (see README)
COUNTIES = {"Cluj": 0.3, "Iasi": -0.4, "Timis": 0.1, "Bucuresti": 0.6}  # county minus national CPI rate, pp
# Months where the IRS national CPI rate differs from the HICP rate beyond tolerance (value conflicts).
PLANTED_RATE_CONFLICTS = {"2023-04": 0.6, "2023-11": -0.5, "2024-07": 0.8}
# Planted validation issues on sheet Preturi.
NON_NUMERIC = ("Cluj", "IPC_VAR_AN", "2024-06", "5,9 provizoriu")
DUPLICATE = ("Iasi", "IPC_VAR_AN", "2024-02")
UNKNOWN_COUNTY = ("Cluj-Napoca", "IPC_VAR_AN", "2024-05")
DMY_DATES = {("Timis", "IPC_VAR_AN", "2024-03"): "15.03.2024", ("Timis", "IPC_VAR_AN", "2024-04"): "15.04.2024"}
# Planted missing value on sheet Somaj (confidential / not yet available, §2.5 of the data dictionary).
MISSING = ("Bucuresti", "SOMERI_INREG", "2024-12")
UM = {"IPC": "indice 2021=100", "IPC_VAR_AN": "%", "RATA_SOMAJ_INREG": "%", "SOMERI_INREG": "persoane",
      "CASTIG_NET": "lei", "SALARIATI": "mii persoane"}


def _series(path: Path) -> dict[str, float]:
    """period → value of a recorded single-geo Eurostat JSON-stat response."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    index = doc["dimension"]["time"]["category"]["index"]
    return {period: doc["value"][str(i)] for period, i in index.items() if str(i) in doc["value"]}


def _irs_prices(rng: random.Random) -> list[list]:
    """Rows of sheet Preturi: national and county IPC and IPC_VAR_AN with the planted issues."""
    index15, rate = _series(HICP_INDEX_FIXTURE), _series(HICP_RATE_FIXTURE)
    base2021 = sum(index15[f"2021-{m:02d}"] for m in range(1, 13)) / 12
    rows: list[list] = []
    for month in MONTHS:
        ipc = round(index15[month] / base2021 * 100 + rng.choice((-0.04, -0.02, 0.0, 0.02, 0.04)), 2)
        rows.append([month, NATIONAL, "IPC", ipc, UM["IPC"]])
    national_rate = {}
    for month in MONTHS:
        dev = PLANTED_RATE_CONFLICTS.get(month, rng.choice((-0.1, 0.0, 0.0, 0.1)))
        national_rate[month] = round(rate[month] + dev, 1)
        rows.append([month, NATIONAL, "IPC_VAR_AN", national_rate[month], UM["IPC_VAR_AN"]])
    for county, offset in COUNTIES.items():
        for month in MONTHS:
            value = round(national_rate[month] + offset + rng.choice((-0.2, -0.1, 0.0, 0.1, 0.2)), 1)
            luna = DMY_DATES.get((county, "IPC_VAR_AN", month), month)
            cell = NON_NUMERIC[3] if (county, "IPC_VAR_AN", month) == NON_NUMERIC[:3] else value
            rows.append([luna, county, "IPC_VAR_AN", cell, UM["IPC_VAR_AN"]])
            if (county, "IPC_VAR_AN", month) == DUPLICATE:
                rows.append(list(rows[-1]))
    county, indicator, month = UNKNOWN_COUNTY
    rows.append([month, county, indicator, round(national_rate[month] + 0.3, 1), UM[indicator]])
    return [r + [_adj(r[3], rng)] for r in rows]


def _adj(value, rng: random.Random):
    """The undocumented val_adj_x column: an unexplained adjusted figure (meaning unknown on purpose)."""
    if not isinstance(value, float | int):
        return None
    return round(value * rng.uniform(0.97, 1.03), 2)


def _irs_unemployment(rng: random.Random) -> list[list]:
    """Rows of sheet Somaj: registered unemployment rate and persons, with one planted missing value."""
    rate0 = {NATIONAL: 3.3, "Cluj": 1.6, "Iasi": 4.3, "Timis": 1.1, "Bucuresti": 0.9}
    persons0 = {NATIONAL: 255_000, "Cluj": 6_900, "Iasi": 15_600, "Timis": 4_300, "Bucuresti": 10_200}
    rows: list[list] = []
    for region, r0 in rate0.items():
        for i, month in enumerate(MONTHS):
            season = 0.15 if month[5:] in ("01", "02", "12") else 0.0
            rows.append([month, region, "RATA_SOMAJ_INREG", round(r0 - 0.01 * i + season, 1), UM["RATA_SOMAJ_INREG"]])
    for region, p0 in persons0.items():
        for i, month in enumerate(MONTHS):
            value = None if (region, "SOMERI_INREG", month) == MISSING else round(p0 * (1 - 0.003 * i)
                                                                                  + rng.randint(-150, 150))
            rows.append([month, region, "SOMERI_INREG", value, UM["SOMERI_INREG"]])
    return [r + [_adj(r[3], rng)] for r in rows]


def build_irs_workbook(rng: random.Random) -> bytes:
    """IRS_regional_monthly_2023-2024.xlsx: sheets Preturi and Somaj with Romanian headers."""
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in (("Preturi", _irs_prices(rng)), ("Somaj", _irs_unemployment(rng))):
        ws = wb.create_sheet(name)
        ws.append(HEADER)
        for r in rows:
            ws.append(r)
    wb.properties.creator = "Institutul Român de Statistică (IRS)"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_sector_csv(rng: random.Random) -> bytes:
    """sector_output_Q.csv: quarterly gross value added levels (mil. lei) with thousands separators."""
    base = {"GVA_INDUSTRY": 52_000.0, "GVA_CONSTRUCTION": 17_500.0, "GVA_SERVICES": 128_000.0}
    shares = {"RO": 1.0, "RO11": 0.12, "RO12": 0.11, "RO32": 0.27}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["period", "region", "indicator", "value", "unit"])
    for indicator, level in base.items():
        for region, share in shares.items():
            for i, quarter in enumerate(QUARTERS):
                seasonal = (0.96, 1.01, 1.04, 0.99)[i % 4]
                value = level * share * seasonal * (1.011 ** i) * rng.uniform(0.99, 1.01)
                w.writerow([quarter, region, indicator, f"{value:,.1f}", "mil. lei"])
    return buf.getvalue().encode("utf-8")


def build_regional_json(rng: random.Random) -> bytes:
    """regional_indicators.json: an IRS online-database export (records wrapped in an object)."""
    earnings0 = {"Cluj": 5_350, "Iasi": 4_450, "Timis": 5_050, "Bucuresti": 6_400}
    employees0 = {"Cluj": 262.0, "Iasi": 178.5, "Timis": 238.0, "Bucuresti": 1_095.0}
    records = []
    for county in COUNTIES:
        for i, month in enumerate(MONTHS[12:]):
            records.append({"luna": month, "judet": county, "indicator": "CASTIG_NET",
                            "valoare": round(earnings0[county] * (1.012 ** i) + rng.randint(-40, 40)), "um": "lei"})
            records.append({"luna": month, "judet": county, "indicator": "SALARIATI",
                            "valoare": round(employees0[county] * (1 + 0.001 * i) + rng.uniform(-0.5, 0.5), 1),
                            "um": "mii persoane"})
    doc = {"dataset": "REG_LUNAR", "issuer": "Institutul Român de Statistică (IRS)",
           "exported": "2025-02-14", "records": records}
    return (json.dumps(doc, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def build_cryptic_csv(rng: random.Random) -> bytes:
    """cryptic_columns.csv: plausible data under meaningless column names (mapping must be confirmed)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["c_01", "x7", "kk", "vv", "zz_flag", "adj2"])
    for code, region in (("RO113", 2.4), ("RO213", 4.1), ("RO424", 1.3)):
        for i, month in enumerate(MONTHS[12:]):
            value = round(region + 0.05 * (i % 3) - 0.02 * i, 2)
            w.writerow([month, "A17", code, value, "p" if i == 11 else "", round(value * rng.uniform(0.9, 1.1), 3)])
    return buf.getvalue().encode("utf-8")


def main() -> None:
    """Write every sample file (one RNG, fixed seed, fixed order)."""
    rng = random.Random(SEED)
    OUT.mkdir(exist_ok=True)
    outputs = {"IRS_regional_monthly_2023-2024.xlsx": build_irs_workbook(rng),
               "sector_output_Q.csv": build_sector_csv(rng),
               "regional_indicators.json": build_regional_json(rng),
               "cryptic_columns.csv": build_cryptic_csv(rng)}
    for name, data in outputs.items():
        (OUT / name).write_bytes(data)
        print(f"{name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
