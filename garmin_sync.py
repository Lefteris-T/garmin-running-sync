"""Garmin dry-run planning; test-copy updates are a separate offline command."""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import warnings
import zipfile
import re
import hashlib
from collections import Counter
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

BASE = 'https://personal.connectapi.garmin.com'
TOKEN_PATH = '/personalapitoken-service/personal-api/personal-api-key/token'
LIST_PATH = '/activitylist-service/activities/search/activities'
MAX_BYTES = 64 * 1024 * 1024
ROOT = Path(__file__).resolve().parent
TEST_HISTORY = ROOT / 'private' / 'running_history_master.test.json'
PROCESSING_VERSION = 'history-compatible-v1'


class SyncError(Exception):
    """Messages are fixed strings safe for display; never include upstream content."""


def activity_id(value):
    if isinstance(value, bool) or not str(value).isdigit() or int(value) <= 0:
        raise SyncError('Invalid or missing activity ID.')
    return str(int(value))


def activity_kind(meta):
    kind = meta.get('activityType')
    key = kind.get('typeKey') if isinstance(kind, dict) else None
    return key if key in {'running', 'treadmill_running', 'track_running',
                          'trail_running', 'indoor_track_running', 'virtual_run'} else None


# Deliberately explicit: generic fitness, multisport, and new keys stay unknown.
NON_RUNNING_TYPES = frozenset({'walking', 'hiking', 'cycling', 'indoor_cycling', 'road_biking',
    'mountain_biking', 'gravel_cycling', 'strength_training', 'yoga', 'pilates', 'elliptical',
    'stair_climbing', 'lap_swimming', 'open_water_swimming', 'swimming', 'rowing',
    'indoor_rowing', 'breathwork', 'meditation', 'stretching', 'jump_rope'})


def type_key(meta):
    value = meta.get('activityType')
    key = value.get('typeKey') if isinstance(value, dict) else None
    # Accept machine keys only, never arbitrary API strings or activity titles.
    return key if isinstance(key, str) and re.fullmatch(r'[a-z0-9_]{1,80}', key) else '<missing-or-invalid>'


def type_category(meta):
    if activity_kind(meta): return 'running'
    return 'non_running' if type_key(meta) in NON_RUNNING_TYPES else 'unknown'


def type_counts(activities):
    counts = Counter((type_category(m), type_key(m)) for m in activities)
    return [{'category': c, 'type': k, 'count': n, 'running_name_hint': 'run' in k}
            for (c,k),n in sorted(counts.items())]


def resolve_unknown_type(meta, sessions):
    """Use FIT sport evidence, never key substrings or activity titles."""
    if len(sessions) != 1: return None
    sport = sessions[0].get('sport')
    if sport == 'running':
        subtype = sessions[0].get('sub_sport')
        return {'treadmill': 'treadmill_running', 'track': 'track_running',
                'indoor_track': 'indoor_track_running', 'trail': 'trail_running'}.get(subtype, 'running')
    return None


def read_history(path):
    try:
        history = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if history.get('schema_version') != '3.0': raise ValueError()
        records = history['running_activities']
        if not isinstance(records, list) or history['activity_count'] != len(records): raise ValueError()
        ids = [activity_id(r['garmin_activity_id']) for r in records]
        if len(ids) != len(set(ids)): raise ValueError()
        return history
    except Exception:
        raise SyncError('History validation failed; nothing was changed.') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class GarminClient:
    def __init__(self, key, opener=None):
        self._opener = opener or build_opener(NoRedirect())
        self._bearer = None
        token = self._request('POST', TOKEN_PATH, authorization='ApiKey ' + key).decode('utf-8').strip()
        if not token or any(c.isspace() for c in token):
            raise SyncError('Garmin returned an invalid token; response suppressed.')
        self._bearer = token

    def _request(self, method, path, authorization=None):
        if not path.startswith('/') or path.startswith('//'):
            raise SyncError('Invalid Garmin request path.')
        request = Request(BASE + path, data=b'' if method == 'POST' else None,
                          headers={'Authorization': authorization or 'Bearer ' + self._bearer}, method=method)
        try:
            with self._opener.open(request, timeout=60) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES: raise SyncError('Garmin response exceeded the size limit.')
                return body
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise SyncError('Garmin HTTP request failed (status %d); response suppressed.' % code) from None
        except (URLError, OSError, ValueError):
            raise SyncError('Garmin connection failed; details suppressed.') from None

    def activities(self, limit=100):
        start = 0
        seen = set()
        for _ in range(10000):
            try:
                page = json.loads(self._request('GET', f'{LIST_PATH}?start={start}&limit={limit}'))
                if not isinstance(page, list): raise ValueError()
                ids = [activity_id(m.get('activityId')) for m in page]
            except SyncError: raise
            except Exception: raise SyncError('Garmin activity-list format is unexpected.') from None
            if not page: return
            if not (set(ids) - seen):
                raise SyncError('Garmin pagination repeated a page; plan is incomplete.')
            for meta, identifier in zip(page, ids):
                if identifier not in seen:
                    seen.add(identifier)
                    yield meta
            start += len(page)
        raise SyncError('Garmin pagination safety limit reached; plan is incomplete.')

    def fit(self, identifier):
        return unpack_fit(self._request('GET', '/download-service/files/activity/' + activity_id(identifier)))


def unpack_fit(data):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            fits = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith('.fit')]
            if len(fits) != 1 or fits[0].file_size > MAX_BYTES: raise ValueError()
            # Never extract archive paths to disk.
            with archive.open(fits[0]) as stream:
                fit = stream.read(MAX_BYTES + 1)
                if len(fit) > MAX_BYTES: raise ValueError()
                return fit
    except Exception:
        raise SyncError('Activity ZIP must contain exactly one readable FIT within the size limit.') from None


# Only these native FIT fields can cross the decoder boundary; no GPS fields.
RECORD_UNITS = {'heart_rate': 'bpm', 'cadence': 'rpm', 'fractional_cadence': 'rpm',
                'speed': 'm/s', 'enhanced_speed': 'm/s', 'distance': 'm', 'temperature': 'C'}
SESSION_UNITS = {'total_timer_time': 's', 'total_elapsed_time': 's', 'total_distance': 'm',
                 'avg_speed': 'm/s', 'enhanced_avg_speed': 'm/s', 'avg_heart_rate': 'bpm',
                 'max_heart_rate': 'bpm', 'avg_cadence': 'rpm', 'avg_fractional_cadence': 'rpm',
                 'total_ascent': 'm', 'total_descent': 'm'}


def decode_fit(data):
    try:
        import fitdecode
    except ImportError:
        raise SyncError('Install requirements.txt in the Windows virtual environment before decoding FIT.') from None
    sessions, records = [], []
    try:
        # Strict parsing and CRC checking: corrupt files must not produce proposals.
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE,
                                     error_handling=fitdecode.ErrorHandling.RAISE) as reader:
                for frame in reader:
                    if frame.frame_type != fitdecode.FIT_FRAME_DATA or frame.name not in {'session', 'record'}:
                        continue
                    units = SESSION_UNITS if frame.name == 'session' else RECORD_UNITS
                    values = {}
                    for name, expected in units.items():
                        # fitdecode 0.11 supports fallback on get_value(), not
                        # get_field(). Optional fields must be checked first.
                        field = frame.get_field(name) if frame.has_field(name) else None
                        if field is not None and field.value is not None:
                            # FIT resolves session cadence to a sport-specific
                            # subfield for running; it is explicitly strides/min.
                            if frame.name == 'session' and name == 'avg_cadence' and field.name == 'avg_running_cadence':
                                expected = 'strides/min'
                            if field.units != expected: raise SyncError('Unexpected FIT units; activity requires review.')
                            values[name] = field.value
                    for name in ('start_time', 'sport', 'sub_sport') if frame.name == 'session' else ('timestamp',):
                        values[name] = frame.get_value(name, fallback=None)
                    (sessions if frame.name == 'session' else records).append(values)
        return sessions, records
    except SyncError: raise
    except Exception:
        raise SyncError('FIT decoding or integrity check failed; details suppressed.') from None


def number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def nonnegative(value):
    n = number(value)
    return n if n is not None and n >= 0 else None


def preferred(values, *keys):
    for key in keys:
        n = nonnegative(values.get(key))
        if n is not None: return n
    return None


def cadence(values, average=False):
    key = 'avg_cadence' if average else 'cadence'
    base = nonnegative(values.get(key))
    fraction = nonnegative(values.get('avg_fractional_cadence' if average else 'fractional_cadence'))
    # Native FIT running cadence is strides/min (rpm); output is steps/min.
    return None if base is None else (base + (fraction or 0)) * 2


def integer_cadence(values, average=False):
    base = nonnegative(values.get('avg_cadence' if average else 'cadence'))
    return None if base is None else base * 2


def pace(speed):
    return 1000 / speed if speed is not None and speed > 0 else None


def clock(seconds, duration=False):
    if seconds is None: return None
    seconds = round(seconds)
    return (f'{seconds//3600}:{seconds//60%60:02}:{seconds%60:02}' if duration
            else f'{seconds//60}:{seconds%60:02}')


def build_record(meta, sessions, records):
    kind = activity_kind(meta)
    if not kind or len(sessions) != 1 or sessions[0].get('sport') != 'running':
        raise SyncError('FIT sport is ambiguous, conflicting, or multisession; manual review required.')
    s = sessions[0]
    subtype = s.get('sub_sport')
    if (kind == 'treadmill_running' and subtype in {'track', 'indoor_track'}) or (
            kind in {'track_running', 'indoor_track_running'} and subtype == 'treadmill'):
        raise SyncError('Garmin and FIT running subtypes conflict; manual review required.')
    start = s.get('start_time')
    if not isinstance(start, datetime): raise SyncError('FIT has no valid session start time.')
    distance = nonnegative(s.get('total_distance'))
    duration = nonnegative(s.get('total_timer_time'))
    speed = preferred(s, 'enhanced_avg_speed', 'avg_speed')
    # Derive only from measured timer time and distance when average speed is absent.
    avg_pace = pace(speed)
    if avg_pace is None and distance and duration is not None: avg_pace = duration * 1000 / distance
    local = meta.get('startTimeLocal')
    if local is not None:
        try: datetime.fromisoformat(local)
        except (ValueError, TypeError): raise SyncError('Invalid Garmin local start time.') from None
    buckets = {}
    timed = [r for r in records if isinstance(r.get('timestamp'), datetime)]
    try: timed.sort(key=lambda r: r['timestamp'])
    except TypeError: raise SyncError('FIT timestamps have inconsistent timezone information.') from None
    for r in timed:
        timestamp = r.get('timestamp')
        if not isinstance(timestamp, datetime): continue
        try: elapsed = (timestamp - start).total_seconds()
        except TypeError: raise SyncError('FIT timestamps have inconsistent timezone information.') from None
        if elapsed < 0: continue
        bucket = int(elapsed // 15) * 15
        buckets.setdefault(bucket, []).append(r)
    def mean(group, getter, positive=False):
        values = [v for r in group if (v := getter(r)) is not None]
        if positive: values = [v for v in values if v > 0]
        return sum(values)/len(values) if values else None
    def rounded(value): return None if value is None else round(value, 1)
    series, precision_series = [], []
    for elapsed, group in sorted(buckets.items()):
        speed_getter = lambda r: preferred(r, 'enhanced_speed', 'speed')
        speed = mean(group, speed_getter, positive=True)
        distances = [v for r in group if (v := nonnegative(r.get('distance'))) is not None]
        precise = {'elapsed_sec': elapsed,
                   'hr_bpm': mean(group, lambda r: nonnegative(r.get('heart_rate'))),
                   'cadence_spm': mean(group, cadence, positive=True), 'speed_mps': speed,
                   'pace_sec_per_km': pace(speed), 'distance_m': distances[-1] if distances else None,
                   'temperature_c': mean(group, lambda r: number(r.get('temperature')))}
        legacy = {k: v if k == 'elapsed_sec' else rounded(v) for k,v in precise.items()}
        legacy['cadence_spm'] = rounded(mean(group, integer_cadence, positive=True))
        series.append(legacy)
        precise['cadence_spm_including_zero'] = mean(group, cadence)
        precise['speed_mps_including_zero'] = mean(group, speed_getter)
        precision_series.append(precise)
    return {'garmin_activity_id': int(activity_id(meta.get('activityId'))),
            'date': local[:10] if local else None, 'local_start': local, 'activity_type': kind,
            'title': meta.get('activityName'), 'distance_km': distance/1000 if distance is not None else None,
            'duration_seconds': duration, 'duration': clock(duration, duration=True),
            'average_pace_seconds_per_km': avg_pace, 'average_pace': clock(avg_pace),
            'average_hr_bpm': nonnegative(s.get('avg_heart_rate')), 'max_hr_bpm': nonnegative(s.get('max_heart_rate')),
            'elevation_gain_m': nonnegative(s.get('total_ascent')), 'elevation_loss_m': nonnegative(s.get('total_descent')),
            'average_cadence_spm': integer_cadence(s, average=True), 'vo2max_estimate': None,
            'source': 'Garmin Connect Personal API; FIT', 'fit_record_count': len(records),
            'fit_match_minutes': None,
            'fit_details': {'record_count': len(records), 'sampling_interval_seconds': 15,
                            'timeseries_15s': series, 'km_splits_estimated': [],
                            'timestamp_timezone': 'seconds since activity start; no geolocation retained',
                            'processing': {'version': PROCESSING_VERSION,
                                           'distance': 'last valid chronological observation per 15-second bucket',
                                           'cadence': 'integer FIT cadence x2; positive-only bucket mean',
                                           'speed': 'positive-only bucket mean', 'series_rounding_decimals': 1,
                                           'limitations': ['historical cadence outliers may remain; no low-positive-value filtering']},
                            'precision': {'average_cadence_spm': cadence(s, average=True),
                                          'timeseries_15s': precision_series}}}


def plan(history, activities, parse, inspect_unknown=None):
    activities = list(activities)
    known = {activity_id(r['garmin_activity_id']) for r in history['running_activities']}
    additions, review, excluded, resolutions = [], [], [], []
    duplicate_count = 0
    for meta in activities:
        identifier = activity_id(meta.get('activityId'))
        if identifier in known:
            duplicate_count += 1
            continue
        known.add(identifier)
        if activity_kind(meta) is None:
            key = type_key(meta)
            if type_category(meta) == 'non_running':
                excluded.append({'activity_id': identifier, 'type': key})
                continue
            if inspect_unknown is None:
                review.append({'activity_id': identifier, 'type': key, 'reason': 'unknown type; FIT inspection needed'})
                continue
            try:
                sessions, records = inspect_unknown(meta)
                resolved = resolve_unknown_type(meta, sessions)
                # An explicit FIT sport can also confirm a non-running exclusion.
                sport = sessions[0].get('sport') if len(sessions) == 1 else None
                known_nonrunning_sports = {'cycling', 'walking', 'swimming', 'rowing', 'hiking',
                                         'basketball', 'soccer', 'tennis', 'golf'}
                if resolved:
                    evidence_meta = dict(meta, activityType={'typeKey': resolved})
                    additions.append(build_record(evidence_meta, sessions, records))
                    resolutions.append({'activity_id': identifier, 'type': key, 'fit_sport': 'running', 'resolved': resolved})
                elif sport in known_nonrunning_sports:
                    excluded.append({'activity_id': identifier, 'type': key, 'fit_sport': sport})
                else:
                    review.append({'activity_id': identifier, 'type': key, 'reason': 'FIT sport ambiguous or multisession'})
            except SyncError as exc:
                review.append({'activity_id': identifier, 'type': key, 'reason': str(exc)})
            continue
        try:
            additions.append(parse(meta))
        except SyncError as exc:
            review.append({'activity_id': identifier, 'reason': str(exc)})
    return {'additions': additions, 'review': review, 'excluded_non_running': excluded,
            'type_counts': type_counts(activities), 'unknown_resolutions': resolutions,
            'duplicates_skipped': duplicate_count}


def compare_existing(history, activities, parse, inspect_fit=None):
    """Re-decode one available historical example per type to check conversions.

    Differences are evidence for review, never permission to replace a record.
    """
    existing = {activity_id(a['garmin_activity_id']): a for a in history['running_activities']}
    checked = set()
    comparisons = []
    for meta in activities:
        kind = activity_kind(meta)
        identifier = activity_id(meta.get('activityId'))
        if not kind or kind in checked or identifier not in existing: continue
        checked.add(kind)
        old = existing[identifier]
        try:
            fresh = parse(meta)
            fields = {}
            for key in ('distance_km', 'duration_seconds', 'average_hr_bpm', 'average_cadence_spm',
                        'average_pace_seconds_per_km'):
                prior, current = number(old.get(key)), number(fresh.get(key))
                fields[key] = {'history': prior, 'decoded': current,
                               'difference': current-prior if prior is not None and current is not None else None}
            old_series = {p['elapsed_sec']: p for p in old['fit_details']['timeseries_15s']}
            differences = {}
            for key in ('hr_bpm', 'cadence_spm', 'speed_mps', 'distance_m'):
                ds = []
                for p in fresh['fit_details']['timeseries_15s']:
                    a, b = number(old_series.get(p['elapsed_sec'], {}).get(key)), number(p.get(key))
                    if a is not None and b is not None: ds.append(abs(a-b))
                differences[key] = {'comparable_buckets': len(ds), 'mean_absolute_difference': sum(ds)/len(ds) if ds else None}
            comparisons.append({'activity_id': identifier, 'type': kind, 'summary': fields,
                                'series_comparison': differences})
            if inspect_fit:
                from processing_audit import historical_processing_audit
                sessions, records = inspect_fit(meta)
                comparisons[-1]['processing_audit'] = historical_processing_audit(old, sessions, records)
        except SyncError as exc:
            comparisons.append({'activity_id': identifier, 'type': kind, 'reason': str(exc)})
    return comparisons


def read_windows_key():
    if sys.platform != 'win32': raise SyncError('Windows is required for credential access.')
    path = Path(os.environ['LOCALAPPDATA']) / 'GarminRunningSync' / 'api_key.dpapi'
    # Reuse the audited ACL validator; never capture a secret through PowerShell output.
    support = str(ROOT / 'scripts' / 'CredentialSupport.ps1').replace("'", "''")
    command = ". '" + support + "'; Assert-CredentialAcl (Join-Path $env:LOCALAPPDATA 'GarminRunningSync') -Directory; Assert-CredentialAcl (Join-Path $env:LOCALAPPDATA 'GarminRunningSync\\api_key.dpapi')"
    try:
        check = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                '-Command', command], capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if check.returncode: raise ValueError()
        encrypted = path.read_bytes()
        class Blob(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
        buf = (ctypes.c_ubyte * len(encrypted)).from_buffer_copy(encrypted)
        source = Blob(len(encrypted), buf)
        target = Blob()
        crypt = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        crypt.CryptUnprotectData.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
            raise ValueError()
        try:
            key = ctypes.string_at(target.data, target.size).decode('utf-8')
            if not key or any(c.isspace() for c in key): raise ValueError()
            return key
        finally:
            ctypes.memset(target.data, 0, target.size)
            kernel.LocalFree(target.data)
    except Exception:
        raise SyncError('Windows credential access failed; verify permissions in your own PowerShell. Details suppressed.') from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', required=True, action='store_true')
    parser.add_argument('--save-plan', action='store_true', help='Save a GPS-free private proposal for review; never update history')
    args = parser.parse_args()
    try:
        # Fail before credential or network access if the parser is unavailable.
        try: import fitdecode  # noqa: F401
        except ImportError: raise SyncError('Install requirements.txt in the Windows virtual environment first.') from None
        history = read_history(TEST_HISTORY)
        before = TEST_HISTORY.read_bytes()
        key = read_windows_key()
        try: client = GarminClient(key)
        finally: key = None
        decoded_cache = {}
        def inspect_fit(meta):
            identifier = activity_id(meta['activityId'])
            if identifier not in decoded_cache:
                if len(decoded_cache) >= 4: decoded_cache.pop(next(iter(decoded_cache)))
                decoded_cache[identifier] = decode_fit(client.fit(identifier))
            return decoded_cache[identifier]
        def parse(meta):
            sessions, records = inspect_fit(meta)
            return build_record(meta, sessions, records)
        activities = list(client.activities())
        comparisons = compare_existing(history, activities, parse, inspect_fit)
        result = plan(history, activities, parse, inspect_fit)
        result.update({'plan_version': 1, 'processing_version': PROCESSING_VERSION,
                       'base_sha256': hashlib.sha256(before).hexdigest()})
        result['existing_activity_checks'] = comparisons
        result['series_method'] = PROCESSING_VERSION
        if TEST_HISTORY.read_bytes() != before: raise SyncError('Test history changed during planning; discard this plan.')
        if args.save_plan:
            # Only explicit processed fields, no raw FIT, tokens, or full API metadata.
            proposal = ROOT / 'private' / ('dry_run_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.json')
            with proposal.open('x', encoding='utf-8') as out: json.dump(result, out, ensure_ascii=False, indent=2, allow_nan=False)
        print('Dry run complete. No history was changed.')
        print('Existing IDs skipped:', result['duplicates_skipped'])
        print('Proposed additions:', len(result['additions']))
        print('Known non-running activities excluded:', len(result['excluded_non_running']))
        print('Activities requiring review:', len(result['review']))
        print('Unknown types confirmed running by FIT:', len(result['unknown_resolutions']))
        print('Counts by Garmin type (all retrieved activities):')
        for row in result['type_counts']:
            print(' TYPE', row['type'], 'category=', row['category'], 'count=', row['count'],
                  'running_name_hint=', row['running_name_hint'])
        print('Excluded counts by type and evidence:')
        for (key, evidence), count in sorted(Counter((r['type'], r.get('fit_sport', 'explicit Garmin non-running type'))
                                                     for r in result['excluded_non_running']).items()):
            print(' NONRUN', key, 'count=', count, 'evidence=', evidence)
        for key, count in sorted(Counter(r['type'] for r in result['unknown_resolutions']).items()):
            print(' FIT-CONFIRMED-RUNNING', key, 'count=', count)
        print('Historical conversion checks:', len(comparisons))
        for check in comparisons:
            print('CHECK', check['activity_id'], 'type=', check['type'])
            if 'reason' in check:
                print('  Requires review:', check['reason'])
            else:
                for key, values in check['summary'].items():
                    print(' ', key, 'history=', values['history'], 'decoded=', values['decoded'], 'difference=', values['difference'])
                for key, values in check['series_comparison'].items():
                    print('  series', key, 'matched_buckets=', values['comparable_buckets'], 'mean_absolute_difference=', values['mean_absolute_difference'])
                audit = check.get('processing_audit', {})
                if 'summary_cadence' in audit:
                    print('  cadence hypothesis:', json.dumps(audit['summary_cadence']))
                if 'cadence_outlier_count' in audit:
                    print('  cadence buckets outside 0.05 spm rounding tolerance:',audit['cadence_outlier_count'])
                    for outlier in audit['cadence_outlier_buckets'][:5]:
                        print('  cadence outlier diagnostics:',json.dumps(outlier))
                for field, candidates in audit.get('candidate_errors', {}).items():
                    ranked = sorted(((name, scores) for name, scores in candidates.items() if scores['comparable_buckets']),
                                    key=lambda pair: pair[1]['mean_absolute_difference'])
                    for name, scores in ranked[:4]:
                        print('  hypothesis', field, name, json.dumps(scores))
        for a in result['additions']:
            print('ADD', a['garmin_activity_id'], 'type=', a['activity_type'], 'km=', a['distance_km'],
                  'seconds=', a['duration_seconds'], 'samples=', len(a['fit_details']['timeseries_15s']))
        print('Unresolved review counts by type and reason:')
        for (key, reason), count in sorted(Counter((r.get('type', '<known-running>'), r['reason']) for r in result['review']).items()):
            print(' REVIEW', key, 'count=', count, reason)
        if args.save_plan: print('GPS-free proposal saved in private/. Review required before any future update.')
        return 0
    except SyncError as exc:
        print('Stopped:', str(exc), file=sys.stderr)
        return 1
    except Exception:
        print('Stopped: unexpected error; details suppressed to protect private data. No history write was attempted.', file=sys.stderr)
        return 1


if __name__ == '__main__': sys.exit(main())
