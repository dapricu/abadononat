"""
Statistical analysis of swimming competition results.

Matches inscriptions (entry marks) to results and computes improvement stats.
Only entries whose pool_length matches the competition pool_type are included.
"""
import statistics
from dataclasses import dataclass, field

from pdf_parser import name_match_key


def _full_birth_year(year_str: str, reference_year: int) -> int | None:
    """Convert a 2-digit birth year string to a 4-digit year."""
    if not year_str:
        return None
    try:
        yy = int(str(year_str).strip()[-2:])
    except (ValueError, TypeError):
        return None
    ref_yy = reference_year % 100
    return (2000 + yy) if yy <= ref_yy else (1900 + yy)


@dataclass
class ImprovementRecord:
    event_number: int
    gender: str
    distance: int
    stroke: str
    relay: bool
    swimmer_name: str
    club: str
    year: str
    age: int | None
    entry_time: float
    result_time: float
    improvement_sec: float      # positive = faster
    improvement_pct: float      # positive = faster
    pool_length: str            # pool of the entry mark


@dataclass
class Stats:
    count: int = 0
    improved_count: int = 0
    mean_improvement_sec: float = 0.0
    std_improvement_sec: float = 0.0
    median_improvement_sec: float = 0.0
    pct_improved: float = 0.0
    mean_age: float | None = None
    records: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'count': self.count,
            'improved_count': self.improved_count,
            'mean_improvement_sec': round(self.mean_improvement_sec, 3),
            'std_improvement_sec': round(self.std_improvement_sec, 3),
            'median_improvement_sec': round(self.median_improvement_sec, 3),
            'pct_improved': round(self.pct_improved, 1),
            'mean_age': round(self.mean_age, 1) if self.mean_age is not None else None,
        }


def _compute_stats(records: list[ImprovementRecord]) -> Stats:
    if not records:
        return Stats()
    improvements = [r.improvement_sec for r in records]
    improved = [r for r in records if r.improvement_sec > 0]
    ages = [r.age for r in records if r.age is not None]
    return Stats(
        count=len(records),
        improved_count=len(improved),
        mean_improvement_sec=statistics.mean(improvements),
        std_improvement_sec=statistics.stdev(improvements) if len(improvements) > 1 else 0.0,
        median_improvement_sec=statistics.median(improvements),
        pct_improved=100 * len(improved) / len(records),
        mean_age=statistics.mean(ages) if ages else None,
        records=records,
    )


def analyze(
    entries: list[dict],
    results: list[dict],
    competition_pool_type: str,
    competition_year: int = 0,
) -> dict:
    """
    Match entries to results and compute improvement statistics.

    Args:
        entries: from parser.parse_inscriptions()
        results: from parser.parse_results()
        competition_pool_type: '25m' or '50m'

    Returns a dict with:
      - 'records': list of ImprovementRecord dicts
      - 'excluded_pool': list of entry dicts excluded due to pool mismatch
      - 'unmatched_results': list of result dicts with no inscription
      - 'overall': Stats dict
      - 'by_stroke': {stroke: Stats dict}
      - 'by_distance': {distance: Stats dict}
      - 'by_club': {club: Stats dict}
      - 'by_gender': {gender: Stats dict}
      - 'by_event': {event_number: Stats dict}
    """
    # Index entries by (event_number, normalized_name)
    # Filter to same pool type
    entry_index: dict[tuple, dict] = {}
    excluded_pool: list[dict] = []

    for e in entries:
        if e.get('weighted_time') is None:
            continue
        if competition_pool_type and e.get('pool_length') and e['pool_length'] != competition_pool_type:
            excluded_pool.append(e)
            continue
        key = (e['event_number'], name_match_key(e['swimmer_name_normalized']))
        # Keep the entry with the best (lowest) time if duplicates
        if key not in entry_index or e['weighted_time'] < entry_index[key]['weighted_time']:
            entry_index[key] = e

    # Match results
    improvement_records: list[ImprovementRecord] = []
    unmatched_results: list[dict] = []

    for r in results:
        if r.get('dsq') or r.get('result_time') is None:
            continue
        key = (r['event_number'], name_match_key(r.get('swimmer_name_normalized', '')))
        entry = entry_index.get(key)
        if entry is None:
            unmatched_results.append(r)
            continue

        imp_sec = entry['weighted_time'] - r['result_time']  # positive = faster
        imp_pct = 100 * imp_sec / entry['weighted_time'] if entry['weighted_time'] else 0.0
        birth_yr = _full_birth_year(r.get('year', ''), competition_year) if competition_year else None
        age = (competition_year - birth_yr) if (birth_yr and competition_year) else None

        improvement_records.append(ImprovementRecord(
            event_number=r['event_number'],
            gender=r['gender'],
            distance=r['distance'],
            stroke=r['stroke'],
            relay=bool(r.get('relay', False)),
            swimmer_name=r['swimmer_name'],
            club=r['club'],
            year=r['year'],
            age=age,
            entry_time=entry['weighted_time'],
            result_time=r['result_time'],
            improvement_sec=imp_sec,
            improvement_pct=imp_pct,
            pool_length=entry.get('pool_length', ''),
        ))

    # Compute aggregate stats
    overall = _compute_stats(improvement_records)

    by_stroke: dict[str, Stats] = {}
    for stroke in {r.stroke for r in improvement_records}:
        by_stroke[stroke] = _compute_stats([r for r in improvement_records if r.stroke == stroke])

    by_distance: dict[int, Stats] = {}
    for dist in sorted({r.distance for r in improvement_records}):
        by_distance[dist] = _compute_stats([r for r in improvement_records if r.distance == dist])

    by_club: dict[str, Stats] = {}
    for club in sorted({r.club for r in improvement_records}):
        by_club[club] = _compute_stats([r for r in improvement_records if r.club == club])

    by_gender: dict[str, Stats] = {}
    for gender in sorted({r.gender for r in improvement_records}):
        by_gender[gender] = _compute_stats([r for r in improvement_records if r.gender == gender])

    by_event: dict[int, Stats] = {}
    for ev in sorted({r.event_number for r in improvement_records}):
        by_event[ev] = _compute_stats([r for r in improvement_records if r.event_number == ev])

    return {
        'records': [_rec_to_dict(r) for r in improvement_records],
        'excluded_pool': excluded_pool,
        'unmatched_results': unmatched_results,
        'overall': overall.to_dict(),
        'by_stroke': {k: v.to_dict() for k, v in by_stroke.items()},
        'by_distance': {str(k): v.to_dict() for k, v in by_distance.items()},
        'by_club': {k: v.to_dict() for k, v in by_club.items()},
        'by_gender': {k: v.to_dict() for k, v in by_gender.items()},
        'by_event': {str(k): v.to_dict() for k, v in by_event.items()},
    }


def _rec_to_dict(r: ImprovementRecord) -> dict:
    return {
        'event_number':   r.event_number,
        'gender':         r.gender,
        'distance':       r.distance,
        'stroke':         r.stroke,
        'relay':          r.relay,
        'swimmer_name':   r.swimmer_name,
        'club':           r.club,
        'year':           r.year,
        'age':            r.age,
        'entry_time':     round(r.entry_time, 2),
        'result_time':    round(r.result_time, 2),
        'improvement_sec': round(r.improvement_sec, 3),
        'improvement_pct': round(r.improvement_pct, 2),
        'pool_length':    r.pool_length,
    }


# ---------------------------------------------------------------------------
# Venue / pool anomaly detection
# ---------------------------------------------------------------------------

def venue_ranking_analysis(all_records: list[dict]) -> list[dict]:
    """
    Compare competition venues by their event-adjusted improvement residuals
    to detect installations whose pools may be shorter than declared.

    Logic:
      1. For each event (gender, distance, stroke) compute the mean and std
         of improvements ACROSS ALL venues combined.
      2. Each swimmer's residual = (improvement - event_mean) / event_std.
         This makes swimmers in the 100m Libre comparable to those in the
         400m Estilos regardless of how hard each event typically is.
      3. Aggregate residuals per venue and compute a t-statistic testing
         whether the venue mean is significantly different from 0.

    Interpretation — competition venue:
      • HIGH positive residual / t-stat → swimmers who competed at this
        venue improved significantly MORE than peers in the same events at
        other venues. A consistently short pool would inflate everyone's
        result times, producing suspiciously large improvements against
        entry marks from properly measured pools.
      • Negative residual → below-average improvement at this venue
        (tougher conditions, slow pool, etc.).

    Records must include a 'venue' key (the installation/facility name)
    and the standard improvement fields from analyzer.analyze().
    """
    if len(all_records) < 2:
        return []

    # Only work with records that have a venue assigned
    records = [r for r in all_records if r.get('venue')]
    if len(records) < 2:
        return []

    # --- Event-level stats across all venues combined ---------------------
    event_groups: dict[tuple, list[float]] = {}
    for r in records:
        key = (r['gender'], r['distance'], r['stroke'])
        event_groups.setdefault(key, []).append(r['improvement_sec'])

    event_stats: dict[tuple, tuple[float, float]] = {}
    for key, vals in event_groups.items():
        ev_mean = statistics.mean(vals)
        ev_std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        event_stats[key] = (ev_mean, ev_std)

    # --- Per-swimmer event-adjusted residual and relative index -----------
    venue_items: dict[str, list[dict]] = {}
    for r in records:
        key = (r['gender'], r['distance'], r['stroke'])
        ev_mean, ev_std = event_stats[key]
        residual = (r['improvement_sec'] - ev_mean) / ev_std if ev_std > 0 else 0.0
        rel_idx = r['result_time'] / r['entry_time'] if r.get('entry_time') else 1.0
        venue_items.setdefault(r['venue'], []).append({
            'residual': residual,
            'improvement_sec': r['improvement_sec'],
            'improvement_pct': r['improvement_pct'],
            'rel_idx': rel_idx,
            'swimmer_name': r['swimmer_name'],
            'club': r['club'],
            'event': f"{r['gender']} {r['distance']}m {r['stroke']}",
            'entry_time': r['entry_time'],
            'result_time': r['result_time'],
            'competition_name': r.get('competition_name', ''),
        })

    overall_vals = [r['improvement_sec'] for r in records]
    overall_mean = statistics.mean(overall_vals)
    overall_std = statistics.stdev(overall_vals) if len(overall_vals) > 1 else 1.0

    # --- Aggregate per venue ---------------------------------------------
    rows = []
    for venue, items in venue_items.items():
        n = len(items)
        residuals = [x['residual'] for x in items]
        improvements = [x['improvement_sec'] for x in items]
        pcts = [x['improvement_pct'] for x in items]
        rel_idxs = [x['rel_idx'] for x in items]

        mean_res = statistics.mean(residuals)
        mean_imp = statistics.mean(improvements)
        std_res = statistics.stdev(residuals) if n > 1 else 0.0
        std_imp = statistics.stdev(improvements) if n > 1 else 0.0

        # t-statistic: mean_residual / SE  (SE = std_res / sqrt(n))
        t_stat = mean_res / (std_res / (n ** 0.5)) if std_res > 0 and n > 1 else 0.0

        cohen_d = (mean_imp - overall_mean) / overall_std if overall_std > 0 else 0.0

        n_improved = sum(1 for v in improvements if v > 0)

        # Competitions included at this venue
        comps = sorted({x['competition_name'] for x in items if x['competition_name']})

        # Top improvers (highest residual = most suspiciously fast vs peers)
        top_3 = sorted(items, key=lambda x: -x['residual'])[:3]

        rows.append({
            'venue': venue,
            'competitions': comps,
            'n': n,
            'mean_improvement_sec': round(mean_imp, 3),
            'mean_improvement_pct': round(statistics.mean(pcts), 2),
            'std_improvement_sec': round(std_imp, 3),
            'median_improvement_sec': round(statistics.median(improvements), 3),
            'mean_rel_idx': round(statistics.mean(rel_idxs), 4),
            'pct_improved': round(100 * n_improved / n, 1),
            'n_improved': n_improved,
            'mean_residual': round(mean_res, 3),
            'std_residual': round(std_res, 3),
            't_stat': round(t_stat, 2),
            'cohen_d': round(cohen_d, 3),
            'gap_vs_mean_sec': round(mean_imp - overall_mean, 3),
            # High positive t is suspicious (everyone improves unusually much = possible short pool)
            'suspicion': _venue_suspicion(t_stat, n),
            'top_improvers': [
                {
                    'swimmer_name': x['swimmer_name'],
                    'club': x['club'],
                    'event': x['event'],
                    'improvement_sec': round(x['improvement_sec'], 3),
                    'improvement_pct': round(x['improvement_pct'], 2),
                    'residual': round(x['residual'], 2),
                    'entry_time': x['entry_time'],
                    'result_time': x['result_time'],
                }
                for x in top_3
            ],
        })

    # Sort: highest mean_residual first (most suspicious = everyone improves a lot)
    rows.sort(key=lambda x: (-x['mean_residual'], x['venue']))
    for i, row in enumerate(rows):
        row['rank'] = i + 1

    return rows


def _venue_suspicion(t_stat: float, n: int) -> str:
    """
    Flag venues whose swimmers improve significantly MORE than at other venues.

    A high positive t-stat means all swimmers at this venue improved
    more than peers in the same events at other venues — consistent
    with a competition pool that is shorter than declared.
    Low negative t-stat means below-average improvement (slow/long pool).
    """
    if n < 5:
        return 'insuf'
    if t_stat >= 2.5:
        return 'alta'      # highly suspicious: anomalously high improvement
    if t_stat >= 1.5:
        return 'media'     # moderate suspicion
    if t_stat <= -2.5:
        return 'baja'      # below-average improvement (slow pool)
    if t_stat <= -1.5:
        return 'baja_mod'
    return 'normal'


# Points awarded to positions 1–16 in Copa de Clubs (FNCV scoring)
COPA_POINTS = [19, 16, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]
# Relay events score double
COPA_POINTS_RELAY = [p * 2 for p in COPA_POINTS]


def compute_copa_classification(all_results: list[dict], reference_year: int = 0) -> dict:
    """
    Simulate a combined Copa de Clubs classification.

    Takes results from multiple competitions (divisions) already tagged with
    a 'division' key. For each event (gender + distance + stroke), merges all
    valid finishers, sorts by result_time, and assigns Copa points:
      1st → 19, 2nd → 16, 3rd → 14, 4th–16th → 13 down to 1, rest → 0.

    Events are matched by (gender, distance, stroke) so different event
    numbers across PDFs from different divisions are treated as the same event.

    Returns:
      {
        'club_ranking': [{'club', 'total_points', 'events_scored', 'breakdown': {event_key: pts}}, ...],
        'events': {
          event_key: {
            'gender', 'distance', 'stroke',
            'finishers': [{'pos', 'swimmer_name', 'club', 'result_time', 'division', 'copa_points'}, ...]
          }
        }
      }
    """
    # Group finishers by (gender, distance, stroke, relay)
    event_groups: dict[tuple, list[dict]] = {}
    for r in all_results:
        if r.get('dsq') or r.get('result_time') is None:
            continue
        key = (r['gender'], r['distance'], r['stroke'], bool(r.get('relay', False)))
        event_groups.setdefault(key, []).append(r)

    club_points: dict[str, dict] = {}  # club → {total, events_scored, breakdown}
    events_out: dict[str, dict] = {}

    for (gender, distance, stroke, _relay_flag), finishers in sorted(event_groups.items()):
        # Sort by result_time ascending (fastest first); ties share the same position
        finishers_sorted = sorted(finishers, key=lambda r: r['result_time'])

        is_relay = bool(finishers_sorted[0].get('relay', False))
        pts_table = COPA_POINTS_RELAY if is_relay else COPA_POINTS
        event_key = f'{gender}_{distance}m_{stroke}{"_relay" if is_relay else ""}'
        annotated = []
        pos = 0
        prev_time = None
        prev_copa_pts = 0
        for i, r in enumerate(finishers_sorted):
            if r['result_time'] != prev_time:
                pos = i + 1
                prev_copa_pts = pts_table[pos - 1] if pos <= len(pts_table) else 0
                prev_time = r['result_time']
            copa_pts = prev_copa_pts
            club = (r['club'] or '').rstrip('- ').strip()

            birth_yr = _full_birth_year(r.get('year', ''), reference_year) if reference_year else None
            age = (reference_year - birth_yr) if (birth_yr and reference_year) else None
            annotated.append({
                'pos': pos,
                'swimmer_name': r['swimmer_name'],
                'club': club,
                'year': r.get('year', ''),
                'age': age,
                'result_time': r['result_time'],
                'division': r.get('division', ''),
                'copa_points': copa_pts,
            })

            if copa_pts > 0 and club:
                entry = club_points.setdefault(club, {'total': 0, 'events_scored': 0, 'breakdown': {}})
                entry['total'] += copa_pts
                entry['events_scored'] += 1
                entry['breakdown'][event_key] = entry['breakdown'].get(event_key, 0) + copa_pts

        scorer_ages = [f['age'] for f in annotated if f['copa_points'] > 0 and f['age'] is not None]
        events_out[event_key] = {
            'gender':   gender,
            'distance': distance,
            'stroke':   stroke,
            'relay':    is_relay,
            'mean_age': round(statistics.mean(scorer_ages), 1) if scorer_ages else None,
            'finishers': annotated,
        }

    # Mean age of point-scoring swimmers per club
    club_ages: dict[str, list[int]] = {}
    for ev in events_out.values():
        for f in ev['finishers']:
            if f['copa_points'] > 0 and f['age'] is not None and f['club']:
                club_ages.setdefault(f['club'], []).append(f['age'])

    club_ranking = sorted(
        [{'club': club, **data,
          'mean_age': round(statistics.mean(club_ages[club]), 1) if club in club_ages else None}
         for club, data in club_points.items()],
        key=lambda x: (-x['total'], x['club'])
    )
    for i, row in enumerate(club_ranking):
        row['rank'] = i + 1

    return {
        'club_ranking': club_ranking,
        'events': events_out,
    }


def seconds_to_time(secs: float) -> str:
    """Format float seconds as M:SS.cc string."""
    if secs is None:
        return '-'
    mins = int(secs) // 60
    remainder = secs - mins * 60
    if mins > 0:
        return f'{mins}:{remainder:05.2f}'
    return f'{remainder:.2f}'
