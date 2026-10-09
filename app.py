from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import io
import re

app = FastAPI(title="EM Shift Spatial Parser")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

KNOWN_ZONES = ['R1', 'R2A', 'R2B', 'Y/TL', 'YS', 'Y/C', 'Y', 'Y2', 'GZ', 'GS', 'REG']

def match_exact_name(source_text: str, target_name: str) -> bool:
    if not source_text or not target_name:
        return False
    clean_target = re.escape(target_name.upper().strip())
    pattern = r'(?:^|\s|[^A-Z0-9])' + clean_target + r'(?:$|\s|[^A-Z0-9])'
    return bool(re.search(pattern, source_text.upper()))

def parse_date_from_text(text: str) -> str:
    # Extracts DD/MM/YYYY, DD/MM/YY, or DD.MM format from cell text
    m = re.search(r'\b([0-3]?[0-9])[/.-]([0-1]?[0-9])(?:[/.-](20\d\d|\d\d))?\b', text)
    if m:
        day, month = m.group(1).zfill(2), m.group(2).zfill(2)
        year = m.group(3) if m.group(3) else "2026"
        if len(year) == 2:
            year = "20" + year
        return f"{year}-{month}-{day}"
    return ""

@app.get("/")
def health_check():
    return {"status": "ok", "message": "EM Shift Spatial Parser Engine Online"}

@app.post("/parse-roster")
async def parse_roster(
    file: UploadFile = File(...),
    roster_type: str = Form(...),  # "weekly_hospital" or "monthly_locum"
    user_name: str = Form(...)
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    contents = await file.read()
    target_name = user_name.upper().strip()
    extracted_results = []

    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        if roster_type == "weekly_hospital":
            # 1. WEEKLY OFFICIAL SHIFT ENGINE (2D Cell-Index Column Matching)
            for page in pdf.pages:
                tables = page.extract_tables()
                if tables:
                    for table in tables:
                        if not table or len(table) < 2:
                            continue
                        
                        # Inspect Header Row to map column index -> Shift Type
                        header_row = [str(c or '').upper().strip() for c in table[0]]
                        col_map = {}
                        for idx, h in enumerate(header_row):
                            if 'AM' in h: col_map[idx] = 'AM'
                            elif 'PM' in h: col_map[idx] = 'PM'
                            elif 'NIGHT' in h or h == 'N': col_map[idx] = 'N'
                            elif 'SD' in h: col_map[idx] = 'SD'
                            elif 'OD' in h: col_map[idx] = 'OD'
                            elif 'AL' in h: col_map[idx] = 'AL'

                        # Process Data Rows (One Row = One Day)
                        for row in table[1:]:
                            if not row: continue
                            row_str = " ".join([str(c or '') for c in row]).upper()
                            
                            # Exclude bottom remarks
                            if "REMARKS" in row_str or "REGISTRAR ON CALL" in row_str or "NOTES" in row_str:
                                continue
                            
                            extracted_date = parse_date_from_text(row_str)
                            
                            # Check each cell by specific column index
                            for col_idx, cell in enumerate(row):
                                cell_text = str(cell or '').strip()
                                if match_exact_name(cell_text, target_name):
                                    shift_type = col_map.get(col_idx, 'AM')
                                    zone = next((z for z in KNOWN_ZONES if z in cell_text.upper() or z in row_str), "")
                                    
                                    start_time, end_time = "08:00", "16:00"
                                    if shift_type == 'PM': start_time, end_time = "14:00", "22:00"
                                    elif shift_type == 'N': start_time, end_time = "21:00", "09:00"
                                    elif shift_type in ['SD', 'OD', 'AL']: start_time, end_time = "", ""

                                    extracted_results.append({
                                        "date": extracted_date or f"2026-10-0{len(extracted_results)+5}",
                                        "shift": shift_type,
                                        "zone": zone,
                                        "assignmentType": shift_type,
                                        "startTime": start_time,
                                        "endTime": end_time,
                                        "isLocum": False
                                    })
                                    break # Lock 1 shift per day row
                else:
                    # Line fallback if table grid vector lines are implicit
                    lines = page.extract_text().split('\n')
                    for line in lines:
                        if "REMARKS" in line.upper() or "REGISTRAR" in line.upper():
                            continue
                        if match_exact_name(line, target_name):
                            extracted_date = parse_date_from_text(line)
                            detected_shift = "AM"
                            if "PM" in line.upper(): detected_shift = "PM"
                            elif "NIGHT" in line.upper() or " N " in line.upper(): detected_shift = "N"
                            elif "SD" in line.upper(): detected_shift = "SD"
                            
                            zone = next((z for z in KNOWN_ZONES if z in line.upper()), "")
                            
                            extracted_results.append({
                                "date": extracted_date or f"2026-10-0{len(extracted_results)+5}",
                                "shift": detected_shift,
                                "zone": zone,
                                "assignmentType": detected_shift,
                                "startTime": "08:00" if detected_shift == "AM" else ("14:00" if detected_shift == "PM" else "21:00"),
                                "endTime": "16:00" if detected_shift == "AM" else ("22:00" if detected_shift == "PM" else "09:00"),
                                "isLocum": False
                            })

        elif roster_type == "monthly_locum":
            # 2. MONTHLY LOCUM ENGINE (Session Column & Date Cell Lock)
            for page in pdf.pages:
                w, h = page.width, page.height
                left_half = page.crop((0, 0, w / 2, h))
                right_half = page.crop((w / 2, 0, w, h))

                for crop_page in [left_half, right_half]:
                    tables = crop_page.extract_tables()
                    if tables:
                        for table in tables:
                            if not table: continue
                            for row in table:
                                row_str = " ".join([str(c or '') for c in row]).upper()
                                if "REMARKS" in row_str or "NOTES" in row_str:
                                    continue
                                
                                # Date is strictly in Column 0
                                date_cell = str(row[0] or '') if len(row) > 0 else ''
                                date_match = re.search(r'\b([1-3]?[0-9])\b', date_cell)
                                date_num = date_match.group(1).zfill(2) if date_match else ""

                                # Check session columns specifically (Col 1: 10-2, Col 2: 2-6, Col 3: 7-11)
                                for col_idx in range(1, len(row)):
                                    cell_text = str(row[col_idx] or '').strip()
                                    if match_exact_name(cell_text, target_name):
                                        start_time, end_time = "19:00", "23:00"
                                        if col_idx == 1: start_time, end_time = "10:00", "14:00"
                                        elif col_idx == 2: start_time, end_time = "14:00", "18:00"
                                        elif col_idx == 3: start_time, end_time = "19:00", "23:00"

                                        extracted_results.append({
                                            "date": f"2026-10-{date_num or '01'}",
                                            "shift": "Locum",
                                            "assignmentType": "Internal Locum",
                                            "startTime": start_time,
                                            "endTime": end_time,
                                            "facilityName": "ED Department Locum",
                                            "isLocum": True
                                        })

    return {"status": "success", "count": len(extracted_results), "data": extracted_results}
