from __future__ import annotations

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
    'pisc_start':  377,   # pool type (25m/50m): 377 <= X < 410
    'pisc_end':    410,
    # M.Ponderada starts at ~414; revert to 410 to avoid pisc-column overlap
    'mpond_start': 410,
    'mpond_end':   448,
}

TIME_RE = re.compile(r'(\d{1,2}:\d{2}\.\d{2}|\d{2}\.\d{2})')
POOL_RE = re.compile(r'(25|50)\s*m', re.IGNORECASE)

# X threshold for the Tiempo (result time) column in FNCV results PDFs.
RES_TIEMPO_X = 420

# Three-column boundaries for results PDFs — mirror inscriptions column thresholds.
# Col A  X < POS_NAME_END : position number + swimmer name
# Col B  POS_NAME_END ≤ X < YEAR_COL_END : birth year (and any overflow chars)
# Col C  X ≥ YEAR_COL_END : club name, qualifier, points, split times
POS_NAME_END = 213   # = INS_COL['ano_start']
YEAR_COL_END = 233   # = INS_COL['ano_end']

# Lines in results PDFs that carry only split times, not swimmer results.
# E.g. "50m: 31.77  150m: 1:48.23 ..."  or  "100m: 1:09.08  200m: 2:26.20 ..."
SPLIT_ROW_RE = re.compile(r'^\d+\s*m\s*:', re.IGNORECASE)

# Matches individual-event headers, e.g.:
#   "Prueba 1Masc., 200m LibreAbs."
#   "Prueba 2 Femenino, 400m Libre"
EVENT_HEADER_RE = re.compile(
    r'Prueba\s+(\d+)[^A-Za-z]*(Masc\.?|Masculino|Fem\.?|Femenino|Mixto)[,.]?\s*(\d+)\s*m\s*'
    r'(Libre|Espalda|Mariposa|Braza|Estilos)',
    re.IGNORECASE,
)

# Matches relay event headers, e.g.:
#   "Prueba 5 Masc., 4x100m Libre"
#   "Prueba 6 Femenino, 4 x 50m Estilos"
RELAY_HEADER_RE = re.compile(
    r'Prueba\s+(\d+)[^A-Za-z]*(Masc\.?|Masculino|Fem\.?|Femenino|Mixto)[,.]?\s*'
    r'(\d+)\s*[xX]\s*(\d+)\s*m\s*'
    r'(Libre|Espalda|Mariposa|Braza|Estilos)',
    re.IGNORECASE,
)


def normalize_name(name: str) -> str:
    """Uppercase, remove accents, collapse whitespace."""
    nfkd = unicodedata.normalize('NFKD', name)
    ascii_str = ''.join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', ascii_str).strip().upper()


def name_match_key(normalized_name: str) -> str:
    """
    Collision-resistant key for matching inscriptions to results.
    Results PDFs abbreviate first names ('PEREZ, Ana' vs 'PEREZ, A.'), so we
    reduce both sides to 'SURNAME,FIRST_INITIAL' before comparing.
    """
    if not normalized_name:
        return ''
    parts = normalized_name.split(',', 1)
    surname = parts[0].strip()
    if len(parts) > 1:
        given = parts[1].strip()
        if given:
            return f'{surname},{given[0]}'
    return surname


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
# Event header parsing
# ---------------------------------------------------------------------------

def _parse_event_header(line: str) -> dict | None:
    """
    Return an event dict if the line is an event header, else None.
    Relay events (4x100m) are detected first; relay=True is set on them
    and distance is stored as total metres (legs × leg_distance).
    """
    # Relay: "Prueba N Gender, LxDm Stroke"
    m = RELAY_HEADER_RE.search(line)
    if m:
        gender_raw = m.group(2).upper()
        gender = 'M' if 'MASC' in gender_raw else ('F' if 'FEM' in gender_raw else 'X')
        legs = int(m.group(3))
        leg_dist = int(m.group(4))
        return {
            'event_number': int(m.group(1)),
            'gender': gender,
            'distance': legs * leg_dist,
            'stroke': _normalize_stroke(m.group(5).strip().upper()),
            'relay': True,
        }
    # Individual: "Prueba N Gender, Dm Stroke"
    m = EVENT_HEADER_RE.search(line)
    if m:
        gender_raw = m.group(2).upper()
        gender = 'M' if 'MASC' in gender_raw else ('F' if 'FEM' in gender_raw else 'X')
        return {
            'event_number': int(m.group(1)),
            'gender': gender,
            'distance': int(m.group(3)),
            'stroke': _normalize_stroke(m.group(4).strip().upper()),
            'relay': False,
        }
    return None


# ---------------------------------------------------------------------------
# Inscriptions parser
# ---------------------------------------------------------------------------

RESERVAS_RE = re.compile(r'\bReservas?\b', re.IGNORECASE)


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
        'reserve': bool,
      }
    """
    entries = []
    current_event: dict | None = None
    in_reservas = False

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rows = _group_chars_by_row(page)
            for _y, chars in sorted(rows.items()):
                line = _chars_to_text(chars).strip()
                if not line:
                    continue

                # Detect event header line (relay first, then individual)
                ev = _parse_event_header(line)
                if ev is not None:
                    current_event = ev
                    in_reservas = False
                    continue

                # Detect "Reservas" section marker within current event
                if current_event is not None and RESERVAS_RE.search(line):
                    in_reservas = True
                    continue

                if current_event is None:
                    continue

                # Try to parse a swimmer row by column split
                entry = _parse_inscription_row(chars, current_event)
                if entry:
                    entry['reserve'] = in_reservas
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

    club = _chars_to_text(club_chars).strip().rstrip('- ').strip()

    # Pool length from Pisc. column
    pisc_raw = _chars_to_text(pisc_chars).strip()
    pm = POOL_RE.search(pisc_raw)
    pool_length = (pm.group(1) + 'm') if pm else ''

    # M.Ponderada: take the LAST time found anywhere right of the club column.
    # M.Real sits at ~X=343–377, M.Ponderada at ~X=410–448, so the last
    # TIME_RE match in that region is always the ponderada value.
    # This also handles wide times (≥10 min) whose leading digit sits at X<410.
    right_text = _chars_to_text([c for c in chars if c['x0'] > INS_COL['club_end']])
    all_times = TIME_RE.findall(right_text)
    weighted_time = time_to_seconds(all_times[-1]) if all_times else None

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

                # Detect event header (relay first, then individual)
                ev = _parse_event_header(line)
                if ev is not None:
                    current_event = ev
                    continue

                if current_event is None:
                    continue

                # Skip split-time rows ("50m: 31.77  150m: 1:48.23 ...")
                if SPLIT_ROW_RE.match(line):
                    continue

                result = _parse_result_row(chars, current_event)
                if result:
                    results.append(result)

    return results, pool_type


def _parse_result_row(chars: list, event: dict) -> dict | None:
    """
    Parse one result row using a three-column split that mirrors the inscriptions
    column thresholds (same PDF software, same column layout):

      Col A  X < POS_NAME_END (213): position number + swimmer name
      Col B  POS_NAME_END ≤ X < YEAR_COL_END (233): birth-year column.
             May also contain name overflow (long names) and a club prefix
             (first few chars of the club name that land left of 233).
      Col C  X ≥ YEAR_COL_END: club name, qualifier tokens, points, split times.

    Year: first two-digit sequence found in col B (after any name overflow).
    Club: alphabetical prefix found in col B after the year, concatenated with
          the leading letter sequence from col C.
    Time: existing three-tier bold / right-column approach.
    """
    bold_chars    = [c for c in chars if 'Bold' in c.get('fontname', '')]
    regular_chars = [c for c in chars if 'Bold' not in c.get('fontname', '')]

    bold_text = _chars_to_text(bold_chars).strip()
    reg_text  = _chars_to_text(regular_chars).strip()

    # ---- Result time: three-tier detection ----
    right_bold = [c for c in bold_chars if c['x0'] >= RES_TIEMPO_X]
    tm = TIME_RE.search(_chars_to_text(right_bold))
    if not tm:
        tm = TIME_RE.search(bold_text)
    if not tm:
        right_all = [c for c in chars if c['x0'] >= RES_TIEMPO_X]
        tm = TIME_RE.search(_chars_to_text(right_all))
    result_time = time_to_seconds(tm.group(0)) if tm else None

    # ---- DSQ / DNS ----
    dsq = bool(re.search(r'\b(DSQ|DNS|DQ|NP|AB)\b', reg_text + bold_text, re.IGNORECASE))

    if not reg_text:
        return None

    # ---- Three-column split ----
    col_a = [c for c in regular_chars if c['x0'] < POS_NAME_END]
    col_b = [c for c in regular_chars if POS_NAME_END <= c['x0'] < YEAR_COL_END]
    col_c = [c for c in regular_chars if c['x0'] >= YEAR_COL_END]

    col_a_text = _chars_to_text(col_a).strip()
    col_b_text = _chars_to_text(col_b).strip()
    col_c_text = _chars_to_text(col_c).strip()

    # Position: leading digits + '.' in col A
    pos_match = re.match(r'^(\d+)\.', col_a_text)
    position  = int(pos_match.group(1)) if pos_match else None
    if position is None and not dsq:
        return None

    # Swimmer name: text in col A after the position number
    after_pos    = col_a_text[pos_match.end():].strip() if pos_match else col_a_text
    swimmer_name = after_pos.rstrip(',').strip()

    # Col B may start with name overflow (letters before the year digits)
    col_b_rem = col_b_text
    name_overflow_m = re.match(r'^([A-Za-záéíóúÁÉÍÓÚüÜñÑ\s\.\,\-]+)', col_b_rem)
    if name_overflow_m:
        overflow = name_overflow_m.group(1).rstrip(', ').strip()
        if overflow:
            swimmer_name = (swimmer_name + ' ' + overflow).strip()
        col_b_rem = col_b_rem[name_overflow_m.end():]

    if not swimmer_name:
        return None

    # Year: first two-digit sequence in the remaining col B text
    year_m = re.search(r'\d{2}', col_b_rem)
    year   = year_m.group(0) if year_m else ''

    # Club prefix: alphabetical chars in col B after the year (and digit/time noise)
    after_yr    = col_b_rem[year_m.end():] if year_m else col_b_rem
    club_prefix = re.sub(r'^[\d\.\s:,]+', '', after_yr).strip()

    # Full club text = col B prefix + col C; club regex stops at first digit
    full_right = (club_prefix + col_c_text).strip()
    club_m     = re.match(r'^([A-Za-záéíóúÁÉÍÓÚüÜñÑ\s\.\-]+)', full_right)
    club       = club_m.group(1).strip().rstrip('- ').strip() if club_m else ''
    club       = re.sub(r'\s+[A-Z]{2}$', '', club).strip().rstrip('- ').strip()

    # Points: first decimal number after the club text
    after_club = full_right[len(club_m.group(0)):] if club_m else full_right
    pts_m      = re.search(r'(\d+[,\.]\d+)', after_club)
    if pts_m:
        try:
            points = float(pts_m.group(1).replace(',', '.'))
        except ValueError:
            points = None
    else:
        points = None

    if result_time is None and not dsq:
        return None

    return {
        **event,
        'position':                position,
        'swimmer_name':            swimmer_name,
        'swimmer_name_normalized': normalize_name(swimmer_name),
        'year':                    year,
        'club':                    club,
        'result_time':             result_time,
        'points':                  points,
        'dsq':                     dsq,
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
