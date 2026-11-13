"""Chronological exploration from published, revision-checked map matches."""
import json

from garmin_library.errors import AppError
from garmin_library.geometry import point_time
from garmin_library.network_geometry import merge_ranges, projected_line


def new_ranges(covered, previous):
    """Subtract the earlier union, retaining only nontrivial interval pieces."""
    previous = merge_ranges(previous)
    result = []
    for low, high in merge_ranges(covered):
        cursor = low
        for start, end in previous:
            if end <= cursor:
                continue
            if start >= high:
                break
            if start > cursor:
                result.append([cursor, min(start, high)])
            cursor = max(cursor, end)
            if cursor >= high:
                break
        if cursor < high:
            result.append([cursor, high])
    return merge_ranges(result)


def _current(row):
    return (row['match_status'] == 'completed' and row['source_revision'] is not None
            and row['match_revision'] == row['source_revision'])


def track_exploration(db, aid, geometry=False):
    """Caller owns a read snapshot; no mutations, network requests, or caches."""
    activity = db.execute('SELECT id,start_time,sport FROM activities WHERE id=?', (aid,)).fetchone()
    if activity is None:
        raise AppError(404, 'Activity not found.')
    state = db.execute('SELECT active_generation,data_revision FROM network_state WHERE id=1').fetchone()
    generation = state['active_generation']
    result = {'activity_id': aid, 'generation': generation, 'data_revision': state['data_revision'],
              'status': 'unavailable', 'message': None, 'new_m': None, 'matched_unique_m': None,
              'new_percent': None, 'history_complete': False, 'coverage_complete': False,
              'earlier_activities': 0, 'missing_history_matches': 0, 'undated_history_activities': 0,
              'partial_history_activities': 0,
              'outside_coverage': False, 'unmatched_points': None}
    if geometry:
        result['geometry'] = {'type': 'FeatureCollection', 'features': []}
    if activity['sport'] not in ('cycling', 'running'):
        result['message'] = 'Street/path exploration is available for cycling and running activities.'
        return result
    start = point_time(activity['start_time'])
    if start is None:
        result['message'] = 'This activity needs a valid start date to compare its history.'
        return result
    if not generation or not db.execute(
            "SELECT 1 FROM network_generations WHERE id=? AND status='published'", (generation,)).fetchone():
        result['message'] = 'No published street/path coverage is available yet.'
        return result
    rows = db.execute('''SELECT a.id,a.start_time,j.status AS match_status,j.revision AS match_revision,
        v.revision AS source_revision,j.outside_coverage,j.unmatched_points
        FROM activities a LEFT JOIN network_jobs j ON j.activity_id=a.id AND j.generation=?
        LEFT JOIN network_source_versions v ON v.activity_id=a.id
        WHERE a.sport IN ('cycling','running')''', (generation,)).fetchall()
    selected = next(row for row in rows if row['id'] == aid)
    if not _current(selected):
        result['status'] = 'failed' if selected['match_status'] == 'failed' else 'pending'
        result['message'] = ('Street/path matching failed for this activity.' if result['status'] == 'failed'
                             else 'Waiting for current street/path matches for this activity.')
        return result
    earlier = set()
    for row in rows:
        if row['id'] == aid:
            continue
        timestamp = point_time(row['start_time'])
        if timestamp is None:
            result['undated_history_activities'] += 1
        elif timestamp < start:
            result['earlier_activities'] += 1
            if _current(row):
                earlier.add(row['id'])
                if row['outside_coverage'] or row['unmatched_points']:
                    result['partial_history_activities'] += 1
            else:
                result['missing_history_matches'] += 1
    result['history_complete'] = not (result['missing_history_matches'] or result['undated_history_activities']
                                      or result['partial_history_activities'])
    result['outside_coverage'] = bool(selected['outside_coverage'])
    result['unmatched_points'] = selected['unmatched_points']
    result['coverage_complete'] = not result['outside_coverage'] and not result['unmatched_points']
    sections = db.execute('''SELECT s.id,s.length_m,s.geometry,c.ranges FROM network_contributions c
        JOIN network_sections s ON s.id=c.section_id AND s.generation=c.generation
        WHERE c.generation=? AND c.activity_id=?''', (generation, aid)).fetchall()
    matched_m = new_m = 0.0
    # Bound SQL parameter count and memory to the selected track's sections.
    for offset in range(0, len(sections), 400):
        batch = sections[offset:offset+400]
        previous = {row['id']: [] for row in batch}
        if earlier:
            placeholders = ','.join('?' for _ in batch)
            history = db.execute('''SELECT section_id,activity_id,ranges FROM network_contributions
                INDEXED BY network_contribution_section_idx WHERE generation=? AND section_id IN ('''
                + placeholders + ')', (generation, *(row['id'] for row in batch)))
            for row in history:
                if row['activity_id'] in earlier:
                    previous[row['section_id']].extend(json.loads(row['ranges']))
        for row in batch:
            covered = merge_ranges(json.loads(row['ranges']))
            unseen = new_ranges(covered, previous[row['id']])
            matched_m += row['length_m'] * sum(high-low for low, high in covered)
            new_m += row['length_m'] * sum(high-low for low, high in unseen)
            if geometry and unseen:
                from shapely.ops import substring
                line, scale = projected_line(json.loads(row['geometry']))
                for low, high in unseen:
                    piece = substring(line, low, high, normalized=True)
                    if piece.geom_type != 'LineString' or piece.is_empty:
                        continue
                    coordinates = [[x/scale, y/111195] for x, y in piece.coords]
                    result['geometry']['features'].append({'type': 'Feature',
                        'properties': {'section_id': row['id'], 'new_m': row['length_m']*(high-low)},
                        'geometry': {'type': 'LineString', 'coordinates': coordinates}})
    result.update(new_m=new_m, matched_unique_m=matched_m)
    if matched_m <= 0:
        result.update(status='no_matched_distance', message='No usable matched path distance is available for this activity.')
        return result
    result['new_percent'] = min(100.0, max(0.0, new_m/matched_m*100))
    result['status'] = 'available' if result['history_complete'] and result['coverage_complete'] else 'provisional'
    result['message'] = 'Estimated paths first recorded in this library, compared with strictly earlier cycling and running activities.'
    return result
