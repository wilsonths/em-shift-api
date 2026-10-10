from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
import requests
import csv
import io
import re

app = FastAPI(title="EM Shift Cloud Sync API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Your Master Hub Google Sheet ID
SHEET_ID = "1tNxIC97VlR1LKxRIqoKHpJnzcssua_FTH8Y1wLEp6cg"
CSV_BASE_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet="

KNOWN_ZONES = ['Y/TL', 'R2A', 'R2B', 'Y/C', 'REG', 'R1', 'YS', 'Y2', 'GZ', 'GS', 'Y']
WORKING_SHIFTS = ['AM', 'PM', 'N']
NO_ZONE_SHIFTS = ['SD', 'OD', 'AL', 'MC', 'OH', 'AD', 'COURSE', 'OL', 'LEAVE']

def fetch_sheet_csv(tab_name: str):
    response = requests.get(CSV_BASE_URL + tab_name)
    response.encoding = 'utf-8'
    return list(csv.reader(io.StringIO(response.text)))

def match_exact_name(cell_text: str, target_name: str) -> bool:
    if not cell_text or not target_name: return False
    clean_target = re.escape(target_name.upper().strip())
    pattern = r'(?:^|\s|[^A-Z0-9])' + clean_target + r'(?:$|\s|[^A-Z0-9])'
    return bool(re.search(pattern, cell_text.upper()))

def extract_zone(text: str) -> str:
    cleaned = re.sub(r'[^A-Z0-9/]', '', text.upper().strip())
    for z in KNOWN_ZONES:
        pattern = r'(?:^|[^A-Z0-9])' + re.escape(z) + r'(?:$|[^A-Z0-9])'
        if re.search(pattern, cleaned): return z
    return ""

def parse_date(text: str) -> str:
    m = re.search(r'\b([0-3]?[0-9])[/.-]([0-1]?[0-9])(?:[/.-](20\d\d|\d\d))?\b', text)
    if m:
        return f"{m.group(3) if len(m.group(3))==4 else '20'+m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return ""

@app.get("/")
def health_check():
    return {"status": "ok", "message": "EM Shift Cloud API Online"}

@app.post("/sync-roster")
def sync_roster(user_name: str = Form(...)):
    app_name = user_name.upper().strip()
    weekly_name = app_name
    locum_name = app_name

    # 1. READ ALIASES TAB
    try:
        aliases_sheet = fetch_sheet_csv("Aliases")
        for row in aliases_sheet[1:]: # Skip header
            if len(row) >= 3 and row[0].upper().strip() == app_name:
                weekly_name = row[1].upper().strip()
                locum_name = row[2].upper().strip()
                break
    except:
        pass # Fallback to using app_name for both if Aliases tab fails

    results = []

    # 2. READ WEEKLY SHIFTS TAB
    try:
        weekly_sheet = fetch_sheet_csv("Weekly")
        col_to_shift = {}
        current_shift = "AM"
        
        # Identify Columns
        for r_idx, row in enumerate(weekly_sheet[:5]):
            for c_idx, cell in enumerate(row):
                val = cell.upper()
                if "AM" in val: current_shift = "AM"
                elif "PM" in val: current_shift = "PM"
                elif "NIGHT" in val or val == "N": current_shift = "N"
                elif "SD" in val: current_shift = "SD"
                elif "OD" in val: current_shift = "OD"
                elif "AL" in val: current_shift = "AL"
                col_to_shift[c_idx] = current_shift
        
        # Scan Rows for user
        last_date = "2026-10-01"
        for row in weekly_sheet:
            if not row: continue
            
            # Update date if present in column 0
            row_date = parse_date(row[0])
            if row_date: last_date = row_date

            for c_idx, cell in enumerate(row):
                if match_exact_name(cell, weekly_name):
                    shift = col_to_shift.get(c_idx, "AM")
                    
                    zone = ""
                    if shift in WORKING_SHIFTS:
                        # Check inside cell first
                        zone = extract_zone(cell)
                        # If no zone in same cell, check the cell immediately to the right
                        if not zone and c_idx + 1 < len(row):
                            zone = extract_zone(row[c_idx + 1])

                    s_time, e_time = "08:00", "16:00"
                    if shift == 'PM': s_time, e_time = "14:00", "22:00"
                    elif shift == 'N': s_time, e_time = "21:00", "09:00"
                    elif shift in NO_ZONE_SHIFTS: s_time, e_time = "", ""

                    results.append({
                        "date": last_date, "shift": shift, "zone": zone,
                        "assignmentType": shift, "startTime": s_time, "endTime": e_time, "isLocum": False
                    })
                    break # Stop scanning this row once found
    except Exception as e:
        print(f"Weekly parsing error: {e}")

    # 3. READ LOCUM TAB
    try:
        locum_sheet = fetch_sheet_csv("Locum")
        col_to_times = {}
        
        # Identify time columns
        for r_idx, row in enumerate(locum_sheet[:5]):
            for c_idx, cell in enumerate(row):
                val = cell.upper()
                if "10" in val and "2" in val: col_to_times[c_idx] = ("10:00", "14:00")
                elif "2" in val and "6" in val: col_to_times[c_idx] = ("14:00", "18:00")
                elif "7" in val and "11" in val: col_to_times[c_idx] = ("19:00", "23:00")

        last_locum_date = "2026-10-01"
        for row in locum_sheet:
            if not row: continue
            
            # Find date in col 0
            date_match = re.search(r'\b([0-3]?[0-9])\b', row[0])
            if date_match: last_locum_date = f"2026-10-{date_match.group(1).zfill(2)}"

            for c_idx, cell in enumerate(row):
                if match_exact_name(cell, locum_name):
                    s_time, e_time = col_to_times.get(c_idx, ("19:00", "23:00"))
                    results.append({
                        "date": last_locum_date, "shift": "Locum", "assignmentType": "Internal Locum",
                        "startTime": s_time, "endTime": e_time, "facilityName": "ED Department Locum", "isLocum": True
                    })
    except Exception as e:
        print(f"Locum parsing error: {e}")

    # Sort results by date
    results.sort(key=lambda x: x['date'])

    return {"status": "success", "count": len(results), "data": results}
