"""Offline, append-only updates restricted to the project's private test copy."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

from garmin_sync import (TEST_HISTORY, ROOT, PROCESSING_VERSION, SyncError, activity_id,
                         activity_kind, read_history, number)

TARGET = TEST_HISTORY
TOP_FIELDS = frozenset('garmin_activity_id date local_start activity_type title distance_km duration_seconds duration average_pace_seconds_per_km average_pace average_hr_bpm max_hr_bpm elevation_gain_m elevation_loss_m average_cadence_spm vo2max_estimate source fit_record_count fit_match_minutes fit_details'.split())
DETAIL_FIELDS = frozenset('record_count sampling_interval_seconds timeseries_15s km_splits_estimated timestamp_timezone processing precision'.split())
SERIES_FIELDS = frozenset('elapsed_sec hr_bpm cadence_spm speed_mps pace_sec_per_km distance_m temperature_c'.split())
PROCESSING_FIELDS = frozenset('version distance cadence speed series_rounding_decimals limitations'.split())
PRECISION_EXTRA = frozenset({'cadence_spm_including_zero','speed_mps_including_zero'})


def safe_target():
    """No CLI argument can select a different history. Reject aliases and links."""
    try:
        target = Path(TARGET).absolute()
        if target.name != 'running_history_master.test.json': raise ValueError()
        target.relative_to(ROOT / 'private')
        if target.resolve(strict=True) != target or target.stat().st_nlink != 1: raise ValueError()
        for p in (target, *target.parents):
            if p == ROOT: break
            stat = p.lstat()
            if p.is_symlink() or getattr(stat, 'st_file_attributes', 0) & 0x400: raise ValueError()
        if not target.is_file(): raise ValueError()
        return target
    except Exception:
        raise SyncError('Update target is not the permitted regular private test-copy file.') from None


def snapshot():
    target = safe_target()
    original = target.read_bytes()
    history = read_history(target)
    if json.loads(original.decode('utf-8-sig')) != history:
        raise SyncError('Test history changed while it was being read.')
    return target, original, history


def validate_new_record(record):
    """Validate only new records; never normalize or strip old manual fields."""
    try:
        if not isinstance(record, dict) or not TOP_FIELDS <= record.keys() or record.keys()-TOP_FIELDS-{'notes'}: raise ValueError()
        activity_id(record['garmin_activity_id'])
        if not activity_kind({'activityType':{'typeKey':record['activity_type']}}): raise ValueError()
        for key in ('date','local_start','title','duration','average_pace','source'):
            if record[key] is not None and not isinstance(record[key],str): raise ValueError()
        for key in TOP_FIELDS-{'garmin_activity_id','fit_details','activity_type','date','local_start','title','duration','average_pace','source'}:
            if record[key] is not None and number(record[key]) is None: raise ValueError()
        if 'notes' in record and not isinstance(record['notes'],str): raise ValueError()
        details = record['fit_details']
        if set(details) != DETAIL_FIELDS or details['sampling_interval_seconds'] != 15: raise ValueError()
        processing = details['processing']
        if set(processing) != PROCESSING_FIELDS or processing['version'] != PROCESSING_VERSION: raise ValueError()
        if any(not isinstance(processing[k],str) for k in ('distance','cadence','speed')): raise ValueError()
        if not isinstance(processing['limitations'],list) or any(not isinstance(x,str) for x in processing['limitations']): raise ValueError()
        if processing['series_rounding_decimals'] != 1: raise ValueError()
        if details['km_splits_estimated'] != []: raise ValueError()
        if not isinstance(details['record_count'],int) or isinstance(details['record_count'],bool) or details['record_count'] < 0: raise ValueError()
        if record['fit_record_count'] != details['record_count']: raise ValueError()
        precision = details['precision']
        if set(precision) != {'average_cadence_spm','timeseries_15s'}: raise ValueError()
        if precision['average_cadence_spm'] is not None and number(precision['average_cadence_spm']) is None: raise ValueError()
        legacy, precise = details['timeseries_15s'], precision['timeseries_15s']
        if not isinstance(legacy,list) or not isinstance(precise,list) or len(legacy) != len(precise): raise ValueError()
        prior = -1
        for a,b in zip(legacy,precise):
            if set(a) != SERIES_FIELDS or set(b) != SERIES_FIELDS | PRECISION_EXTRA: raise ValueError()
            elapsed = a['elapsed_sec']
            if type(elapsed) is not int or elapsed < 0 or elapsed%15 or elapsed<=prior or b['elapsed_sec'] != elapsed: raise ValueError()
            prior=elapsed
            for p in (a,b):
                for key,value in p.items():
                    if value is not None and number(value) is None: raise ValueError()
        # Refuse NaN, objects, and non-JSON values; never serialize exception data.
        json.dumps(record, allow_nan=False)
    except Exception:
        raise SyncError('New record validation failed; no history update attempted.') from None


def deduplicate_additions(additions):
    if not isinstance(additions,list): raise SyncError('Plan additions must be a list.')
    unique={}
    for record in additions:
        validate_new_record(record)
        identifier=activity_id(record['garmin_activity_id'])
        if identifier in unique and unique[identifier] != record:
            raise SyncError('Plan contains conflicting records for one new activity ID.')
        unique[identifier]=copy.deepcopy(record)
    return list(unique.values())


def make_update_plan(additions):
    _, original, _ = snapshot()
    return {'plan_version':1, 'processing_version':PROCESSING_VERSION,
            'base_sha256':hashlib.sha256(original).hexdigest(),
            'additions':deduplicate_additions(additions)}


def prepare(proposal):
    target, original, history = snapshot()
    if not isinstance(proposal,dict) or proposal.get('plan_version') != 1 or proposal.get('processing_version') != PROCESSING_VERSION:
        raise SyncError('Plan is outdated or unsupported; generate a new dry run.')
    if proposal.get('base_sha256') != hashlib.sha256(original).hexdigest():
        raise SyncError('Plan is stale; generate a new dry run before updating the test copy.')
    additions=deduplicate_additions(proposal.get('additions'))
    known={activity_id(r['garmin_activity_id']) for r in history['running_activities']}
    new=[r for r in additions if activity_id(r['garmin_activity_id']) not in known]
    merged=copy.deepcopy(history)
    merged['running_activities'].extend(new)
    merged['activity_count']=len(merged['running_activities'])
    result={'added':len(new), 'duplicates_skipped':len(additions)-len(new), 'backup':None}
    return target, original, merged, result


def preview(proposal):
    return prepare(proposal)[-1]


def write_backup(target, original):
    backup=target.with_name(target.name+'.'+uuid.uuid4().hex+'.bak')
    try:
        with backup.open('xb') as out:
            out.write(original); out.flush(); os.fsync(out.fileno())
        if backup.read_bytes() != original: raise OSError()
        return backup
    except Exception:
        if backup.exists(): backup.unlink()
        raise


def apply_test_copy(proposal):
    """No network calls. Backup verified before atomic same-directory replacement."""
    target=safe_target()
    lock=target.with_suffix('.json.lock')
    locked=False
    temporary=None
    try:
        try:
            with lock.open('xb') as out: out.write(b'private test-copy update\n')
            locked=True
        except FileExistsError:
            raise SyncError('Test-copy update lock exists; another update may be active.') from None
        target, original, merged, result=prepare(proposal)
        if not result['added']: return result
        encoded=(json.dumps(merged,ensure_ascii=False,indent=2,allow_nan=False)+'\n').encode('utf-8')
        # Validate serialized structure before writing anything to the history.
        if json.loads(encoded) != merged: raise SyncError('Serialization verification failed.')
        backup=write_backup(target,original)
        result['backup']=str(backup)
        temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.tmp')
        with temporary.open('xb') as out:
            out.write(encoded); out.flush(); os.fsync(out.fileno())
        if temporary.read_bytes() != encoded: raise SyncError('Temporary file verification failed.')
        # Protect against cooperating writers and detect intervening manual edits.
        if safe_target() != target or target.read_bytes() != original:
            raise SyncError('Test history changed before replacement; update stopped.')
        os.replace(temporary,target)
        return result
    except SyncError: raise
    except Exception:
        raise SyncError('Test-copy update failed; error details suppressed. Check the unchanged history and any backup.') from None
    finally:
        if temporary and temporary.exists(): temporary.unlink()
        if locked: lock.unlink()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan',type=Path,required=True)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run',action='store_true')
    mode.add_argument('--apply-test-copy',action='store_true')
    args=parser.parse_args()
    try:
        proposal=json.loads(args.plan.read_text(encoding='utf-8-sig'))
        result=preview(proposal) if args.dry_run else apply_test_copy(proposal)
        print('Test-copy preview.' if args.dry_run else 'Private test-copy update complete.')
        print('Additions:',result['added'],'duplicates skipped:',result['duplicates_skipped'])
        print('Backup created:',bool(result['backup']))
        print('Original history was not opened or changed.')
        return 0
    except SyncError as exc:
        print('Stopped:',str(exc),file=sys.stderr); return 1
    except Exception:
        print('Stopped: invalid plan or file access failure; details suppressed.',file=sys.stderr); return 1


if __name__=='__main__': sys.exit(main())
