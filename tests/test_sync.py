import copy
import io
import json
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from garmin_sync import SyncError, GarminClient, TEST_HISTORY, activity_kind, build_record, compare_existing, plan, read_history, unpack_fit


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.meta = {'activityId': 123, 'activityType': {'typeKey': 'running'},
                     'startTimeLocal': '2026-01-01 12:00:00'}
        self.session = {'sport': 'running', 'start_time': self.start,
                        'total_timer_time': 30., 'total_distance': 100.,
                        'avg_speed': 10/3, 'avg_cadence': 85.}

    def test_conversion_missing_fields_and_no_coordinates(self):
        points = [{'timestamp': self.start + timedelta(seconds=s), 'speed': 4.,
                   'distance': s*4., 'cadence': 80., 'position_lat': 123} for s in (0, 14, 15, 29)]
        result = build_record(self.meta, [self.session], points)
        self.assertEqual(result['distance_km'], .1)
        self.assertEqual(result['average_cadence_spm'], 170.)
        self.assertEqual(result['average_pace_seconds_per_km'], 300.)
        self.assertIsNone(result['average_hr_bpm'])
        series = result['fit_details']['timeseries_15s']
        self.assertEqual([p['elapsed_sec'] for p in series], [0, 15])
        self.assertEqual(series[0]['cadence_spm'], 160.)
        self.assertEqual(series[0]['pace_sec_per_km'], 250.)
        self.assertNotIn('position_lat', json.dumps(result))

    def test_sparse_series_does_not_fill_gaps(self):
        p = [{'timestamp': self.start + timedelta(seconds=30), 'heart_rate': 140}]
        r = build_record(self.meta, [self.session], p)
        self.assertEqual([x['elapsed_sec'] for x in r['fit_details']['timeseries_15s']], [30])
        self.assertIsNone(r['fit_details']['timeseries_15s'][0]['speed_mps'])

    def test_missing_start_and_multisession_fail_closed(self):
        for sessions in ([], [self.session, self.session], [{'sport': 'running'}]):
            with self.assertRaises(SyncError):
                build_record(self.meta, sessions, [])

    def test_ambiguous_and_nonrunning_not_classified(self):
        for key in ('other', 'fitness_equipment', 'cycling', None):
            self.assertIsNone(activity_kind({'activityType': {'typeKey': key}}))
        for key in ('running', 'treadmill_running', 'track_running'):
            self.assertEqual(activity_kind({'activityType': {'typeKey': key}}), key)

    def test_fit_sport_mismatch_rejected(self):
        with self.assertRaises(SyncError):
            build_record(self.meta, [dict(self.session, sport='cycling')], [])

    def test_conflicting_running_subtypes_rejected(self):
        meta = dict(self.meta, activityType={'typeKey': 'track_running'})
        with self.assertRaises(SyncError):
            build_record(meta, [dict(self.session, sub_sport='treadmill')], [])

    def test_historical_comparisons_do_not_replace_records(self):
        old = build_record(self.meta, [self.session], [])
        old['notes'] = 'synthetic observation'
        history = {'running_activities': [old]}
        before = copy.deepcopy(history)
        def parse(meta): return build_record(meta, [dict(self.session, total_distance=110.)], [])
        comparisons = compare_existing(history, [self.meta], parse)
        self.assertAlmostEqual(comparisons[0]['summary']['distance_km']['difference'], .01)
        self.assertEqual(history, before)

    def test_pagination_reads_past_known_activities(self):
        pages = [[{'activityId': 1}, {'activityId': 2}], [{'activityId': 3}], []]
        class Client(GarminClient):
            def __init__(self): self.paths = []
            def _request(self, method, path, **kwargs):
                self.paths.append(path)
                return json.dumps(pages.pop(0)).encode()
        c = Client()
        self.assertEqual([x['activityId'] for x in c.activities(limit=2)], [1, 2, 3])
        self.assertIn('start=3', c.paths[-1])

    def test_repeated_page_is_error(self):
        class Client(GarminClient):
            def __init__(self): pass
            def _request(self, *args, **kwargs): return b'[{"activityId": 1}]'
        with self.assertRaises(SyncError): list(Client().activities())

    def test_zip_requires_exactly_one_fit_without_extraction(self):
        def archive(names):
            b = io.BytesIO()
            with zipfile.ZipFile(b, 'w') as z:
                for n in names: z.writestr(n, b'fake-fit')
            return b.getvalue()
        self.assertEqual(unpack_fit(archive(['folder/activity.fit'])), b'fake-fit')
        for names in ([], ['one.fit', 'two.fit']):
            with self.assertRaises(SyncError): unpack_fit(archive(names))
        with self.assertRaises(SyncError): unpack_fit(b'not zip')

    def test_plan_preserves_every_existing_field_and_notes(self):
        history = {'schema_version': '3.0', 'running_activities': [
            {'garmin_activity_id': '123', 'notes': 'synthetic', 'manual': {'x': [1]}}],
            'activity_count': 1, 'unknown': ['keep']}
        before = copy.deepcopy(history)
        new = dict(self.meta, activityId=124)
        fetched = []
        def parse(meta):
            fetched.append(meta['activityId'])
            return build_record(meta, [self.session], [])
        result = plan(history, [self.meta, new, new], parse)
        self.assertEqual(fetched, [124])
        self.assertEqual(history, before)
        self.assertEqual(len(result['additions']), 1)

    @unittest.skipUnless(TEST_HISTORY.is_file(), 'Optional private-history schema check; private data is not distributed')
    def test_actual_private_copy_schema_readonly(self):
        path = Path('private/running_history_master.test.json')
        before = path.read_bytes()
        history = read_history(path)
        self.assertEqual(history['schema_version'], '3.0')
        r = build_record(self.meta, [self.session], [])
        template = history['running_activities'][0]
        self.assertEqual(set(r), set(template))
        self.assertEqual(set(r['fit_details']), set(template['fit_details']) | {'processing', 'precision'})
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__': unittest.main()
