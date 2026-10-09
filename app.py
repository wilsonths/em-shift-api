from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import pdfplumber
import io
import re

app = FastAPI(title="EM Shift Spatial Parser")

# Enable CORS so Cloudflare Pages can call this API securely
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

KNOWN_ZONES = ['R1', 'R2A', 'R2B', 'Y/TL', 'YS', 'Y/C', 'Y', 'Y2', 'GZ', 'GS', 'REG']
REF_DATES = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-10', '2026-10-11']

def match_exact_name(source_text: str, target_name: str) -> bool:
    if not source_text or not target_name:
        return False
    clean_target = re.escape(target_name.upper().strip())
    pattern = r'(?:^|\s|[^A-Z0-9])' + clean_target + r'(?:$|\s|[^A-Z0-9])'
    return bool(re.search(pattern, source_text.upper()))

@app.get("/")
def health_check():
    return {"status": "ok", "message": "EM Shift Parser Engine Online"}

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
            # 1. WEEKLY OFFICIAL SHIFT ENGINE (Spatial Table Vector Slicing)
            for page in pdf.pages:
                tables = page.extract_tables()
                if not tables:
                    # Fallback to visual line crop if table borders are implicit
                    lines = page.extract_text().split('\n')
                    for idx, line in enumerate(lines):
                        if "REMARKS" in line.upper() or "REGISTRAR" in line.upper():
                            continue
                        if match_exact_name(line, target_name):
                            target_date = REF_DATES[len(extracted_results) % len(REF_DATES)]
                            detected_shift = "AM"
                            if "PM" in line.upper(): detected_shift = "PM"
                            elif "NIGHT" in line.upper() or " N " in line.upper(): detected_shift = "N"
                            elif "SD" in line.upper(): detected_shift = "SD"
                            
                            detected_zone = next((z for z in KNOWN_ZONES if z in line.upper()), "")
                            
                            extracted_results.append({
                                "date": target_date,
                                "shift": detected_shift,
                                "zone": detected_zone,
                                "assignmentType": detected_shift,
                                "startTime": "08:00" if detected_shift == "AM" else ("14:00" if detected_shift == "PM" else "21:00"),
                                "endTime": "16:00" if detected_shift == "AM" else ("22:00" if detected_shift == "PM" else "09:00"),
                                "isLocum": False
                            })
                else:
                    for table in tables:
                        for row in table:
                            row_cells = [cell for cell in row if cell]
                            row_str = " ".join(row_cells).upper()
                            if "REMARKS" in row_str or "REGISTRAR" in row_str:
                                continue
                            if match_exact_name(row_str, target_name):
                                target_date = REF_DATES[len(extracted_results) % len(REF_DATES)]
                                detected_shift = "AM"
                                if "PM" in row_str: detected_shift = "PM"
                                elif "NIGHT" in row_str or " N " in row_str: detected_shift = "N"
                                elif "SD" in row_str: detected_shift = "SD"

                                detected_zone = next((z for z in KNOWN_ZONES if z in row_str), "")

                                extracted_results.append({
                                    "date": target_date,
                                    "shift": detected_shift,
                                    "zone": detected_zone,
                                    "assignmentType": detected_shift,
                                    "startTime": "08:00" if detected_shift == "AM" else "14:00",
                                    "endTime": "16:00" if detected_shift == "AM" else "22:00",
                                    "isLocum": False
                                })

        elif roster_type == "monthly_locum":
            # 2. MONTHLY LOCUM ENGINE (2-Column Crop Slicing)
            for page in pdf.pages:
                w, h = page.width, page.height
                left_half = page.crop((0, 0, w / 2, h)).extract_text()
                right_half = page.crop((w / 2, 0, w, h)).extract_text()

                for half_text in [left_half, right_half]:
                    if not half_text: continue
                    for line in half_text.split('\n'):
                        line_str = line.upper()
                        if "REMARKS" in line_str or "NOTES" in line_str:
                            continue
                        if match_exact_name(line_str, target_name):
                            date_match = re.search(r'\b([1-3]?[0-9])\b', line_str)
                            date_num = date_match.group(1).zfill(2) if date_match else "01"

                            start_time, end_time = "19:00", "23:00"
                            if "10AM" in line_str or "10-2" in line_str:
                                start_time, end_time = "10:00", "14:00"
                            elif "2PM" in line_str or "2-6" in line_str:
                                start_time, end_time = "14:00", "18:00"

                            extracted_results.append({
                                "date": f"2026-10-{date_num}",
                                "shift": "Locum",
                                "assignmentType": "Internal Locum",
                                "startTime": start_time,
                                "endTime": end_time,
                                "facilityName": "ED Department Locum",
                                "isLocum": True
                            })

    return {"status": "success", "count": len(extracted_results), "data": extracted_results}