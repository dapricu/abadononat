from __future__ import annotations

"""
Flask web application for swimming competition analysis.
"""
import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, g,
)
from werkzeug.utils import secure_filename

from pdf_parser import parse_inscriptions, parse_results
from analyzer import analyze, compute_copa_classification, venue_ranking_analysis, seconds_to_time

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
UPLOAD_FOLDER = BASE_DIR / 'uploads'
DB_PATH = BASE_DIR / 'swimming.db'
ALLOWED_EXTENSIONS = {'pdf'}

UPLOAD_FOLDER.mkdir(exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-me')
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA journal_mode=WAL')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS competitions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            date       TEXT,
            pool_type  TEXT DEFAULT '',
            venue      TEXT DEFAULT '',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS events (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
            number         INTEGER,
            gender         TEXT,
            distance       INTEGER,
            stroke         TEXT,
            relay          INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS entries (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id                 INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            seq                      INTEGER,
            swimmer_name             TEXT,
            swimmer_name_normalized  TEXT,
            year                     TEXT,
            club                     TEXT,
            pool_length              TEXT,
            weighted_time            REAL,
            reserve                  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS results (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id                 INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
            position                 INTEGER,
            swimmer_name             TEXT,
            swimmer_name_normalized  TEXT,
            year                     TEXT,
            club                     TEXT,
            result_time              REAL,
            points                   REAL,
            dsq                      INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS copa_clubs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS copa_clubs_competitions (
            copa_id        INTEGER NOT NULL REFERENCES copa_clubs(id) ON DELETE CASCADE,
            competition_id INTEGER NOT NULL REFERENCES competitions(id) ON DELETE CASCADE,
            division_name  TEXT DEFAULT '',
            PRIMARY KEY (copa_id, competition_id)
        );
    """)
    db.commit()
    # Migrations: add columns that may not exist in older databases
    _migrate(db)
    db.commit()
    db.close()


def _migrate(db: sqlite3.Connection):
    """Add new columns to existing databases without losing data."""
    existing = {row[1] for row in db.execute('PRAGMA table_info(competitions)').fetchall()}
    if 'venue' not in existing:
        db.execute("ALTER TABLE competitions ADD COLUMN venue TEXT DEFAULT ''")
    # Strip trailing dashes/spaces from club names left by earlier parser versions
    db.execute("UPDATE results SET club = TRIM(RTRIM(club, '- ')) WHERE club GLOB '*-'")
    db.execute("UPDATE entries  SET club = TRIM(RTRIM(club, '- ')) WHERE club GLOB '*-'")
    # Add relay column to events if missing
    existing_ev = {row[1] for row in db.execute('PRAGMA table_info(events)').fetchall()}
    if 'relay' not in existing_ev:
        db.execute("ALTER TABLE events ADD COLUMN relay INTEGER DEFAULT 0")
    # Add reserve column to entries if missing
    existing_en = {row[1] for row in db.execute('PRAGMA table_info(entries)').fetchall()}
    if 'reserve' not in existing_en:
        db.execute("ALTER TABLE entries ADD COLUMN reserve INTEGER DEFAULT 0")


def allowed_file(filename: str) -> bool:
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


# ---------------------------------------------------------------------------
# Competition helpers
# ---------------------------------------------------------------------------

def get_competition(comp_id: int) -> sqlite3.Row | None:
    return get_db().execute(
        'SELECT * FROM competitions WHERE id = ?', (comp_id,)
    ).fetchone()


def _get_or_create_event(db, competition_id: int, ev: dict) -> int:
    row = db.execute(
        'SELECT id FROM events WHERE competition_id=? AND number=?',
        (competition_id, ev['event_number'])
    ).fetchone()
    if row:
        return row['id']
    cur = db.execute(
        'INSERT INTO events (competition_id, number, gender, distance, stroke, relay) VALUES (?,?,?,?,?,?)',
        (competition_id, ev['event_number'], ev['gender'], ev['distance'], ev['stroke'],
         int(ev.get('relay', False)))
    )
    return cur.lastrowid


def _store_entries(db, competition_id: int, entries: list[dict]):
    for e in entries:
        ev_id = _get_or_create_event(db, competition_id, e)
        db.execute(
            '''INSERT INTO entries
               (event_id, seq, swimmer_name, swimmer_name_normalized, year, club, pool_length, weighted_time, reserve)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (ev_id, e.get('seq'), e['swimmer_name'], e['swimmer_name_normalized'],
             e.get('year', ''), e.get('club', ''), e.get('pool_length', ''), e.get('weighted_time'),
             int(e.get('reserve', False)))
        )


def _store_results(db, competition_id: int, results: list[dict]):
    for r in results:
        ev_id = _get_or_create_event(db, competition_id, r)
        db.execute(
            '''INSERT INTO results
               (event_id, position, swimmer_name, swimmer_name_normalized, year, club, result_time, points, dsq)
               VALUES (?,?,?,?,?,?,?,?,?)''',
            (ev_id, r.get('position'), r['swimmer_name'], r['swimmer_name_normalized'],
             r.get('year', ''), r.get('club', ''), r.get('result_time'), r.get('points'), int(r.get('dsq', False)))
        )


def _load_entries(db, competition_id: int) -> list[dict]:
    rows = db.execute(
        '''SELECT e.*, ev.number as event_number, ev.gender, ev.distance, ev.stroke, ev.relay
           FROM entries e JOIN events ev ON e.event_id = ev.id
           WHERE ev.competition_id = ?''', (competition_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def _load_results(db, competition_id: int) -> list[dict]:
    rows = db.execute(
        '''SELECT r.*, ev.number as event_number, ev.gender, ev.distance, ev.stroke, ev.relay
           FROM results r JOIN events ev ON r.event_id = ev.id
           WHERE ev.competition_id = ?''', (competition_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    db = get_db()
    comps = db.execute(
        'SELECT * FROM competitions ORDER BY created_at DESC'
    ).fetchall()
    copas = db.execute(
        'SELECT * FROM copa_clubs ORDER BY created_at DESC'
    ).fetchall()
    return render_template('index.html', competitions=comps, copas=copas)


@app.route('/competition/new', methods=['GET', 'POST'])
def new_competition():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        date = request.form.get('date', '').strip()
        pool_type = request.form.get('pool_type', '').strip()
        venue = request.form.get('venue', '').strip()
        if not name:
            flash('El nombre de la competición es obligatorio.', 'danger')
            return redirect(url_for('new_competition'))
        db = get_db()
        cur = db.execute(
            'INSERT INTO competitions (name, date, pool_type, venue, created_at) VALUES (?,?,?,?,?)',
            (name, date, pool_type, venue, datetime.utcnow().isoformat())
        )
        db.commit()
        flash('Competición creada.', 'success')
        return redirect(url_for('competition', comp_id=cur.lastrowid))
    return render_template('new_competition.html')


@app.route('/competition/<int:comp_id>')
def competition(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        flash('Competición no encontrada.', 'danger')
        return redirect(url_for('index'))
    db = get_db()
    n_entries = db.execute(
        'SELECT COUNT(*) FROM entries e JOIN events ev ON e.event_id=ev.id WHERE ev.competition_id=?',
        (comp_id,)
    ).fetchone()[0]
    n_results = db.execute(
        'SELECT COUNT(*) FROM results r JOIN events ev ON r.event_id=ev.id WHERE ev.competition_id=?',
        (comp_id,)
    ).fetchone()[0]
    return render_template('competition.html', comp=comp, n_entries=n_entries, n_results=n_results)


@app.route('/competition/<int:comp_id>/edit', methods=['POST'])
def edit_competition(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        return jsonify({'error': 'Not found'}), 404
    pool_type = request.form.get('pool_type', '').strip()
    name = request.form.get('name', comp['name']).strip()
    date = request.form.get('date', comp['date']).strip()
    venue = request.form.get('venue', comp['venue'] if comp['venue'] else '').strip()
    db = get_db()
    db.execute(
        'UPDATE competitions SET name=?, date=?, pool_type=?, venue=? WHERE id=?',
        (name, date, pool_type, venue, comp_id)
    )
    db.commit()
    flash('Competición actualizada.', 'success')
    return redirect(url_for('competition', comp_id=comp_id))


@app.route('/competition/<int:comp_id>/delete', methods=['POST'])
def delete_competition(comp_id: int):
    db = get_db()
    db.execute('DELETE FROM competitions WHERE id=?', (comp_id,))
    db.commit()
    flash('Competición eliminada.', 'success')
    return redirect(url_for('index'))


@app.route('/competition/<int:comp_id>/upload_inscriptions', methods=['POST'])
def upload_inscriptions(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        flash('Competición no encontrada.', 'danger')
        return redirect(url_for('index'))

    f = request.files.get('file')
    if not f or not allowed_file(f.filename):
        flash('Selecciona un archivo PDF válido.', 'danger')
        return redirect(url_for('competition', comp_id=comp_id))

    filename = secure_filename(f.filename)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], f'comp{comp_id}_inscriptions_{filename}')
    f.save(save_path)

    try:
        entries = parse_inscriptions(save_path)
    except Exception as exc:
        flash(f'Error al parsear las inscripciones: {exc}', 'danger')
        return redirect(url_for('competition', comp_id=comp_id))

    db = get_db()
    # Clear existing entries for this competition
    db.execute(
        'DELETE FROM entries WHERE event_id IN (SELECT id FROM events WHERE competition_id=?)',
        (comp_id,)
    )
    _store_entries(db, comp_id, entries)
    db.commit()
    flash(f'{len(entries)} inscripciones cargadas correctamente.', 'success')
    return redirect(url_for('competition', comp_id=comp_id))


@app.route('/competition/<int:comp_id>/upload_results', methods=['POST'])
def upload_results(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        flash('Competición no encontrada.', 'danger')
        return redirect(url_for('index'))

    f = request.files.get('file')
    if not f or not allowed_file(f.filename):
        flash('Selecciona un archivo PDF válido.', 'danger')
        return redirect(url_for('competition', comp_id=comp_id))

    filename = secure_filename(f.filename)
    save_path = os.path.join(app.config['UPLOAD_FOLDER'], f'comp{comp_id}_results_{filename}')
    f.save(save_path)

    try:
        results, detected_pool = parse_results(save_path)
    except Exception as exc:
        flash(f'Error al parsear los resultados: {exc}', 'danger')
        return redirect(url_for('competition', comp_id=comp_id))

    db = get_db()
    # Clear existing results
    db.execute(
        'DELETE FROM results WHERE event_id IN (SELECT id FROM events WHERE competition_id=?)',
        (comp_id,)
    )
    _store_results(db, comp_id, results)

    # Update pool type if detected and not already set manually
    if detected_pool and not comp['pool_type']:
        db.execute('UPDATE competitions SET pool_type=? WHERE id=?', (detected_pool, comp_id))
        flash(f'Tipo de piscina detectado automáticamente: {detected_pool}.', 'info')

    db.commit()
    flash(f'{len(results)} resultados cargados correctamente.', 'success')
    return redirect(url_for('competition', comp_id=comp_id))


@app.route('/competition/<int:comp_id>/entries')
def entries_view(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        flash('Competicion no encontrada.', 'danger')
        return redirect(url_for('index'))
    db = get_db()
    ev_cols = {r[1] for r in db.execute('PRAGMA table_info(events)').fetchall()}
    en_cols = {r[1] for r in db.execute('PRAGMA table_info(entries)').fetchall()}
    relay_col   = 'ev.relay'   if 'relay'   in ev_cols else '0 AS relay'
    reserve_col = 'e.reserve'  if 'reserve' in en_cols else '0 AS reserve'
    rows = db.execute(
        f'''SELECT e.id, e.seq, e.swimmer_name, e.year, e.club,
                   e.pool_length, e.weighted_time, {reserve_col},
                   ev.number AS event_number, ev.gender, ev.distance, ev.stroke, {relay_col}
            FROM entries e
            JOIN events ev ON e.event_id = ev.id
            WHERE ev.competition_id = ?
            ORDER BY ev.number, e.seq''',
        (comp_id,)
    ).fetchall()
    entries = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, bytes):
                d[k] = v.decode('latin-1')
        entries.append(d)

    stats = {
        'total':    len(entries),
        'pool_25':  sum(1 for e in entries if e.get('pool_length') == '25m'),
        'pool_50':  sum(1 for e in entries if e.get('pool_length') == '50m'),
        'no_time':  sum(1 for e in entries if e.get('weighted_time') is None),
        'reserves': sum(1 for e in entries if e.get('reserve')),
    }
    return render_template('entries.html', comp=comp, entries=entries, stats=stats)


@app.route('/competition/<int:comp_id>/entries/<int:entry_id>', methods=['POST'])
def update_entry(comp_id: int, entry_id: int):
    db = get_db()
    swimmer_name = request.form.get('swimmer_name', '').strip()
    year         = request.form.get('year', '').strip()
    club         = request.form.get('club', '').strip().rstrip('- ').strip()
    pool_length  = request.form.get('pool_length', '').strip()
    time_str     = request.form.get('weighted_time', '').strip()

    from pdf_parser import time_to_seconds, normalize_name
    weighted_time = time_to_seconds(time_str) if time_str else None

    db.execute(
        '''UPDATE entries SET swimmer_name=?, swimmer_name_normalized=?,
           year=?, club=?, pool_length=?, weighted_time=? WHERE id=?''',
        (swimmer_name, normalize_name(swimmer_name),
         year, club, pool_length, weighted_time, entry_id)
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/competition/<int:comp_id>/entries/<int:entry_id>/delete', methods=['POST'])
def delete_entry(comp_id: int, entry_id: int):
    db = get_db()
    db.execute('DELETE FROM entries WHERE id=?', (entry_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/competition/<int:comp_id>/entries/bulk-delete', methods=['POST'])
def bulk_delete_entries(comp_id: int):
    """Delete a list of entry IDs (JSON body: {ids: [...]}) or a named preset."""
    db = get_db()
    preset = request.form.get('preset', '')
    if preset in ('no_time', 'reserves', 'no_time_and_reserves'):
        conditions = []
        if preset in ('no_time', 'no_time_and_reserves'):
            conditions.append('e.weighted_time IS NULL')
        if preset in ('reserves', 'no_time_and_reserves'):
            conditions.append('e.reserve = 1')
        where = ' OR '.join(conditions)
        db.execute(
            f'''DELETE FROM entries WHERE id IN (
                SELECT e.id FROM entries e
                JOIN events ev ON e.event_id = ev.id
                WHERE ev.competition_id = ? AND ({where}))''',
            (comp_id,)
        )
        db.commit()
        return jsonify({'ok': True})
    # Individual IDs from JSON
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in data.get('ids', []) if str(i).isdigit()]
    if not ids:
        return jsonify({'ok': False, 'error': 'No IDs provided'}), 400
    db.execute(f'DELETE FROM entries WHERE id IN ({",".join("?" * len(ids))})', ids)
    db.commit()
    return jsonify({'ok': True, 'deleted': len(ids)})


@app.route('/competition/<int:comp_id>/entries/add', methods=['POST'])
def add_entry(comp_id: int):
    """Manually add a single inscription entry."""
    db = get_db()
    event_number = request.form.get('event_number', type=int)
    if not event_number:
        return jsonify({'error': 'Número de prueba requerido'}), 400

    ev_row = db.execute(
        'SELECT * FROM events WHERE competition_id=? AND number=?',
        (comp_id, event_number)
    ).fetchone()
    if ev_row is None:
        return jsonify({'error': f'Prueba {event_number} no encontrada en esta competición'}), 404

    from pdf_parser import time_to_seconds, normalize_name
    swimmer_name = request.form.get('swimmer_name', '').strip()
    year         = request.form.get('year', '').strip()
    club         = request.form.get('club', '').strip().rstrip('- ').strip()
    pool_length  = request.form.get('pool_length', '').strip()
    time_str     = request.form.get('weighted_time', '').strip()
    weighted_time = time_to_seconds(time_str) if time_str else None

    if not swimmer_name:
        return jsonify({'error': 'Nombre requerido'}), 400

    cur = db.execute(
        '''INSERT INTO entries
           (event_id, seq, swimmer_name, swimmer_name_normalized, year, club, pool_length, weighted_time)
           VALUES (?,?,?,?,?,?,?,?)''',
        (ev_row['id'], 0, swimmer_name, normalize_name(swimmer_name),
         year, club, pool_length, weighted_time)
    )
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/competition/<int:comp_id>/results')
def results_view(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        flash('Competicion no encontrada.', 'danger')
        return redirect(url_for('index'))
    db = get_db()
    ev_cols = {r[1] for r in db.execute('PRAGMA table_info(events)').fetchall()}
    relay_col = 'ev.relay' if 'relay' in ev_cols else '0 AS relay'
    rows = db.execute(
        f'''SELECT r.id, r.position, r.swimmer_name, r.year, r.club,
                   r.result_time, r.points, r.dsq,
                   ev.number AS event_number, ev.gender, ev.distance, ev.stroke, {relay_col}
            FROM results r
            JOIN events ev ON r.event_id = ev.id
            WHERE ev.competition_id = ?
            ORDER BY ev.number, r.position''',
        (comp_id,)
    ).fetchall()
    results = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, bytes):
                d[k] = v.decode('latin-1')
        results.append(d)

    # Detect pool mismatch: entry marks from a different pool type than the competition
    pool_type = comp['pool_type'] or ''
    mismatch_keys: set[tuple] = set()
    valid_entry_keys: set[tuple] = set()
    if pool_type:
        entry_rows = db.execute(
            '''SELECT e.pool_length, e.swimmer_name_normalized,
                      ev.number AS event_number, e.weighted_time
               FROM entries e
               JOIN events ev ON e.event_id = ev.id
               WHERE ev.competition_id = ?''',
            (comp_id,)
        ).fetchall()
        for er in entry_rows:
            key = (er['event_number'], er['swimmer_name_normalized'])
            if er['pool_length'] and er['pool_length'] != pool_type:
                mismatch_keys.add(key)
            elif er['pool_length'] == pool_type and er['weighted_time'] is not None:
                valid_entry_keys.add(key)

    for r in results:
        key = (r['event_number'], r.get('swimmer_name_normalized', ''))
        r['pool_mismatch'] = key in mismatch_keys
        r['no_entry'] = (
            bool(pool_type)
            and not r['pool_mismatch']
            and not r.get('dsq')
            and r.get('result_time') is not None
            and key not in valid_entry_keys
        )

    stats = {
        'total':        len(results),
        'dsq':          sum(1 for r in results if r.get('dsq')),
        'no_time':      sum(1 for r in results if r.get('result_time') is None and not r.get('dsq')),
        'pool_mismatch': sum(1 for r in results if r.get('pool_mismatch')),
        'no_entry':     sum(1 for r in results if r.get('no_entry')),
    }
    return render_template('results.html', comp=comp, results=results, stats=stats)


@app.route('/competition/<int:comp_id>/results/<int:result_id>', methods=['POST'])
def update_result(comp_id: int, result_id: int):
    db = get_db()
    swimmer_name = request.form.get('swimmer_name', '').strip()
    year         = request.form.get('year', '').strip()
    club         = request.form.get('club', '').strip().rstrip('- ').strip()
    time_str     = request.form.get('result_time', '').strip()
    pos_str      = request.form.get('position', '').strip()

    from pdf_parser import time_to_seconds, normalize_name
    result_time = time_to_seconds(time_str) if time_str else None
    position    = int(pos_str) if pos_str.isdigit() else None

    db.execute(
        '''UPDATE results SET swimmer_name=?, swimmer_name_normalized=?,
           year=?, club=?, result_time=?, position=? WHERE id=?''',
        (swimmer_name, normalize_name(swimmer_name),
         year, club, result_time, position, result_id)
    )
    db.commit()
    return jsonify({'ok': True})


@app.route('/competition/<int:comp_id>/results/<int:result_id>/delete', methods=['POST'])
def delete_result(comp_id: int, result_id: int):
    db = get_db()
    db.execute('DELETE FROM results WHERE id=?', (result_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/competition/<int:comp_id>/results/bulk-delete', methods=['POST'])
def bulk_delete_results(comp_id: int):
    db = get_db()
    preset = request.form.get('preset', '')
    if preset == 'dsq':
        db.execute(
            '''DELETE FROM results WHERE id IN (
               SELECT r.id FROM results r
               JOIN events ev ON r.event_id = ev.id
               WHERE ev.competition_id = ? AND r.dsq = 1)''',
            (comp_id,)
        )
        db.commit()
        return jsonify({'ok': True})
    data = request.get_json(silent=True) or {}
    ids = [int(i) for i in data.get('ids', []) if str(i).isdigit()]
    if not ids:
        return jsonify({'ok': False, 'error': 'No IDs provided'}), 400
    db.execute(f'DELETE FROM results WHERE id IN ({",".join("?" * len(ids))})', ids)
    db.commit()
    return jsonify({'ok': True, 'deleted': len(ids)})


@app.route('/competition/<int:comp_id>/analysis')
def analysis(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        flash('Competición no encontrada.', 'danger')
        return redirect(url_for('index'))

    db = get_db()
    entries = _load_entries(db, comp_id)
    results = _load_results(db, comp_id)

    if not entries or not results:
        flash('Sube las inscripciones y los resultados antes de analizar.', 'warning')
        return redirect(url_for('competition', comp_id=comp_id))

    pool_type = comp['pool_type'] or ''
    comp_year = int(comp['date'][:4]) if comp['date'] and len(comp['date']) >= 4 else datetime.utcnow().year
    data = analyze(entries, results, pool_type, competition_year=comp_year)
    data['seconds_to_time'] = seconds_to_time

    return render_template('analysis.html', comp=comp, data=data, seconds_to_time=seconds_to_time)


@app.route('/competition/<int:comp_id>/analysis/json')
def analysis_json(comp_id: int):
    comp = get_competition(comp_id)
    if comp is None:
        return jsonify({'error': 'Not found'}), 404

    db = get_db()
    entries = _load_entries(db, comp_id)
    results = _load_results(db, comp_id)

    pool_type = comp['pool_type'] or ''
    data = analyze(entries, results, pool_type)
    # Remove non-serializable helper
    data.pop('seconds_to_time', None)
    return jsonify(data)


# ---------------------------------------------------------------------------
# Global pool / venue ranking
# ---------------------------------------------------------------------------

@app.route('/pool-ranking')
def pool_ranking():
    db = get_db()
    comps = db.execute('SELECT * FROM competitions ORDER BY name').fetchall()

    all_records: list[dict] = []
    comps_with_data = []
    for comp in comps:
        entries = _load_entries(db, comp['id'])
        results = _load_results(db, comp['id'])
        if not entries or not results:
            continue
        comp_data = analyze(entries, results, comp['pool_type'] or '')
        venue = (comp['venue'] or '').strip() or comp['name']
        for rec in comp_data['records']:
            rec['venue'] = venue
            rec['competition_name'] = comp['name']
            rec['competition_id'] = comp['id']
        all_records.extend(comp_data['records'])
        comps_with_data.append({'id': comp['id'], 'name': comp['name'],
                                 'venue': venue, 'n': len(comp_data['records'])})

    ranking = venue_ranking_analysis(all_records)
    return render_template('pool_ranking.html',
                           ranking=ranking,
                           all_records=all_records,
                           comps_with_data=comps_with_data,
                           seconds_to_time=seconds_to_time)


@app.route('/debug-pdf', methods=['GET', 'POST'])
def debug_pdf():
    """Diagnostic page: upload any PDF and inspect what pdfplumber extracts."""
    if request.method == 'GET':
        return render_template('debug_pdf.html')

    f = request.files.get('file')
    if not f:
        return jsonify({'error': 'No file'}), 400

    import tempfile, pdfplumber, re as _re
    with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
        f.save(tmp.name)
        tmp_path = tmp.name

    pages_out = []
    with pdfplumber.open(tmp_path) as pdf:
        for page_num, page in enumerate(pdf.pages[:5]):  # first 5 pages
            # Full text (simple)
            full_text = page.extract_text() or ''

            # Character-level rows
            from pdf_parser import _group_chars_by_row, _chars_to_text, EVENT_HEADER_RE
            rows = _group_chars_by_row(page)
            char_rows = []
            for y, chars in sorted(rows.items()):
                line = _chars_to_text(chars).strip()
                if not line:
                    continue
                fonts = list({c.get('fontname', '') for c in chars})
                bold_text = _chars_to_text([c for c in chars if 'Bold' in c.get('fontname', '')])
                is_event = bool(EVENT_HEADER_RE.search(line))
                char_rows.append({
                    'y': round(y, 1),
                    'text': line[:120],
                    'fonts': fonts[:4],
                    'bold': bold_text[:40],
                    'is_event_header': is_event,
                })

            pages_out.append({
                'page': page_num + 1,
                'full_text_sample': full_text[:800],
                'char_rows': char_rows[:60],
            })

    os.unlink(tmp_path)
    return jsonify(pages_out)


# ---------------------------------------------------------------------------
# Copa de Clubs helpers
# ---------------------------------------------------------------------------

def get_copa(copa_id: int):
    return get_db().execute('SELECT * FROM copa_clubs WHERE id=?', (copa_id,)).fetchone()


def _copa_competitions(db, copa_id: int) -> list[sqlite3.Row]:
    return db.execute(
        '''SELECT c.*, cc.division_name
           FROM copa_clubs_competitions cc
           JOIN competitions c ON c.id = cc.competition_id
           WHERE cc.copa_id = ?
           ORDER BY cc.division_name''',
        (copa_id,)
    ).fetchall()


# ---------------------------------------------------------------------------
# Copa de Clubs routes
# ---------------------------------------------------------------------------

@app.route('/copa/new', methods=['GET', 'POST'])
def new_copa():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        if not name:
            flash('El nombre de la Copa es obligatorio.', 'danger')
            return redirect(url_for('new_copa'))
        db = get_db()
        cur = db.execute(
            'INSERT INTO copa_clubs (name, created_at) VALUES (?,?)',
            (name, datetime.utcnow().isoformat())
        )
        db.commit()
        flash('Copa de Clubs creada.', 'success')
        return redirect(url_for('copa', copa_id=cur.lastrowid))
    return render_template('copa_new.html')


@app.route('/copa/<int:copa_id>')
def copa(copa_id: int):
    c = get_copa(copa_id)
    if c is None:
        flash('Copa no encontrada.', 'danger')
        return redirect(url_for('index'))
    db = get_db()
    linked = _copa_competitions(db, copa_id)
    linked_ids = {row['id'] for row in linked}
    all_comps = db.execute('SELECT * FROM competitions ORDER BY name').fetchall()
    available = [comp for comp in all_comps if comp['id'] not in linked_ids]
    return render_template('copa.html', copa=c, linked=linked, available=available)


@app.route('/copa/<int:copa_id>/add', methods=['POST'])
def copa_add_competition(copa_id: int):
    c = get_copa(copa_id)
    if c is None:
        flash('Copa no encontrada.', 'danger')
        return redirect(url_for('index'))
    comp_id = request.form.get('competition_id', type=int)
    division_name = request.form.get('division_name', '').strip()
    if not comp_id:
        flash('Selecciona una competición.', 'danger')
        return redirect(url_for('copa', copa_id=copa_id))
    db = get_db()
    db.execute(
        'INSERT OR IGNORE INTO copa_clubs_competitions (copa_id, competition_id, division_name) VALUES (?,?,?)',
        (copa_id, comp_id, division_name)
    )
    db.commit()
    flash('División añadida.', 'success')
    return redirect(url_for('copa', copa_id=copa_id))


@app.route('/copa/<int:copa_id>/remove/<int:comp_id>', methods=['POST'])
def copa_remove_competition(copa_id: int, comp_id: int):
    db = get_db()
    db.execute(
        'DELETE FROM copa_clubs_competitions WHERE copa_id=? AND competition_id=?',
        (copa_id, comp_id)
    )
    db.commit()
    flash('División eliminada de la Copa.', 'success')
    return redirect(url_for('copa', copa_id=copa_id))


@app.route('/copa/<int:copa_id>/delete', methods=['POST'])
def delete_copa(copa_id: int):
    db = get_db()
    db.execute('DELETE FROM copa_clubs WHERE id=?', (copa_id,))
    db.commit()
    flash('Copa eliminada.', 'success')
    return redirect(url_for('index'))


@app.route('/copa/<int:copa_id>/clasificacion')
def copa_clasificacion(copa_id: int):
    c = get_copa(copa_id)
    if c is None:
        flash('Copa no encontrada.', 'danger')
        return redirect(url_for('index'))
    db = get_db()
    linked = _copa_competitions(db, copa_id)
    if not linked:
        flash('Añade al menos una división antes de ver la clasificación.', 'warning')
        return redirect(url_for('copa', copa_id=copa_id))

    all_results: list[dict] = []
    for row in linked:
        comp_results = _load_results(db, row['id'])
        division = row['division_name'] or row['name']
        for r in comp_results:
            r['division'] = division
            r['competition_name'] = row['name']
        all_results.extend(comp_results)

    ref_year = datetime.utcnow().year
    data = compute_copa_classification(all_results, reference_year=ref_year)
    return render_template('copa_clasificacion.html', copa=c, linked=linked, data=data,
                           seconds_to_time=seconds_to_time)


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------

@app.template_filter('fmt_event_key')
def fmt_event_key_filter(key: str) -> str:
    """'M_800m_Libre_relay' → 'M 4x200m Libre (relevo)'"""
    import re as _re
    m = _re.match(r'^([MFX])_(\d+)m_(\w+?)(_relay)?$', key)
    if not m:
        return key.replace('_', ' ')
    gender, dist, stroke, relay = m.groups()
    dist = int(dist)
    if relay:
        return f'{gender} 4x{dist // 4}m {stroke} (relevo)'
    return f'{gender} {dist}m {stroke}'


@app.template_filter('fmt_time')
def fmt_time_filter(secs):
    if secs is None:
        return '-'
    return seconds_to_time(float(secs))


@app.template_filter('fmt_sign')
def fmt_sign_filter(val):
    if val is None:
        return '-'
    val = float(val)
    sign = '+' if val >= 0 else ''
    return f'{sign}{val:.3f}'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5000)
