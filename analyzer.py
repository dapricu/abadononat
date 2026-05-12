"""
Statistical analysis of swimming competition results.

Matches inscriptions (entry marks) to results and computes improvement stats.
Only entries whose pool_length matches the competition pool_type are included.
"""
import statistics
from dataclasses import dataclass, field


@dataclass
class ImprovementRecord:
    event_number: int
    gender: str
    distance: int
    stroke: str
    swimmer_name: str
    club: str
    year: str
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
    records: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'count': self.count,
            'improved_count': self.improved_count,
            'mean_improvement_sec': round(self.mean_improvement_sec, 3),
            'std_improvement_sec': round(self.std_improvement_sec, 3),
            'median_improvement_sec': round(self.median_improvement_sec, 3),
            'pct_improved': round(self.pct_improved, 1),
        }


def _compute_stats(records: list[ImprovementRecord]) -> Stats:
    if not records:
        return Stats()
    improvements = [r.improvement_sec for r in records]
    improved = [r for r in records if r.improvement_sec > 0]
    return Stats(
        count=len(records),
        improved_count=len(improved),
        mean_improvement_sec=statistics.mean(improvements),
        std_improvement_sec=statistics.stdev(improvements) if len(improvements) > 1 else 0.0,
        median_improvement_sec=statistics.median(improvements),
        pct_improved=100 * len(improved) / len(records),
        records=records,
    )


def analyze(
    entries: list[dict],
    results: list[dict],
    competition_pool_type: str,
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
        key = (e['event_number'], e['swimmer_name_normalized'])
        # Keep the entry with the best (lowest) time if duplicates
        if key not in entry_index or e['weighted_time'] < entry_index[key]['weighted_time']:
            entry_index[key] = e

    # Match results
    improvement_records: list[ImprovementRecord] = []
    unmatched_results: list[dict] = []

    for r in results:
        if r.get('dsq') or r.get('result_time') is None:
            continue
        key = (r['event_number'], r['swimmer_name_normalized'])
        entry = entry_index.get(key)
        if entry is None:
            unmatched_results.append(r)
            continue

        imp_sec = entry['weighted_time'] - r['result_time']  # positive = faster
        imp_pct = 100 * imp_sec / entry['weighted_time'] if entry['weighted_time'] else 0.0

        improvement_records.append(ImprovementRecord(
            event_number=r['event_number'],
            gender=r['gender'],
            distance=r['distance'],
            stroke=r['stroke'],
            swimmer_name=r['swimmer_name'],
            club=r['club'],
            year=r['year'],
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
        'event_number': r.event_number,
        'gender': r.gender,
        'distance': r.distance,
        'stroke': r.stroke,
        'swimmer_name': r.swimmer_name,
        'club': r.club,
        'year': r.year,
        'entry_time': round(r.entry_time, 2),
        'result_time': round(r.result_time, 2),
        'improvement_sec': round(r.improvement_sec, 3),
        'improvement_pct': round(r.improvement_pct, 2),
        'pool_length': r.pool_length,
    }


# Points awarded to positions 1–16 in Copa de Clubs (FNCV scoring)
COPA_POINTS = [19, 16, 14, 13, 12, 11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1]


def compute_copa_classification(all_results: list[dict]) -> dict:
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
    # Group finishers by (gender, distance, stroke)
    event_groups: dict[tuple, list[dict]] = {}
    for r in all_results:
        if r.get('dsq') or r.get('result_time') is None:
            continue
        key = (r['gender'], r['distance'], r['stroke'])
        event_groups.setdefault(key, []).append(r)

    club_points: dict[str, dict] = {}  # club → {total, events_scored, breakdown}
    events_out: dict[str, dict] = {}

    for (gender, distance, stroke), finishers in sorted(event_groups.items()):
        # Sort by result_time ascending (fastest first); ties share the same position
        finishers_sorted = sorted(finishers, key=lambda r: r['result_time'])

        event_key = f'{gender}_{distance}m_{stroke}'
        annotated = []
        pos = 0
        prev_time = None
        prev_copa_pts = 0
        for i, r in enumerate(finishers_sorted):
            if r['result_time'] != prev_time:
                pos = i + 1
                prev_copa_pts = COPA_POINTS[pos - 1] if pos <= len(COPA_POINTS) else 0
                prev_time = r['result_time']
            copa_pts = prev_copa_pts

            annotated.append({
                'pos': pos,
                'swimmer_name': r['swimmer_name'],
                'club': r['club'],
                'year': r.get('year', ''),
                'result_time': r['result_time'],
                'division': r.get('division', ''),
                'copa_points': copa_pts,
            })

            if copa_pts > 0 and r['club']:
                entry = club_points.setdefault(r['club'], {'total': 0, 'events_scored': 0, 'breakdown': {}})
                entry['total'] += copa_pts
                entry['events_scored'] += 1
                entry['breakdown'][event_key] = entry['breakdown'].get(event_key, 0) + copa_pts

        events_out[event_key] = {
            'gender': gender,
            'distance': distance,
            'stroke': stroke,
            'finishers': annotated,
        }

    club_ranking = sorted(
        [{'club': club, **data} for club, data in club_points.items()],
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
