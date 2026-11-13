"""Stored Garmin workout instructions and their step equipment, independent of GPS."""
from contextlib import closing
import json
import logging
import urllib.error

from garmin_library.errors import AppError

LOG = logging.getLogger(__name__)


def garmin_activity_id(value):
    return isinstance(value, str) and value.isascii() and value.isdecimal() and 0 < int(value) < 2**63


def validate_workouts(payload):
    if not isinstance(payload, list):
        raise ValueError('Expected a list of workouts.')
    for workout in payload:
        if not isinstance(workout, dict):
            raise ValueError('Invalid workout.')
        if workout.get('workoutName') is not None and not isinstance(workout['workoutName'], str):
            raise ValueError('Invalid workout name.')
        steps = workout.get('steps')
        if steps is None:
            continue
        if not isinstance(steps, list):
            raise ValueError('Invalid workout steps.')
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError('Invalid workout step.')
            for key in ('equipmentType', 'name'):
                if step.get(key) is not None and not isinstance(step[key], str):
                    raise ValueError('Invalid step equipment or name.')
    return payload


def fetch_activity_equipment(context, client, aid):
    """Persist one attempt; failed refreshes retain the last successful payload."""
    if not garmin_activity_id(aid):
        return False
    error = None
    unauthorized = False
    payload = None
    try:
        payload = validate_workouts(json.loads(client.get(
            f'/activity-service/activity/{aid}/workouts', accept='application/json')))
        encoded = json.dumps(payload, allow_nan=False)
    except urllib.error.HTTPError as exc:
        error = f'Garmin returned HTTP {exc.code} while fetching workout equipment.'
        unauthorized = exc.code == 401
    except (urllib.error.URLError, OSError, TimeoutError):
        error = 'Could not reach Garmin while fetching workout equipment.'
    except (ValueError, UnicodeError):
        error = 'Garmin returned invalid workout data.'
    # Do not keep a SQLite connection/transaction open during network requests.
    with closing(context.connect()) as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute('SELECT 1 FROM activities WHERE id=?', (aid,)).fetchone():
            db.rollback()
            return False
        if error is None:
            db.execute('''INSERT INTO activity_workouts(activity_id,payload_json,fetched_at,last_attempt_at,last_error)
                VALUES(?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL)
                ON CONFLICT(activity_id) DO UPDATE SET payload_json=excluded.payload_json,
                fetched_at=excluded.fetched_at,last_attempt_at=excluded.last_attempt_at,last_error=NULL''', (aid, encoded))
        else:
            db.execute('''INSERT INTO activity_workouts(activity_id,last_attempt_at,last_error)
                VALUES(?,CURRENT_TIMESTAMP,?) ON CONFLICT(activity_id) DO UPDATE SET
                last_attempt_at=excluded.last_attempt_at,last_error=excluded.last_error''', (aid, error))
        db.commit()
    if error:
        LOG.warning('Activity %s: %s', aid, error)
    if unauthorized:
        raise SystemExit('Garmin authentication failed. Workout backfill progress is saved; retry with valid credentials.')
    return error is None


def activity_equipment(db, aid):
    if not db.execute('SELECT 1 FROM activities WHERE id=?', (aid,)).fetchone():
        raise AppError(404, 'Activity not found.')
    row = db.execute('SELECT * FROM activity_workouts WHERE activity_id=?', (aid,)).fetchone()
    result = {'activity_id': aid, 'status': 'not_fetched', 'fetched_at': None,
              'last_attempt_at': None, 'error': None, 'equipment_types': [], 'workouts': []}
    if row is None:
        return result
    result.update(fetched_at=row['fetched_at'], last_attempt_at=row['last_attempt_at'], error=row['last_error'])
    if row['payload_json'] is not None:
        workouts = json.loads(row['payload_json'])
        equipment = sorted({step['equipmentType'] for workout in workouts
                            for step in (workout.get('steps') or [])
                            if step.get('equipmentType') and step['equipmentType'] != 'NONE'})
        result.update(workouts=workouts, equipment_types=equipment,
                      status='loaded' if equipment else 'no_equipment' if workouts else 'no_workouts')
    if row['last_error']:
        result['status'] = 'refresh_failed' if row['payload_json'] is not None else 'failed'
    return result
