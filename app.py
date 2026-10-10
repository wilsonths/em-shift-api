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

KNOWN_ZONES_SORTED = ['Y/TL', 'R2A', 'R2B', 'Y/C', 'REG', 'R1', 'YS', 'Y2', 'GZ', 'GS', 'Y']
DAYS_OF_WEEK = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']
WORKING_SHIFTS = ['AM', 'PM', 'N']

def match_exact_name(source_text: str, target_name: str) -> bool:
    if not source_text or not target_name:
        return False
    clean_target = re.escape(target_name.upper().strip())
    pattern = r'(?:^|\s|[^A-Z0-9])' + clean_target + r'(?:$|\s|[^A-Z0-9])'
    return bool(re.search(pattern, source_text.upper()))

def extract_zone_from_word(text: str) -> str:
    text_upper = text.upper().strip()
    for zone in KNOWN_ZONES_SORTED:
        pattern = r'(?:^|[\s/:\-_(])' + re.escape(zone) + r'(?:$|[\s/:\-_)])'
        if re.search(pattern, text_upper):
            return zone
    return ""

@app.get("/")
def health_check():
    return {"status": "ok", "message": "EM Shift Spatial Parser Engine Online"}

@app.post("/parse-roster")
async def parse_roster(
    file: UploadFile = File(...),
    roster_type: str = Form(...),
    user_name: str = Form(...)
):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    contents = await file.read()
    target_name = user_name.upper().strip()
    target_tokens = [t for t in re.split(r'\s+', target_name) if len(t) > 1]
    extracted_results = []

    with pdfplumber.open(io.BytesIO(contents)) as pdf:
        if roster_type == "weekly_hospital":
            for page in pdf.pages:
                words = page.extract_words()
                if not words:
                    continue

                # 1. Locate Shift Column Header X-Ranges (AM, PM, N, SD, etc.)
                shift_cols = []
                for w in words:
                    txt = w['text'].upper().strip()
                    if txt in ['AM', 'PM', 'SD', 'OD', 'AL', 'MC']:
                        shift_cols.append({'shift': txt, 'x0': w['x0'] - 15, 'x1': w['x1'] + 15})
                    elif txt in ['NIGHT', 'N'] and len(txt) <= 5:
                        shift_cols.append({'shift': 'N', 'x0': w['x0'] - 15, 'x1': w['x1'] + 15})
                
                shift_cols.sort(key=lambda c: c['x0'])

                # 2. Locate Day Row Y-Ranges
                day_rows = []
                for w in words:
                    txt = w['text'].upper().strip()
                    for day in DAYS_OF_WEEK:
                        if day in txt:
                            day_rows.append({'day': day, 'top': w['top'] - 5, 'y0': w['top']})
                            break
                
                day_rows.sort(key=lambda r: r['y0'])
                
                for idx, r in enumerate(day_rows):
                    next_top = day_rows[idx + 1]['top'] if idx + 1 < len(day_rows) else page.height
                    r['bottom'] = next_top

                ref_dates = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-10', '2026-10-11']

                # 3. Match exact user name within bounding boxes
                for idx, day_row in enumerate(day_rows):
                    target_date = ref_dates[idx] if idx < len(ref_dates) else f"2026-10-{(5+idx):02d}"
                    
                    row_words = [
                        w for w in words 
                        if day_row['top'] <= w['top'] < day_row['bottom'] 
                        and 'REMARKS' not in w['text'].upper() 
                        and 'REGISTRAR' not in w['text'].upper()
                    ]
                    
                    row_full_text = " ".join([w['text'] for w in row_words])
                    
                    if match_exact_name(row_full_text, target_name):
                        # Locate exact word tokens belonging to doctor's name
                        name_words = [
                            w for w in row_words 
                            if any(t == re.sub(r'[^A-Z0-9]', '', w['text'].upper()) for t in target_tokens)
                        ]

                        if name_words:
                            name_x = sum((w['x0'] + w['x1']) / 2.0 for w in name_words) / len(name_words)
                            name_y = sum((w['top'] + w['bottom']) / 2.0 for w in name_words) / len(name_words)
                        else:
                            name_x = (row_words[0]['x0'] + row_words[0]['x1']) / 2.0
                            name_y = (row_words[0]['top'] + row_words[0]['bottom']) / 2.0

                        # Determine shift type based on closest header column
                        matched_shift = "AM"
                        if shift_cols:
                            best_col = min(shift_cols, key=lambda c: abs((c['x0'] + c['x1'])/2 - name_x))
                            matched_shift = best_col['shift']
                        else:
                            if "PM" in row_full_text.upper(): matched_shift = "PM"
                            elif "NIGHT" in row_full_text.upper() or " N " in row_full_text.upper(): matched_shift = "N"
                            elif "SD" in row_full_text.upper(): matched_shift = "SD"

                        # Determine Zone: STRICTLY CLEAR for non-working shifts (SD, OD, AL, etc.)
                        detected_zone = ""
                        if matched_shift in WORKING_SHIFTS:
                            zone_candidates = []
                            for w in row_words:
                                z_match = extract_zone_from_word(w['text'])
                                if z_match:
                                    zone_candidates.append({
                                        'zone': z_match,
                                        'x': (w['x0'] + w['x1']) / 2.0,
                                        'y': (w['top'] + w['bottom']) / 2.0
                                    })
                            
                            if zone_candidates:
                                # 2D Spatial Proximity: Pick zone physically closest to doctor's name center
                                best_candidate = min(
                                    zone_candidates,
                                    key=lambda c: ((c['x'] - name_x)**2 + 3 * (c['y'] - name_y)**2)
                                )
                                detected_zone = best_candidate['zone']

                        start_time, end_time = "08:00", "16:00"
                        if matched_shift == 'PM': start_time, end_time = "14:00", "22:00"
                        elif matched_shift == 'N': start_time, end_time = "21:00", "09:00"
                        elif matched_shift not in WORKING_SHIFTS: start_time, end_time = "", ""

                        extracted_results.append({
                            "date": target_date,
                            "shift": matched_shift,
                            "zone": detected_zone,
                            "assignmentType": matched_shift,
                            "startTime": start_time,
                            "endTime": end_time,
                            "isLocum": False
                        })

        elif roster_type == "monthly_locum":
            for page in pdf.pages:
                words = page.extract_words()
                if not words: continue

                w_mid = page.width / 2

                for is_right_side in [False, True]:
                    side_words = [
                        w for w in words 
                        if (w['x0'] >= w_mid if is_right_side else w['x0'] < w_mid)
                        and 'REMARKS' not in w['text'].upper()
                        and 'NOTES' not in w['text'].upper()
                    ]

                    side_text = " ".join([w['text'] for w in side_words])
                    if not match_exact_name(side_text, target_name):
                        continue

                    lines = []
                    sorted_words = sorted(side_words, key=lambda w: (w['top'], w['x0']))
                    cur_line = []
                    last_top = None

                    for w in sorted_words:
                        if last_top is None or abs(w['top'] - last_top) < 6:
                            cur_line.append(w)
                        else:
                            lines.append(cur_line)
                            cur_line = [w]
                        last_top = w['top']
                    if cur_line: lines.append(cur_line)

                    for line in lines:
                        line_text = " ".join([w['text'] for w in line]).upper()
                        if match_exact_name(line_text, target_name):
                            date_match = re.search(r'\b([1-3]?[0-9])\b', line_text)
                            date_num = date_match.group(1).zfill(2) if date_match else "01"

                            start_time, end_time = "19:00", "23:00"
                            if "10AM" in line_text or "10-2" in line_text:
                                start_time, end_time = "10:00", "14:00"
                            elif "2PM" in line_text or "2-6" in line_text:
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
