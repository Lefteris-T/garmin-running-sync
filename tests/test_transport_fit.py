import contextlib
import importlib.util
import io
import json
import struct
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from garmin_sync import GarminClient, NoRedirect, SyncError, decode_fit, build_record, SESSION_UNITS


class Reply(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


class FakeOpener:
    def __init__(self, replies): self.replies, self.requests = replies, []
    def open(self, request, timeout):
        self.requests.append(request)
        value = self.replies.pop(0)
        if isinstance(value, Exception): raise value
        return Reply(value)


class TransportTests(unittest.TestCase):
    def test_authentication_and_bearer_are_only_request_headers(self):
        opener = FakeOpener([b'synthetic-token', b'[]'])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            client = GarminClient('synthetic-key', opener)
            self.assertEqual(list(client.activities()), [])
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(opener.requests[0].get_header('Authorization'), 'ApiKey synthetic-key')
        self.assertEqual(opener.requests[1].get_header('Authorization'), 'Bearer synthetic-token')
        self.assertEqual(opener.requests[0].method, 'POST')

    def test_http_error_body_and_url_not_exposed(self):
        error = HTTPError('https://synthetic-secret.invalid', 401, 'synthetic-secret', {}, io.BytesIO(b'synthetic-secret'))
        with self.assertRaises(SyncError) as raised:
            GarminClient('synthetic-key', FakeOpener([error]))
        self.assertNotIn('synthetic', str(raised.exception))
        self.assertIn('401', str(raised.exception))

    def test_redirects_never_forward_authorization(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid'))

    def test_unexpected_api_structure_not_exposed(self):
        client = GarminClient('synthetic-key', FakeOpener([b'synthetic-token', b'{"secret":"synthetic-secret"}']))
        with self.assertRaises(SyncError) as raised: list(client.activities())
        self.assertNotIn('synthetic-secret', str(raised.exception))


def crc(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8): value = (value >> 1) ^ (0xA001 if value & 1 else 0)
    return value


def synthetic_fit():
    # Generated in memory. Native FIT definitions/scales, not a copied personal file.
    def definition(local, global_number, fields):
        return bytes([0x40 | local, 0, 0]) + struct.pack('<H', global_number) + bytes([len(fields)]) + b''.join(bytes(f) for f in fields)
    start = 1_000_000_000
    data = definition(0, 18, [(2,4,0x86), (5,1,0), (8,4,0x86), (9,4,0x86), (14,2,0x84), (18,1,2)])
    data += b'\x00' + struct.pack('<IBIIHB', start, 1, 30000, 12000, 4000, 85)
    data += definition(1, 20, [(253,4,0x86), (3,1,2), (4,1,2), (5,4,0x86), (6,2,0x84), (0,4,0x85)])
    for elapsed, hr in ((0,140), (15,255)):
        data += b'\x01' + struct.pack('<IBBIHi', start+elapsed, hr, 80, elapsed*400, 4000, 123456)
    header = struct.pack('<BBHI4s',12,0x20,2110,len(data),b'.FIT')
    body = header + data
    return body + struct.pack('<H',crc(body))


@unittest.skipUnless(importlib.util.find_spec('fitdecode'), 'fitdecode installation required for binary decoder integration')
class BinaryFitTests(unittest.TestCase):
    def test_fixture_integrity_and_resolved_running_cadence_units(self):
        import fitdecode
        with fitdecode.FitReader(io.BytesIO(synthetic_fit()), check_crc=fitdecode.CrcCheck.RAISE,
                                 error_handling=fitdecode.ErrorHandling.RAISE) as reader:
            frames = list(reader)
        session = next(f for f in frames if f.frame_type == fitdecode.FIT_FRAME_DATA and f.name == 'session')
        field = session.get_field('avg_cadence')
        self.assertEqual(field.name, 'avg_running_cadence')
        self.assertEqual(field.units, 'strides/min')
        self.assertEqual(field.value, 85)
        self.assertTrue(any(f.frame_type == fitdecode.FIT_FRAME_CRC for f in frames))

    def test_unexpected_units_still_rejected(self):
        with patch.dict(SESSION_UNITS, {'total_distance': 'km'}):
            with self.assertRaisesRegex(SyncError, '^Unexpected FIT units; activity requires review\\.$'):
                decode_fit(synthetic_fit())

    def test_native_binary_scaling_invalid_sentinels_and_gps_omission(self):
        sessions, records = decode_fit(synthetic_fit())
        self.assertEqual(sessions[0]['total_distance'], 120.)
        self.assertEqual(sessions[0]['total_timer_time'], 30.)
        self.assertEqual(records[0]['speed'], 4.)
        self.assertNotIn('heart_rate', records[1])
        self.assertNotIn('position_lat', records[0])
        r = build_record({'activityId':1,'activityType':{'typeKey':'running'}}, sessions, records)
        self.assertEqual(r['average_cadence_spm'], 170.)
        self.assertEqual(r['distance_km'], .12)
        self.assertEqual(r['fit_details']['timeseries_15s'][1]['hr_bpm'], None)

    def test_corrupt_crc_is_safe_failure(self):
        import fitdecode
        data = bytearray(synthetic_fit())
        data[-1] ^= 1
        # Prove the fixture reaches its CRC failure, rather than failing earlier
        # because of an unrelated API mismatch or unit conversion bug.
        with self.assertRaises(fitdecode.FitCRCError):
            with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE,
                                     error_handling=fitdecode.ErrorHandling.RAISE) as reader:
                list(reader)
        with self.assertRaises(SyncError) as raised: decode_fit(bytes(data))
        self.assertEqual(str(raised.exception), 'FIT decoding or integrity check failed; details suppressed.')

    def test_truncated_binary_is_safe_failure(self):
        with self.assertRaises(SyncError): decode_fit(synthetic_fit()[:-8])


if __name__ == '__main__': unittest.main()
