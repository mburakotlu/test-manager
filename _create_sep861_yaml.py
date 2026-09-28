import yaml
from pathlib import Path

from import_jahreskalender import extract_entries

entries = extract_entries(Path("DLCM_FZG_Planung.xlsx"))
tests = []
for name, date, text in entries:
    if "861" not in str(name):
        continue
    iso_year, iso_week, _ = date.isocalendar()
    period = f"KW{iso_week:02d}-{iso_year}"
    clean = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    tests.append({"name": clean, "period": period, "template": "analysis_checklist"})

data = {
    "label": "SEP861",
    "key": "PHUB-444945",
    "vin": "WP0ZZZY13PSA50004",
    "series": "J1.1",
    "market": "",
    "scope": "DLCM Wellen-Absicherung / Funktionales Testing",
    "tec_pc": "",
    "tests": tests,
}
header = "# Source: DLCM_FZG_Planung.xlsx (Jahreskalender + FZGToVIN sheets)\n"
content = header + yaml.dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)
Path("vehicles/SEP861.yaml").write_text(content, encoding="utf-8")
print(f"Wrote vehicles/SEP861.yaml with {len(tests)} test(s)")
