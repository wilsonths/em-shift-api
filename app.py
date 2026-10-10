from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
import requests
import csv
import io
import re
from datetime import datetime

app = FastAPI(title="EM Shift Master Hub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SHEET_ID = "1tNxIC97VlR1LKxRIqoKHpJnzcssua_FTH8Y1wLEp6cg"
CSV_BASE_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet="

KNOWN_ZONES = ['Y/TL', 'R2A', 'R2B', 'Y/C', 'REG', 'R1', 'YS', 'Y2', 'GZ', 'GS', 'Y']
WORKING_SHIFTS = ['AM', 'PM', 'N', 'NIGHT']
NO_ZONE_SHIFTS = ['SD', 'OD', 'AL', 'MC', 'OH', 'AD', 'COURSE', 'OL', 'LEAVE']

# Column indices for the 7 horizontal day blocks (A, H, O, V, AC, AJ, AQ)
DAY_BLOCK_COLS = [0, 7, 14, 21, 28, 35, 42]

def fetch_sheet_csv(tab_name: str):
    response = requests.get(CSV_BASE_URL + tab_name)
    response.encoding = 'utf-8'
    return list(csv.reader(io.StringIO(response.text)))

def match_exact_name(cell_text: str, target_name: str) -> bool:
    if not cell_text or not target_name:
        return False
    clean_target = re.escape(target_name.upper().strip())
    pattern = r'(?:^|\s|[^A-Z0-9])' + clean_target + r'(?:$|\s|[^A-Z0-9])'
    return bool(re.search(pattern, cell_text.upper()))

def extract_zone(text: str) -> str:
    cleaned = re.sub(r'[^A-Z0-9/]', '', text.upper().strip())
    for z in KNOWN_ZONES:
        pattern = r'(?:^|[^A-Z0-9])' + re.escape(z) + r'(?:$|[^A-Z0-9])'
        if re.search(pattern, cleaned):
            return z
    return ""

def parse_date_string(text: str) -> str:
    m = re.search(r'\b([0-3]?[0-9])[/.-]([0-1]?[0-9])(?:[/.-](20\d\d|\d\d))?\b', text)
    if m:
        day = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        year = m.group(3) if m.group(3) else "2026"
        if len(year) == 2: year = "20" + year
        return f"{year}-{month}-{day}"
    return ""

@app.get("/")
def health_check():
    return {"status": "ok", "message": "EM Shift Master Hub API Online"}

@app.post("/sync-roster")
def sync_roster(user_name: str = Form(...)):
    app_name = user_name.upper().strip()
    weekly_name = app_name
    locum_name = app_name

    # 1. READ ALIASES TAB
    try:
        aliases_sheet = fetch_sheet_csv("Aliases")
        for row in aliases_sheet[1:]:
            if len(row) >= 3 and row[0].upper().strip() == app_name:
                weekly_name = row[1].upper().strip()
                locum_name = row[2].upper().strip()
                break
    except Exception as e:
        print(f"Aliases tab read error: {e}")

    results = []

    # 2. READ WEEKLY SHIFT TAB
    try:
        weekly_sheet = fetch_sheet_csv("Weekly")
        if len(weekly_sheet) >= 10:
            row_10 = weekly_sheet[9] # Row 10 (0-indexed = 9)

            # Process each of the 7 day column blocks
            for block_col in DAY_BLOCK_COLS:
                # Extract date from Column B, I, P, W, AD, AK, AR on Row 10
                date_cell = row_10[block_col + 1] if block_col + 1 < len(row_10) else ""
                extracted_date = parse_date_string(date_cell)
                if not extracted_date:
                    extracted_date = "2026-10-05"

                current_shift = "AM"

                # Scan from Row 12 downwards (index 11)
                for r_idx in range(11, len(weekly_sheet)):
                    row = weekly_sheet[r_idx]
                    if not row: continue

                    # Check shift label in column A/H/O...
                    shift_cell = row[block_col].upper().strip() if block_col < len(row) else ""
                    if shift_cell:
                        if "AM" in shift_cell: current_shift = "AM"
                        elif "PM" in shift_cell: current_shift = "PM"
                        elif "NIGHT" in shift_cell or shift_cell == "N": current_shift = "N"
                        elif "SD" in shift_cell: current_shift = "SD"
                        elif "OD" in shift_cell: current_shift = "OD"
                        elif "AL" in shift_cell: current_shift = "AL"
                        elif "OL" in shift_cell: current_shift = "OL"

                    # Check name in column B/I/P...
                    name_cell = row[block_col + 1] if block_col + 1 < len(row) else ""
                    if match_exact_name(name_cell, weekly_name):
                        zone_cell = row[block_col + 2] if block_col + 2 < len(row) else ""
                        detected_zone = ""
                        
                        if current_shift in WORKING_SHIFTS:
                            detected_zone = extract_zone(zone_cell) or extract_zone(name_cell)

                        s_time, e_time = "08:00", "16:00"
                        if current_shift == 'PM': s_time, e_time = "14:00", "22:00"
                        elif current_shift in ['N', 'NIGHT']: s_time, e_time = "21:00", "09:00"
                        elif current_shift in NO_ZONE_SHIFTS: s_time, e_time = "", ""

                        results.append({
                            "date": extracted_date,
                            "shift": current_shift,
                            "zone": detected_zone,
                            "assignmentType": current_shift,
                            "startTime": s_time,
                            "endTime": e_time,
                            "isLocum": False
                        })
    except Exception as e:
        print(f"Weekly Sheet Error: {e}")

    # 3. READ MONTHLY LOCUM TAB
    try:
        locum_sheet = fetch_sheet_csv("Locum")
        if len(locum_sheet) > 1:
            # Detect Month & Year from Row 1
            header_text = " ".join(locum_sheet[0]).upper()
            target_month = "10"
            target_year = "2026"
            
            month_match = re.search(r'\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*\b', header_text)
            if month_match:
                m_str = month_match.group(1)
                months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
                if m_str in months:
                    target_month = str(months.index(m_str) + 1).zfill(2)

            year_match = re.search(r'\b(20\d\d)\b', header_text)
            if year_match:
                target_year = year_match.group(1)

            last_date_left = "01"
            last_time_left = ("19:00", "23:00")
            
            last_date_right = "16"
            last_time_right = ("19:00", "23:00")

            for row in locum_sheet[1:]:
                if not row: continue

                # --- LEFT BLOCK (Cols A-E) ---
                d_left = re.search(r'\b([1-3]?[0-9])\b', row[0]) if len(row) > 0 else None
                if d_left: last_date_left = d_left.group(1).zfill(2)

                t_left = row[2].upper() if len(row) > 2 else ""
                if "10" in t_left: last_time_left = ("10:00", "14:00")
                elif "2" in t_left: last_time_left = ("14:00", "18:00")
                elif "7" in t_left: last_time_left = ("19:00", "23:00")

                left_names = " ".join(row[3:5]) if len(row) >= 5 else ""
                if match_exact_name(left_names, locum_name):
                    results.append({
                        "date": f"{target_year}-{target_month}-{last_date_left}",
                        "shift": "Locum",
                        "assignmentType": "Internal Locum",
                        "startTime": last_time_left[0],
                        "endTime": last_time_left[1],
                        "facilityName": "ED Department Locum",
                        "isLocum": True
                    })

                # --- RIGHT BLOCK (Cols G-K) ---
                d_right = re.search(r'\b([1-3]?[0-9])\b', row[6]) if len(row) > 6 else None
                if d_right: last_date_right = d_right.group(1).zfill(2)

                t_right = row[8].upper() if len(row) > 8 else ""
                if "10" in t_right: last_time_right = ("10:00", "14:00")
                elif "2" in t_right: last_time_right = ("14:00", "18:00")
                elif "7" in t_right: last_time_right = ("19:00", "23:00")

                right_names = " ".join(row[9:11]) if len(row) >= 11 else ""
                if match_exact_name(right_names, locum_name):
                    results.append({
                        "date": f"{target_year}-{target_month}-{last_date_right}",
                        "shift": "Locum",
                        "assignmentType": "Internal Locum",
                        "startTime": last_time_right[0],
                        "endTime": last_time_right[1],
                        "facilityName": "ED Department Locum",
                        "isLocum": True
                    })

    except Exception as e:
        print(f"Locum Sheet Error: {e}")

    results.sort(key=lambda x: x['date'])
    return {"status": "success", "count": len(results), "data": results}
