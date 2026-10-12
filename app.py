from fastapi import FastAPI, Form
from fastapi.middleware.cors import CORSMiddleware
import requests
import csv
import io
import re
import time
from datetime import datetime, timedelta

app = FastAPI(title="EM Shift Master Hub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SHEET_ID = "1tNxIC97VlR1LKxRIqoKHpJnzcssua_FTH8Y1wLEp6cg"

KNOWN_ZONES = ['Y/TL', 'R2A', 'R2B', 'Y/C', 'REG', 'R1', 'YS', 'Y2', 'GZ', 'GS', 'Y']
WORKING_SHIFTS = ['AM', 'PM', 'N', 'NIGHT']
NO_ZONE_SHIFTS = ['SD', 'OD', 'AL', 'MC', 'OH', 'AD', 'COURSE', 'OL', 'LEAVE', 'OFF', 'REST', 'SL']

# Mon(O=A:0, P:1), Tue(O=H:7, P:8), Wed(O=O:14, P:15), Thu(O=V:21, P:22), Fri(O=AC:28, P:29), Sat(O=AJ:35, P:36), Sun(O=AQ:42, P:43)
DAY_BLOCK_COLS = [0, 7, 14, 21, 28, 35, 42]

def fetch_sheet_csv(tab_name: str):
    # Cache-busting timestamp (&t=) ensures fresh sheet data
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv&sheet={tab_name}&t={int(time.time())}"
    response = requests.get(url, timeout=10)
    response.encoding = 'utf-8'
    if response.status_code == 200:
        rows = list(csv.reader(io.StringIO(response.text)))
        padded_rows = []
        for r in rows:
            clean_r = [cell.replace('\xa0', ' ').strip() for cell in r]
            if len(clean_r) < 50:
                clean_r += [""] * (50 - len(clean_r))
            padded_rows.append(clean_r)
        return padded_rows
    return []

def match_doctor_name(cell_text: str, app_name: str, weekly_name: str) -> bool:
    if not cell_text:
        return False
    
    cleaned_cell_words = set(re.sub(r'[^A-Z0-9\s]', ' ', cell_text.upper()).split())
    
    for target in [app_name, weekly_name]:
        if not target: continue
        target_words = [w for w in re.sub(r'[^A-Z0-9\s]', ' ', target.upper()).split() if len(w) >= 3]
        
        # Match if any key word (e.g. JOVI) is in the cell
        for w in target_words:
            if w in cleaned_cell_words:
                return True
                
    return False

def extract_zone(text: str) -> str:
    if not text: return ""
    text_upper = text.upper().strip()
    cleaned_spaces = re.sub(r'[^A-Z0-9/]', ' ', text_upper)
    for z in KNOWN_ZONES:
        pattern = r'(?:^|\s|[^A-Z0-9/])' + re.escape(z) + r'(?:$|\s|[^A-Z0-9/])'
        if re.search(pattern, cleaned_spaces):
            return z
    return ""

def parse_date_string(text: str) -> str:
    if not text: return ""
    text = text.strip()

    m = re.search(r'Date\((\d{4})\s*,\s*(\d{1,2})\s*,\s*(\d{1,2})\)', text)
    if m:
        y = int(m.group(1))
        m_idx = int(m.group(2)) + 1
        d = int(m.group(3))
        return f"{y:04d}-{m_idx:02d}-{d:02d}"

    m = re.search(r'\b(20\d\d)[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12][0-9]|3[01])\b', text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    m = re.search(r'\b(0?[1-9]|[12][0-9]|3[01])[/.-](0?[1-9]|1[0-2])(?:[/.-](20\d\d|\d\d))?\b', text)
    if m:
        day = m.group(1).zfill(2)
        month = m.group(2).zfill(2)
        raw_year = m.group(3)
        year = raw_year if (raw_year and len(raw_year) == 4) else ("20" + raw_year if raw_year else "2026")
        return f"{year}-{month}-{day}"

    m = re.search(r'\b(0?[1-9]|[12][0-9]|3[01])[\s\-_](JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[a-z]*\b', text, re.IGNORECASE)
    if m:
        day = m.group(1).zfill(2)
        m_str = m.group(2).upper()
        months = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
        month = str(months.index(m_str) + 1).zfill(2)
        return f"2026-{month}-{day}"

    return ""

def resolve_block_dates(weekly_sheet):
    base_date_dt = None
    for r_idx in range(min(20, len(weekly_sheet))):
        row_data = weekly_sheet[r_idx]
        for c_idx, cell in enumerate(row_data):
            parsed = parse_date_string(cell)
            if parsed:
                try:
                    found_dt = datetime.strptime(parsed, "%Y-%m-%d")
                    b_idx = min(c_idx // 7, 6)
                    base_date_dt = found_dt - timedelta(days=b_idx)
                    break
                except ValueError:
                    pass
        if base_date_dt:
            break

    if not base_date_dt:
        today = datetime.now()
        base_date_dt = today - timedelta(days=today.weekday())

    block_dates = []

    for b_idx, block_col in enumerate(DAY_BLOCK_COLS):
        explicit_date = ""
        for r_check in range(min(12, len(weekly_sheet))):
            row_data = weekly_sheet[r_check]
            for offset in range(7):
                cell_idx = block_col + offset
                if cell_idx < len(row_data):
                    parsed = parse_date_string(row_data[cell_idx])
                    if parsed:
                        explicit_date = parsed
                        break
            if explicit_date: break

        if explicit_date:
            block_dates.append(explicit_date)
        else:
            block_dates.append((base_date_dt + timedelta(days=b_idx)).strftime("%Y-%m-%d"))

    return block_dates

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
        if len(weekly_sheet) >= 1:
            block_dates = resolve_block_dates(weekly_sheet)

            for b_idx, block_col in enumerate(DAY_BLOCK_COLS):
                assigned_date = block_dates[b_idx]
                current_shift = "AM"

                # Scan from Row 8 (0-indexed = 7) downwards to cover P13 and surrounding rows
                for r_idx in range(7, len(weekly_sheet)):
                    row = weekly_sheet[r_idx]

                    # Shift label in Column O (14 for Wed)
                    c_shift = row[block_col].upper().strip()
                    # Doctor name in Column P (15 for Wed)
                    c_name = row[block_col + 1]
                    # Zone in Column Q (16 for Wed)
                    c_zone = row[block_col + 2]

                    if c_shift:
                        if "AM" in c_shift: current_shift = "AM"
                        elif "PM" in c_shift: current_shift = "PM"
                        elif "NIGHT" in c_shift or c_shift == "N": current_shift = "N"
                        elif "SD" in c_shift or "S/D" in c_shift: current_shift = "SD"
                        elif "OD" in c_shift or "O/D" in c_shift: current_shift = "OD"
                        elif "AL" in c_shift: current_shift = "AL"
                        elif "OL" in c_shift: current_shift = "OL"
                        elif "MC" in c_shift: current_shift = "MC"
                        elif "OFF" in c_shift or "REST" in c_shift: current_shift = "OFF"
                        elif "LEAVE" in c_shift: current_shift = "LEAVE"

                    # Check doctor name in Col P (c_name) as well as fallback across adjacent cells
                    combined_block_text = f"{c_shift} {c_name} {c_zone}".strip()

                    if match_doctor_name(c_name, app_name, weekly_name) or match_doctor_name(combined_block_text, app_name, weekly_name):
                        detected_zone = ""
                        if current_shift in WORKING_SHIFTS:
                            detected_zone = extract_zone(c_zone) or extract_zone(c_name) or extract_zone(combined_block_text)

                        s_time, e_time = "08:00", "16:00"
                        if current_shift == 'PM': s_time, e_time = "14:00", "22:00"
                        elif current_shift in ['N', 'NIGHT']: s_time, e_time = "21:00", "09:00"
                        elif current_shift in NO_ZONE_SHIFTS: s_time, e_time = "", ""

                        results.append({
                            "date": assigned_date,
                            "shift": current_shift,
                            "zone": detected_zone,
                            "assignmentType": current_shift,
                            "startTime": s_time,
                            "endTime": e_time,
                            "isLocum": False
                        })
                        break

    except Exception as e:
        print(f"Weekly Sheet Error: {e}")

    # 3. READ MONTHLY LOCUM TAB
    try:
        locum_sheet = fetch_sheet_csv("Locum")
        if len(locum_sheet) > 0:
            header_text = " ".join([" ".join(r) for r in locum_sheet[:5]]).upper()
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
                # LEFT BLOCK (Days 1–15)
                if row[0].strip():
                    d_left = re.search(r'\b([1-3]?[0-9])\b', row[0])
                    if d_left:
                        val = int(d_left.group(1))
                        if 1 <= val <= 31: last_date_left = str(val).zfill(2)

                if row[2].strip():
                    t_left = row[2].upper()
                    if "10" in t_left: last_time_left = ("10:00", "14:00")
                    elif "2" in t_left: last_time_left = ("14:00", "18:00")
                    elif "7" in t_left or "19" in t_left: last_time_left = ("19:00", "23:00")

                left_names = " ".join(row[3:6])
                if match_doctor_name(left_names, app_name, locum_name):
                    results.append({
                        "date": f"{target_year}-{target_month}-{last_date_left}",
                        "shift": "Locum",
                        "assignmentType": "Internal Locum",
                        "startTime": last_time_left[0],
                        "endTime": last_time_left[1],
                        "facilityName": "ED Department Locum",
                        "isLocum": True
                    })

                # RIGHT BLOCK (Days 16–31)
                if row[6].strip():
                    d_right = re.search(r'\b([1-3]?[0-9])\b', row[6])
                    if d_right:
                        val_r = int(d_right.group(1))
                        if 1 <= val_r <= 31: last_date_right = str(val_r).zfill(2)

                if row[8].strip():
                    t_right = row[8].upper()
                    if "10" in t_right: last_time_right = ("10:00", "14:00")
                    elif "2" in t_right: last_time_right = ("14:00", "18:00")
                    elif "7" in t_right or "19" in t_right: last_time_right = ("19:00", "23:00")

                right_names = " ".join(row[9:12])
                if match_doctor_name(right_names, app_name, locum_name):
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
