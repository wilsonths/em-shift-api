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

def extract_zone_from_text(text: str) -> str:
    text_clean = re.sub(r'[^A-Z0-9/]', '', text.upper().strip())
    for zone in KNOWN_ZONES_SORTED:
        pattern = r'(?:^|[^A-Z0-9])' + re.escape(zone) + r'(?:$|[^A-Z0-9])'
        if re.search(pattern, text_clean):
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

                # 1. Identify Shift Column X-Boundaries (Left to Right)
                shift_cols = []
                for w in words:
                    txt = w['text'].upper().strip()
                    if txt in ['AM', 'PM', 'SD', 'OD', 'AL', 'MC']:
                        shift_cols.append({'shift': txt, 'x0': w['x0'] - 10, 'x1': w['x1'] + 45})
                    elif txt in ['NIGHT', 'N'] and len(txt) <= 5:
                        shift_cols.append({'shift': 'N', 'x0': w['x0'] - 10, 'x1': w['x1'] + 45})
                
                # Remove duplicate column detections and sort left-to-right
                shift_cols.sort(key=lambda c: c['x0'])
                unique_cols = []
                for c in shift_cols:
                    if not any(abs(c['x0'] - uc['x0']) < 25 for uc in unique_cols):
                        unique_cols.append(c)

                # 2. Identify Day Row Y-Boundaries (Top to Bottom)
                day_rows = []
                for w in words:
                    txt = w['text'].upper().strip()
                    for day in DAYS_OF_WEEK:
                        if day in txt:
                            day_rows.append({'day': day, 'top': w['top'] - 5, 'y0': w['top']})
                            break
                
                day_rows.sort(key=lambda r: r['y0'])
                for idx, r in enumerate(day_rows):
                    r['bottom'] = day_rows[idx + 1]['top'] if idx + 1 < len(day_rows) else page.height

                ref_dates = ['2026-10-05', '2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-10', '2026-10-11']

                # 3. Process Row-by-Row, Left-to-Right
                for idx, day_row in enumerate(day_rows):
                    target_date = ref_dates[idx] if idx < len(ref_dates) else f"2026-10-{(5+idx):02d}"
                    
                    # Get words strictly inside this Date row (excluding bottom remarks)
                    row_words = [
                        w for w in words 
                        if day_row['top'] <= w['top'] < day_row['bottom'] 
                        and 'REMARKS' not in w['text'].upper() 
                        and 'REGISTRAR' not in w['text'].upper()
                    ]
                    
                    row_full_text = " ".join([w['text'] for w in row_words])
                    if not match_exact_name(row_full_text, target_name):
                        continue

                    # Search Column by Column from Left to Right
                    shift_found = None
                    detected_zone = ""

                    for col in unique_cols:
                        col_words = [
                            w for w in row_words 
                            if col['x0'] <= w['x0'] <= col['x1'] + 60
                        ]
                        col_text = " ".join([w['text'] for w in col_words])

                        if match_exact_name(col_text, target_name):
                            shift_found = col['shift']

                            # If working shift (AM, PM, N): read zone immediately to the RIGHT of the name
                            if shift_found in WORKING_SHIFTS:
                                name_words = [
                                    w for w in col_words 
                                    if any(t == re.sub(r'[^A-Z0-9]', '', w['text'].upper()) for t in target_tokens)
                                ]
                                if name_words:
                                    name_x1 = max(w['x1'] for w in name_words)
                                    name_y = sum((w['top'] + w['bottom'])/2.0 for w in name_words) / len(name_words)

                                    # Search words on the same row line (y +- 6) sitting to the right of the name
                                    right_words = [
                                        w for w in row_words
                                        if abs(((w['top'] + w['bottom'])/2.0) - name_y) <= 8
                                        and w['x0'] >= name_x1 - 5
                                    ]
                                    right_words.sort(key=lambda w: w['x0'])

                                    # Check single word or adjacent combined word (e.g. "Y" + "2" -> "Y2")
                                    for rw_idx in range(len(right_words)):
                                        w1_text = right_words[rw_idx]['text']
                                        z = extract_zone_from_text(w1_text)
                                        if z:
                                            detected_zone = z
                                            break
                                        if rw_idx + 1 < len(right_words):
                                            combo = w1_text + right_words[rw_idx + 1]['text']
                                            z_combo = extract_zone_from_text(combo)
                                            if z_combo:
                                                detected_zone = z_combo
                                                break
                            else:
                                # SD, OD, AL, MC -> Strictly NO Zone
                                detected_zone = ""

                            break # Found name in this date row; stop scanning columns and move to next date row

                    if shift_found:
                        start_time, end_time = "08:00", "16:00"
                        if shift_found == 'PM': start_time, end_time = "14:00", "22:00"
                        elif shift_found == 'N': start_time, end_time = "21:00", "09:00"
                        elif shift_found not in WORKING_SHIFTS: start_time, end_time = "", ""

                        extracted_results.append({
                            "date": target_date,
                            "shift": shift_found,
                            "zone": detected_zone,
                            "assignmentType": shift_found,
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
