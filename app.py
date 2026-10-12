import csv
import io
import logging
import os
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import requests
from fastapi import FastAPI, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware


# ============================================================
# CONFIGURATION
# ============================================================

app = FastAPI(title="EM Shift Master Hub API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

SHEET_ID = os.getenv(
    "SHEET_ID",
    "1tNxIC97VlR1LKxRIqoKHpJnzcssua_FTH8Y1wLEp6cg",
)

SHEETS_BASE_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    "/gviz/tq"
)

CSV_TIMEOUT_SECONDS = 15
CSV_MIN_COLUMNS = 50

# Zero-based CSV column indices.
# Each daily block begins with Shift, Name, Zone.
DAY_BLOCK_COLS = [0, 7, 14, 21, 28, 35, 42]

# Date row = spreadsheet row 10, Python index 9.
DATE_ROW_INDEX = 9

# First assignment row = spreadsheet row 12, Python index 11.
DATA_START_INDEX = 11

KNOWN_ZONES = [
    "Y/TL", "R2A", "R2B", "Y/C", "REG",
    "R1", "YS", "Y2", "GZ", "GS", "Y",
]

NO_ZONE_SHIFTS = {
    "SD", "S/D", "OD", "O/D", "AL", "MC", "OH",
    "AD", "COURSE", "OL", "LEAVE", "OFF", "REST", "SL",
}

SHIFT_TIMES = {
    "AM": ("08:00", "16:00"),
    "PM": ("14:00", "22:00"),
    "N": ("21:00", "09:00"),
    "NIGHT": ("21:00", "09:00"),
}

MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("shift_roster")


# ============================================================
# ERRORS
# ============================================================

class SheetFetchError(Exception):
    """Raised when a Google Sheet cannot be fetched or parsed."""


# ============================================================
# GOOGLE SHEETS CSV
# ============================================================

def fetch_sheet_csv(tab_name: str) -> list[list[str]]:
    """
    Fetch a published/readable Google Sheet tab as CSV.

    Rows are padded so all expected roster columns can be accessed.
    Network and HTTP errors are raised instead of silently returning
    an empty roster.
    """
    params = {
        "tqx": "out:csv",
        "sheet": tab_name,
        "t": str(time.time_ns()),
    }
    url = f"{SHEETS_BASE_URL}?{urlencode(params)}"

    try:
        response = requests.get(
            url,
            timeout=CSV_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.exception("Failed to fetch Google Sheet tab %s", tab_name)
        raise SheetFetchError(
            f"Could not fetch the Google Sheets tab '{tab_name}'."
        ) from exc

    # Decode explicitly, tolerating a UTF-8 BOM.
    try:
        text = response.content.decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text)))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise SheetFetchError(
            f"Could not parse CSV for tab '{tab_name}'."
        ) from exc

    cleaned_rows = []

    for row in rows:
        cleaned = [
            cell.replace("\xa0", " ").strip()
            for cell in row
        ]

        if len(cleaned) < CSV_MIN_COLUMNS:
            cleaned.extend(
                [""] * (CSV_MIN_COLUMNS - len(cleaned))
            )

        cleaned_rows.append(cleaned)

    logger.info(
        "Fetched tab=%s rows=%d",
        tab_name,
        len(cleaned_rows),
    )

    return cleaned_rows


# ============================================================
# NORMALIZATION AND NAME MATCHING
# ============================================================

def normalize_text(value: str) -> str:
    """Normalize case, whitespace and common invisible characters."""
    if not value:
        return ""

    value = (
        value.replace("\xa0", " ")
        .replace("\u200b", "")
        .replace("\ufeff", "")
    )

    return re.sub(r"\s+", " ", value).strip().upper()


def normalize_name(value: str) -> str:
    """
    Normalize a name while ignoring common punctuation differences.
    The complete normalized name is matched, not an arbitrary token.
    """
    value = normalize_text(value)
    value = re.sub(r"[^A-Z0-9\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def match_flexible_name(
    cell_text: str,
    app_name: str,
    roster_name: str,
) -> bool:
    """
    Match a complete name or complete name phrase.

    Examples:
      JOVI matches JOVI
      DR JOVI does not match JOVI by accidental substring
      TAN JOVI matches TAN JOVI if that is the configured alias
    """
    cell = normalize_name(cell_text)

    if not cell:
        return False

    candidates = {
        normalize_name(name)
        for name in (app_name, roster_name)
        if normalize_name(name)
    }

    if cell in candidates:
        return True

    # Permit a full name phrase embedded in a larger name field,
    # but do not match individual words from a multiword name.
    for candidate in candidates:
        if re.search(
            r"(?<![A-Z0-9])"
            + re.escape(candidate)
            + r"(?![A-Z0-9])",
            cell,
        ):
            return True

    return False


def match_locum_name(
    cells: list[str],
    app_name: str,
    locum_name: str,
) -> bool:
    """Check both individual locum name cells and their combined text."""
    for cell in cells:
        if match_flexible_name(cell, app_name, locum_name):
            return True

    return match_flexible_name(
        " ".join(cells),
        app_name,
        locum_name,
    )


# ============================================================
# DATE PARSING
# ============================================================

def parse_date_string(value: str) -> str:
    """
    Return YYYY-MM-DD for supported date formats.

    Malaysian day/month/year formats are checked before the
    month/day/year format.
    """
    if not value:
        return ""

    text = normalize_text(value)

    # Google Visualization date: Date(2026,9,14)
    match = re.search(
        r"DATE\(\s*(\d{4})\s*,\s*(\d{1,2})"
        r"\s*,\s*(\d{1,2})\s*\)",
        text,
    )

    if match:
        try:
            parsed = date(
                int(match.group(1)),
                int(match.group(2)) + 1,
                int(match.group(3)),
            )
            return parsed.isoformat()
        except ValueError:
            return ""

    # Exact, common date formats.
    formats = (
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d.%m.%Y",
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%d/%m/%y",
        "%d-%m-%y",
        "%d.%m.%y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass

    # Find a date embedded in a longer cell value, such as
    # "Wednesday 14/10/2026".
    embedded_patterns = (
        r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)",
        r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{4})(?!\d)",
    )

    for pattern in embedded_patterns:
        match = re.search(pattern, text)

        if match:
            try:
                parsed = date(
                    int(match.group(3)),
                    int(match.group(2)),
                    int(match.group(1)),
                )
                return parsed.isoformat()
            except ValueError:
                return ""

    return ""


def get_weekly_dates(
    weekly_sheet: list[list[str]],
) -> list[str]:
    """
    Determine the date assigned to each weekday.

    Primary source:
      Monday B, Tuesday I, Wednesday P, Thursday W,
      Friday AD, Saturday AK, Sunday AR.

    If some daily date cells are blank, use a valid date from
    the same date row to infer the remaining dates by day offset.
    """
    if len(weekly_sheet) <= DATE_ROW_INDEX:
        raise SheetFetchError(
            "Weekly sheet does not contain spreadsheet row 10."
        )

    date_row = weekly_sheet[DATE_ROW_INDEX]
    dates: list[date | None] = []

    # The date is normally in the second column of each day block.
    for block_col in DAY_BLOCK_COLS:
        date_col = block_col + 1
        raw_value = date_row[date_col]
        parsed = parse_date_string(raw_value)

        try:
            dates.append(
                date.fromisoformat(parsed) if parsed else None
            )
        except ValueError:
            dates.append(None)

    # Use the first explicitly available date as an anchor.
    anchor_index = next(
        (i for i, dt in enumerate(dates) if dt is not None),
        None,
    )

    if anchor_index is None:
        # Fallback: inspect the entire date row for a date.
        for col_idx, cell in enumerate(date_row):
            parsed = parse_date_string(cell)

            if not parsed:
                continue

            try:
                anchor_date = date.fromisoformat(parsed)
            except ValueError:
                continue

            anchor_index = min(col_idx // 7, 6)
            dates[anchor_index] = anchor_date
            break

    if anchor_index is None:
        raise SheetFetchError(
            "No valid date was found on spreadsheet row 10. "
            "Refusing to assign shifts to an assumed week."
        )

    anchor_date = dates[anchor_index]

    assert anchor_date is not None

    # Fill only missing daily dates using the anchor date.
    for day_index in range(7):
        if dates[day_index] is None:
            dates[day_index] = anchor_date + timedelta(
                days=day_index - anchor_index
            )

    # If the sheet supplies multiple dates, retain them but warn
    # when they are inconsistent with the expected weekday sequence.
    result = [dt.isoformat() for dt in dates if dt is not None]

    if len(result) != 7:
        raise SheetFetchError(
            "Unable to determine all seven weekly dates."
        )

    parsed_dates = [date.fromisoformat(value) for value in result]

    for i in range(1, 7):
        expected = parsed_dates[i - 1] + timedelta(days=1)

        if parsed_dates[i] != expected:
            logger.warning(
                "Non-consecutive roster dates: day %d=%s, day %d=%s",
                i,
                parsed_dates[i - 1],
                i + 1,
                parsed_dates[i],
            )

    logger.info("Weekly dates Monday-Sunday: %s", result)

    return result


# ============================================================
# SHIFT AND ZONE PARSING
# ============================================================

def canonical_shift(raw_shift: str) -> str:
    """
    Normalize known shift codes without silently converting
    unknown values to AM.
    """
    value = normalize_text(raw_shift)
    compact = re.sub(r"\s+", "", value)

    aliases = {
        "AM": "AM",
        "PM": "PM",
        "N": "N",
        "NIGHT": "NIGHT",
        "SD": "SD",
        "S/D": "SD",
        "OD": "OD",
        "O/D": "OD",
        "AL": "AL",
        "MC": "MC",
        "OH": "OH",
        "AD": "AD",
        "COURSE": "COURSE",
        "OL": "OL",
        "LEAVE": "LEAVE",
        "OFF": "OFF",
        "REST": "REST",
        "SL": "SL",
    }

    return aliases.get(compact, value)


def extract_zone(text: str) -> str:
    """Extract a known zone using token boundaries."""
    value = normalize_text(text)

    if not value:
        return ""

    # Longest first prevents a shorter code from winning over
    # a more specific code such as Y/TL.
    for zone in sorted(KNOWN_ZONES, key=len, reverse=True):
        if re.search(
            r"(?<![A-Z0-9/])"
            + re.escape(zone)
            + r"(?![A-Z0-9/])",
            value,
        ):
            return zone

    return ""


def get_shift_times(shift: str) -> tuple[str, str]:
    """Return the configured start/end times, or blanks if unknown."""
    return SHIFT_TIMES.get(shift, ("", ""))


# ============================================================
# ALIASES
# ============================================================

def get_user_aliases(app_name: str) -> tuple[str, str]:
    """
    Aliases tab:
      Column A = app username
      Column B = Weekly roster name
      Column C = Locum roster name

    If the user has no alias row, use the app username for both.
    """
    app_name = normalize_text(app_name)

    weekly_name = app_name
    locum_name = app_name

    aliases_sheet = fetch_sheet_csv("Aliases")

    for row in aliases_sheet:
        if len(row) < 3:
            continue

        alias_app_name = normalize_text(row[0])

        if not alias_app_name or alias_app_name != app_name:
            continue

        weekly_name = normalize_text(row[1]) or app_name
        locum_name = normalize_text(row[2]) or app_name

        logger.info(
            "Alias resolved for %s: weekly=%s locum=%s",
            app_name,
            weekly_name,
            locum_name,
        )

        return weekly_name, locum_name

    logger.info("No alias row found for %s; using app name", app_name)

    return weekly_name, locum_name


# ============================================================
# WEEKLY ROSTER
# ============================================================

def extract_weekly_shifts(
    app_name: str,
    weekly_name: str,
) -> list[dict]:
    """
    Return every matching weekly assignment.

    A matching name is checked only in the Name column.
    Each assignment row is evaluated independently; no shift type
    is inherited from the preceding row.
    """
    weekly_sheet = fetch_sheet_csv("Weekly")

    if len(weekly_sheet) <= DATA_START_INDEX:
        raise SheetFetchError(
            "Weekly sheet has no assignment rows beginning at row 12."
        )

    block_dates = get_weekly_dates(weekly_sheet)
    results = []

    logger.info(
        "Parsing weekly roster for app=%s weekly_name=%s",
        app_name,
        weekly_name,
    )

    for day_index, block_col in enumerate(DAY_BLOCK_COLS):
        assigned_date = block_dates[day_index]

        for row_index in range(DATA_START_INDEX, len(weekly_sheet)):
            row = weekly_sheet[row_index]

            raw_shift = row[block_col].strip()
            raw_name = row[block_col + 1].strip()
            raw_zone = row[block_col + 2].strip()

            # Ignore rows with no name.
            if not raw_name:
                continue

            matched = match_flexible_name(
                raw_name,
                app_name,
                weekly_name,
            )

            if not matched:
                continue

            shift = canonical_shift(raw_shift)

            # Do not silently treat blank/unknown shifts as AM.
            if not shift:
                logger.warning(
                    "Matched name but blank shift: date=%s row=%d "
                    "name=%r",
                    assigned_date,
                    row_index + 1,
                    raw_name,
                )
                shift = "UNKNOWN"

            start_time, end_time = get_shift_times(shift)

            zone = ""
            if shift not in NO_ZONE_SHIFTS and shift != "UNKNOWN":
                zone = extract_zone(raw_zone)

            if (
                shift not in SHIFT_TIMES
                and shift not in NO_ZONE_SHIFTS
                and shift != "UNKNOWN"
            ):
                logger.warning(
                    "Unknown shift code %r at spreadsheet row %d",
                    raw_shift,
                    row_index + 1,
                )

            assignment = {
                "date": assigned_date,
                "shift": shift,
                "zone": zone,
                "assignmentType": shift,
                "startTime": start_time,
                "endTime": end_time,
                "isLocum": False,
            }

            results.append(assignment)

            logger.info(
                "Weekly match: date=%s row=%d name=%r "
                "shift=%s zone=%s",
                assigned_date,
                row_index + 1,
                raw_name,
                shift,
                zone,
            )

    # Preserve multiple assignments; do not break after first match.
    results.sort(
        key=lambda item: (
            item["date"],
            item["startTime"] or "99:99",
            item["shift"],
        )
    )

    logger.info(
        "Weekly parsing complete: user=%s assignments=%d",
        app_name,
        len(results),
    )

    return results


# ============================================================
# LOCUM TIME AND DATE PARSING
# ============================================================

def parse_locum_time_slot(time_str: str) -> tuple[str, str]:
    """Preserve the existing locum time-slot conventions."""
    value = normalize_text(time_str).replace(" ", "")

    if not value:
        return ("19:00", "23:00")

    if any(
        token in value
        for token in (
            "7PM", "7.00", "19:", "1900", "7-11",
            "11PM", "NIGHT", "EVE",
        )
    ):
        return ("19:00", "23:00")

    if any(
        token in value
        for token in ("10AM", "10:", "1000", "10-2", "MORNING")
    ):
        return ("10:00", "14:00")

    if any(
        token in value
        for token in (
            "2PM", "14:", "1400", "2-6",
            "6PM", "AFTERNOON",
        )
    ):
        return ("14:00", "18:00")

    logger.warning(
        "Unrecognized locum time slot %r; using legacy default 19:00-23:00",
        time_str,
    )

    return ("19:00", "23:00")


def get_locum_month_year(
    locum_sheet: list[list[str]],
) -> tuple[str, str]:
    """
    Infer the locum month/year from the first five rows.
    Fall back to the current month/year if not found.
    """
    header_text = normalize_text(
        " ".join(
            cell
            for row in locum_sheet[:5]
            for cell in row
        )
    )

    target_month = datetime.now().month
    target_year = datetime.now().year

    month_pattern = (
        r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|"
        r"JULY|AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER|"
        r"JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b"
    )

    month_match = re.search(month_pattern, header_text)

    if month_match:
        month_text = month_match.group(1)[:3]
        if month_text in MONTHS:
            target_month = MONTHS[month_text]

    year_match = re.search(r"\b(20\d{2})\b", header_text)

    if year_match:
        target_year = int(year_match.group(1))

    return str(target_month).zfill(2), str(target_year)


def parse_day_number(value: str) -> str | None:
    """Extract a day number from a locum date cell."""
    if not value:
        return None

    value = normalize_text(value)

    # Prefer an explicit date, e.g. 14/10/2026.
    full_date = parse_date_string(value)

    if full_date:
        return full_date[8:10]

    match = re.search(r"(?<!\d)([1-9]|[12]\d|3[01])(?!\d)", value)

    if not match:
        return None

    day_number = int(match.group(1))

    if 1 <= day_number <= 31:
        return str(day_number).zfill(2)

    return None


# ============================================================
# LOCUM ROSTER
# ============================================================

def extract_locum_shifts(
    app_name: str,
    locum_name: str,
) -> list[dict]:
    """
    Preserve the current Locum tab layout:

    Left block:
      A = date, C = time, D:F = name fields

    Right block:
      G = date, I = time, J:L = name fields

    Date and time values carry forward until another value appears.
    """
    locum_sheet = fetch_sheet_csv("Locum")

    if not locum_sheet:
        return []

    target_month, target_year = get_locum_month_year(locum_sheet)

    last_date_left = "01"
    last_date_right = "16"

    last_time_left = ("19:00", "23:00")
    last_time_right = ("19:00", "23:00")

    results = []

    for row_index, row in enumerate(locum_sheet[1:], start=2):
        # ----------------------------------------------------
        # LEFT BLOCK
        # ----------------------------------------------------
        if row[0].strip():
            day = parse_day_number(row[0])
            if day:
                last_date_left = day

        if row[2].strip():
            last_time_left = parse_locum_time_slot(row[2])

        left_names = row[3:6]

        if match_locum_name(left_names, app_name, locum_name):
            results.append({
                "date": (
                    f"{target_year}-{target_month}-{last_date_left}"
                ),
                "shift": "Locum",
                "assignmentType": "Internal Locum",
                "startTime": last_time_left[0],
                "endTime": last_time_left[1],
                "facilityName": "ED Department Locum",
                "isLocum": True,
            })

            logger.info(
                "Locum left-block match: row=%d date=%s-%s-%s",
                row_index,
                target_year,
                target_month,
                last_date_left,
            )

        # ----------------------------------------------------
        # RIGHT BLOCK
        # ----------------------------------------------------
        if row[6].strip():
            day = parse_day_number(row[6])
            if day:
                last_date_right = day

        if row[8].strip():
            last_time_right = parse_locum_time_slot(row[8])

        right_names = row[9:12]

        if match_locum_name(right_names, app_name, locum_name):
            results.append({
                "date": (
                    f"{target_year}-{target_month}-{last_date_right}"
                ),
                "shift": "Locum",
                "assignmentType": "Internal Locum",
                "startTime": last_time_right[0],
                "endTime": last_time_right[1],
                "facilityName": "ED Department Locum",
                "isLocum": True,
            })

            logger.info(
                "Locum right-block match: row=%d date=%s-%s-%s",
                row_index,
                target_year,
                target_month,
                last_date_right,
            )

    results.sort(
        key=lambda item: (
            item["date"],
            item["startTime"],
        )
    )

    logger.info(
        "Locum parsing complete: user=%s assignments=%d",
        app_name,
        len(results),
    )

    return results


# ============================================================
# API ENDPOINTS
# ============================================================

def normalized_app_name(user_name: str) -> str:
    name = normalize_text(user_name)

    if not name:
        raise HTTPException(
            status_code=422,
            detail="user_name must not be blank.",
        )

    return name


def load_aliases_or_502(app_name: str) -> tuple[str, str]:
    try:
        return get_user_aliases(app_name)
    except SheetFetchError as exc:
        logger.exception("Aliases tab unavailable")
        raise HTTPException(
            status_code=502,
            detail="Could not read the Aliases tab from Google Sheets.",
        ) from exc


@app.get("/")
def health_check():
    return {
        "status": "ok",
        "message": "EM Shift API Online",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/sync-weekly")
def sync_weekly(user_name: str = Form(...)):
    app_name = normalized_app_name(user_name)
    weekly_name, _ = load_aliases_or_502(app_name)

    try:
        weekly_data = extract_weekly_shifts(
            app_name,
            weekly_name,
        )
    except SheetFetchError as exc:
        logger.exception("Weekly roster fetch/parse failed")
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    weekly_data.sort(key=lambda item: item["date"])

    return {
        "status": "success",
        "count": len(weekly_data),
        "data": weekly_data,
    }


@app.post("/sync-locum")
def sync_locum(user_name: str = Form(...)):
    app_name = normalized_app_name(user_name)
    _, locum_name = load_aliases_or_502(app_name)

    try:
        locum_data = extract_locum_shifts(
            app_name,
            locum_name,
        )
    except SheetFetchError as exc:
        logger.exception("Locum roster fetch/parse failed")
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    locum_data.sort(key=lambda item: item["date"])

    return {
        "status": "success",
        "count": len(locum_data),
        "data": locum_data,
    }


@app.post("/sync-roster")
def sync_roster(user_name: str = Form(...)):
    app_name = normalized_app_name(user_name)
    weekly_name, locum_name = load_aliases_or_502(app_name)

    try:
        weekly_data = extract_weekly_shifts(
            app_name,
            weekly_name,
        )
        locum_data = extract_locum_shifts(
            app_name,
            locum_name,
        )
    except SheetFetchError as exc:
        logger.exception("Combined roster sync failed")
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    combined = weekly_data + locum_data
    combined.sort(
        key=lambda item: (
            item["date"],
            item.get("startTime") or "99:99",
        )
    )

    return {
        "status": "success",
        "count": len(combined),
        "data": combined,
    }
```
