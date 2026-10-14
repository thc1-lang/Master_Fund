import importlib.util
from pathlib import Path
import sys


path = Path("outputs/COT Reports.py")
spec = importlib.util.spec_from_file_location("cot_inspect", path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

service = module._build_google_sheets_service(r"C:\Code\service_account.json")
metadata = module._sheet_metadata(service, module.DEFAULT_SPREADSHEET_ID)
print("sheets", len(metadata))
print("grid_cells", sum(item.row_count * item.column_count for item in metadata.values()))
print("missing commodities", [name for name in module.COMMODITY_SHEETS if name not in metadata])
print("missing raw", [year for year in range(2010, module.get_current_year() + 1)
                      if f"COT {year}" not in metadata])
ranges = [f"{module._quote_sheet(name)}!G1" for name in module.COMMODITY_SHEETS
          if name in metadata]
values = module._batch_get_values(service, module.DEFAULT_SPREADSHEET_ID, ranges)
print("G1 values")
for name in module.COMMODITY_SHEETS:
    key = f"{module._quote_sheet(name)}!G1"
    if key in values:
        print(name, values[key])
