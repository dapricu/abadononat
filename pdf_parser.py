"""
PDF parser for Spanish swimming competition files (FNCV/RFEN format).

Inscriptions PDF: character-level column splitting by X position.
Results PDF: font-based separation (Helvetica-Bold = result time, regular = rest).
"""
import re
import unicodedata
import pdfplumber


# --- Column thresholds for inscriptions PDF ---
INS_COL = {
    'seq_end':     213,   # seq + name: X < 213
    'ano_start':   213,   # year: 213 <= X < 233
    'ano_end':     233,
    'club_end':    343,   # club: 233 <= X < 343 (may overflow into next cols)
    'pisc_start':  377,   # pool type (25m/50m): 377 <= X < 408
    'pisc_end':    408,
    'mpond_start': 408,   # M.Ponderada: 408 <= X < 446
    'mpond_end':   446,
}

TIME_RE = re.compile(r'(\d{1,2}:\d{2}\.\d{2}|\d{2}\.\d{2})')
POOL_RE = re.compile(r'(25|50)\s*m', re.IGNORECASE)
EVENT_HEADER_RE = re.compile(
    r'(\d+)\s*[–\-]\s*(MASCULINO|FEMENINO|MIXTO)\s+(\d+)\s*m\s+(\w[\w\s]+)',
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Uppercase, remove accents, collapse whitespace."""
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_str = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', ascii_str).strip().upper()


def time_to_seconds(t: str) -> float | None:
    """Convert 'M:SS.cc' or 'SS.cc' to float seconds. Returns None on failure."""
    if not t:
        return None
    t = t.strip()
    try:
        if ':' in t:
            parts = t.split(':')
            return int(parts[0]) * 60 + float(parts[1])
        return float(t)
    except (ValueError, IndexError):
        return None


def _group_chars_by_row(page, y_tolerance: int = 4) -> dict:
    """Group page characters into rows by top-Y proximity."""
    rows: dict[float, list] = {}
    for c in page.chars:
        matched = None
        for yk in rows:
            if abs(yk - c['top']) <= y_tolerance:
                matched = yk
                break
        if matched is None:
            rows[c['top']] = []
            matched = c['top']
        rows[matched].append(c)
    # Sort each row by X
    for yk in rows:
        rows[yk].sort(key=lambda c: c['x0'])
    return rows


def _chars_to_text(chars) -> str:
    return ''.join(c['text'] for c in chars)


# ---------------------------------------------------------------------------
# Inscriptions parser
# ---------------------------------------------------------------------------

def parse_inscriptions(pdf_path: str) -> list[dict]:
    """
    Parse an inscriptions PDF and return a list of entry dicts:
      {
        'event_number': int,
        'gender': str,          # 'M' | 'F' | 'X'
        'distance': int,
        'stroke': str,
        'seq': int,
        'swimmer_name': str,
        'swimmer_name_normalized': str,
        'year': str,
        'club': str,
        'pool_length': str,     # '25m' | '50m' | ''
        'weighted_time': float | None,
      }
    """
    entries = []
    current_event: dict | None = None

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rows = _group_chars_by_row(page)
            for _y, chars in sorted(rows.items()):
                line = _chars_to_text(chars).strip()
                if not line:
                    continue

                # Detect event header line
                m = EVENT_HEADER_RE.search(line)
                if m:
                    gender_raw = m.group(2).upper()
                    gender = 'M' if 'MASC' in gender_raw else ('F' if 'FEM' in gender_raw else 'X')
                    stroke_raw = m.group(4).strip().upper()
                    current_event = {
                        'event_number': int(m.group(1)),
                        'gender': gender,
                        'distance': int(m.group(3)),
                        'stroke': _normalize_stroke(stroke_raw),
                    }
                    continue

                if current_event is None:
                    continue

                # Try to parse a swimmer row by column split
                entry = _parse_inscription_row(chars, current_event)
                if entry:
                    entries.append(entry)

    return entries


def _parse_inscription_row(chars: list, event: dict) -> dict | None:
    """Extract one inscription row using X-position column thresholds."""
    seq_name_chars = [c for c in chars if c['x0'] < INS_COL['seq_end']]
    ano_chars      = [c for c in chars if INS_COL['ano_start'] <= c['x0'] < INS_COL['ano_end']]
    # club chars: between ano_end and pisc_start, but may overlap with mpond/mreal
    club_chars     = [c for c in chars if INS_COL['ano_end'] <= c['x0'] < INS_COL['club_end']]
    pisc_chars     = [c for c in chars if INS_COL['pisc_start'] <= c['x0'] < INS_COL['pisc_end']]
    mpond_chars    = [c for c in chars if INS_COL['mpond_start'] <= c['x0'] < INS_COL['mpond_end']]

    if not seq_name_chars:
        return None

    seq_name_raw = _chars_to_text(seq_name_chars).strip()
    # seq_name_raw starts with a number (sequence) followed by swimmer name
    seq_match = re.match(r'^(\d+)(.*)', seq_name_raw)
    if not seq_match:
        return None
    seq = int(seq_match.group(1))
    swimmer_name = seq_match.group(2).strip().rstrip(',').strip()
    if not swimmer_name:
        return None

    year = _chars_to_text(ano_chars).strip()
    # year should be 2-4 digits
    year_match = re.search(r'\d{2,4}', year)
    year = year_match.group(0) if year_match else ''

    club = _chars_to_text(club_chars).strip()

    # Pool length from Pisc. column
    pisc_raw = _chars_to_text(pisc_chars).strip()
    pm = POOL_RE.search(pisc_raw)
    pool_length = (pm.group(1) + 'm') if pm else ''

    # M.Ponderada
    mpond_raw = _chars_to_text(mpond_chars).strip()
    tm = TIME_RE.search(mpond_raw)
    weighted_time = time_to_seconds(tm.group(0)) if tm else None

    # Only keep rows that look like real entries (have a time or at least year)
    if not year and weighted_time is None:
        return None

    return {
        **event,
        'seq': seq,
        'swimmer_name': swimmer_name,
        'swimmer_name_normalized': normalize_name(swimmer_name),
        'year': year,
        'club': club,
        'pool_length': pool_length,
        'weighted_time': weighted_time,
    }


# ---------------------------------------------------------------------------
# Results parser
# ---------------------------------------------------------------------------

def parse_results(pdf_path: str) -> tuple[list[dict], str]:
    """
    Parse a results PDF.

    Returns:
        (results_list, competition_pool_type)

    results_list entries:
      {
        'event_number': int,
        'gender': str,
        'distance': int,
        'stroke': str,
        'position': int | None,
        'swimmer_name': str,
        'swimmer_name_normalized': str,
        'year': str,
        'club': str,
        'result_time': float | None,
        'points': float | None,
        'dsq': bool,
      }

    competition_pool_type: '25m' | '50m' | ''
    """
    results = []
    current_event: dict | None = None
    pool_type = ''

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Try to detect pool type from page text if not yet found
            if not pool_type:
                page_text = page.extract_text() or ''
                pm = POOL_RE.search(page_text)
                if pm:
                    pool_type = pm.group(1) + 'm'

            rows = _group_chars_by_row(page)
            for _y, chars in sorted(rows.items()):
                line = _chars_to_text(chars).strip()
                if not line:
                    continue

                # Detect event header
                m = EVENT_HEADER_RE.search(line)
                if m:
                    gender_raw = m.group(2).upper()
                    gender = 'M' if 'MASC' in gender_raw else ('F' if 'FEM' in gender_raw else 'X')
                    stroke_raw = m.group(4).strip().upper()
                    current_event = {
                        'event_number': int(m.group(1)),
                        'gender': gender,
                        'distance': int(m.group(3)),
                        'stroke': _normalize_stroke(stroke_raw),
                    }
                    continue

                if current_event is None:
                    continue

                result = _parse_result_row(chars, current_event)
                if result:
                    results.append(result)

    return results, pool_type


def _parse_result_row(chars: list, event: dict) -> dict | None:
    """
    Use font-based separation:
      Bold chars   → result time (e.g. "1:47.15")
      Regular chars → position, name, year, club, points, splits
    """
    bold_chars    = [c for c in chars if 'Bold' in c.get('fontname', '')]
    regular_chars = [c for c in chars if 'Bold' not in c.get('fontname', '')]

    bold_text = _chars_to_text(bold_chars).strip()
    reg_text  = _chars_to_text(regular_chars).strip()

    # Result time from bold
    tm = TIME_RE.search(bold_text)
    result_time = time_to_seconds(tm.group(0)) if tm else None

    # DSQ / DNS rows
    dsq = bool(re.search(r'\b(DSQ|DNS|DQ|NP|AB)\b', reg_text + bold_text, re.IGNORECASE))

    if not reg_text:
        return None

    # reg_text format: "<pos>.<NAME, Firstname><year><club><points><splits...>"
    # Position: leading digits before first '.'
    pos_match = re.match(r'^(\d+)\.', reg_text)
    position = int(pos_match.group(1)) if pos_match else None

    # After position, the rest: "NAME, Firstname<year><club><points>"
    after_pos = reg_text[pos_match.end():].strip() if pos_match else reg_text

    # Year: 2-digit year embedded somewhere, typically after name
    # Strategy: split on digit runs; the 2-digit year follows the name
    # We look for the first standalone 2-digit number (birth year 90-20)
    name_year_match = re.match(r'^(.*?)(\d{2})([A-Z].*)?$', after_pos)
    if name_year_match:
        raw_name = name_year_match.group(1).strip().rstrip(',').strip()
        year = name_year_match.group(2)
        rest = name_year_match.group(3) or ''
    else:
        raw_name = after_pos
        year = ''
        rest = ''

    swimmer_name = raw_name.strip()
    if not swimmer_name:
        return None

    # Club: first part of rest before digits (points/splits)
    club_match = re.match(r'^([A-Za-záéíóúÁÉÍÓÚüÜñÑ\s\.\-]+)', rest)
    club = club_match.group(1).strip() if club_match else ''

    # Points: first number after club
    points_match = re.search(r'(\d+[,\.]\d+)', rest[len(club):])
    if points_match:
        points_str = points_match.group(1).replace(',', '.')
        try:
            points = float(points_str)
        except ValueError:
            points = None
    else:
        points = None

    if not swimmer_name or (result_time is None and not dsq):
        return None

    return {
        **event,
        'position': position,
        'swimmer_name': swimmer_name,
        'swimmer_name_normalized': normalize_name(swimmer_name),
        'year': year,
        'club': club,
        'result_time': result_time,
        'points': points,
        'dsq': dsq,
    }


# ---------------------------------------------------------------------------
# Stroke normalization
# ---------------------------------------------------------------------------

STROKE_MAP = {
    'LIBRE':     'Libre',
    'FREE':      'Libre',
    'ESPALDA':   'Espalda',
    'BACK':      'Espalda',
    'BRAZA':     'Braza',
    'BREAST':    'Braza',
    'MARIPOSA':  'Mariposa',
    'BUTTERFLY': 'Mariposa',
    'FLY':       'Mariposa',
    'ESTILOS':   'Estilos',
    'MEDLEY':    'Estilos',
    'IM':        'Estilos',
}


def _normalize_stroke(raw: str) -> str:
    raw_up = raw.upper()
    for key, val in STROKE_MAP.items():
        if key in raw_up:
            return val
    return raw.strip().title()
