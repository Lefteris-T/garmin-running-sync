import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import uuid
import unittest
from unittest.mock import patch

import history_update as updater
from garmin_sync import SyncError, TEST_HISTORY


class UpdateTests(unittest.TestCase):
    def setUp(self):
        TEST_HISTORY.parent.mkdir(parents=True,exist_ok=True)
        # Python 3.13's tempfile owner-only Windows ACL is incompatible with the
        # agent's restricted token. Inherit the private project's ACL instead.
        self.root = TEST_HISTORY.parent / ('update-test-' + uuid.uuid4().hex)
        self.root.mkdir()
        self.target = self.root / 'running_history_master.test.json'
        self.history = {'schema_version':'3.0', 'activity_count':1,
                        'running_activities':[{'garmin_activity_id':1,'notes':'synthetic note',
                                               'manual':{'unchanged':[1,2]}}],
                        'manual_root':['preserve']}
        self.target.write_text(json.dumps(self.history),encoding='utf-8')
        self.guard = patch.object(updater, 'TARGET', self.target)
        self.guard.start()

    def tearDown(self):
        self.guard.stop()
        resolved=self.root.resolve()
        resolved.relative_to(TEST_HISTORY.parent.resolve())
        if not resolved.name.startswith('update-test-'): raise RuntimeError('Unsafe cleanup target')
        shutil.rmtree(resolved)

    def record(self, identifier=2):
        from datetime import datetime, timezone
        from garmin_sync import build_record
        return build_record({'activityId':identifier,'activityType':{'typeKey':'running'}},
                            [{'sport':'running','start_time':datetime(2026,1,1,tzinfo=timezone.utc)}],[])

    def test_backup_and_append_preserve_existing_data_and_repeat_is_noop(self):
        before=self.target.read_bytes()
        proposal=updater.make_update_plan([self.record(),self.record()])
        result=updater.apply_test_copy(proposal)
        self.assertEqual(result['added'],1)
        self.assertEqual(Path(result['backup']).read_bytes(),before)
        after=json.loads(self.target.read_text(encoding='utf-8'))
        self.assertEqual(after['running_activities'][0],self.history['running_activities'][0])
        self.assertEqual(after['manual_root'],self.history['manual_root'])
        self.assertEqual(after['activity_count'],2)
        no_op=updater.apply_test_copy(updater.make_update_plan([self.record()]))
        self.assertEqual(no_op['added'],0)
        self.assertIsNone(no_op['backup'])

    def test_dry_run_changes_no_bytes_and_creates_no_backup(self):
        before=self.target.read_bytes()
        proposal=updater.make_update_plan([self.record()])
        self.assertEqual(updater.preview(proposal)['added'],1)
        self.assertEqual(before,self.target.read_bytes())
        self.assertFalse(list(self.root.glob('*.bak')))

    def test_stale_plan_cannot_overwrite_new_manual_notes(self):
        proposal=updater.make_update_plan([self.record()])
        self.target.write_bytes(self.target.read_bytes()+b'\n')
        before=self.target.read_bytes()
        with self.assertRaises(SyncError): updater.apply_test_copy(proposal)
        self.assertEqual(self.target.read_bytes(),before)

    def test_replace_failure_preserves_original_and_verified_backup(self):
        before=self.target.read_bytes()
        proposal=updater.make_update_plan([self.record()])
        with patch.object(updater.os,'replace',side_effect=OSError('synthetic failure')):
            with self.assertRaises(SyncError): updater.apply_test_copy(proposal)
        self.assertEqual(self.target.read_bytes(),before)
        backups=list(self.root.glob('*.bak'))
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),before)
        self.assertFalse(list(self.root.glob('*.tmp')))
        self.assertFalse(list(self.root.glob('*.lock')))

    def test_backup_failure_prevents_replacement(self):
        proposal=updater.make_update_plan([self.record()])
        before=self.target.read_bytes()
        with patch.object(updater,'write_backup',side_effect=OSError('synthetic failure')):
            with self.assertRaises(SyncError): updater.apply_test_copy(proposal)
        self.assertEqual(self.target.read_bytes(),before)

    def test_intervening_manual_edit_is_not_overwritten(self):
        proposal=updater.make_update_plan([self.record()])
        original=updater.write_backup
        edited=self.target.read_bytes()+b'\n'
        def edit_after_backup(target,before):
            backup=original(target,before)
            target.write_bytes(edited)
            return backup
        with patch.object(updater,'write_backup',side_effect=edit_after_backup):
            with self.assertRaises(SyncError): updater.apply_test_copy(proposal)
        self.assertEqual(self.target.read_bytes(),edited)

    def test_original_history_paths_cannot_be_selected(self):
        with patch.object(updater,'TARGET',TEST_HISTORY.parent.parent/'running_history_master.json'):
            with self.assertRaises(SyncError): updater.safe_target()

    def test_hard_link_alias_is_rejected(self):
        alias=self.root/'alias.json'
        os.link(self.target,alias)
        with self.assertRaises(SyncError): updater.make_update_plan([self.record()])

    def test_existing_lock_stops_update(self):
        proposal=updater.make_update_plan([self.record()])
        self.target.with_suffix('.json.lock').write_bytes(b'locked')
        with self.assertRaises(SyncError): updater.apply_test_copy(proposal)

    def test_duplicate_conflict_within_plan_rejected(self):
        a=self.record()
        b=copy.deepcopy(a); b['notes']='different'
        with self.assertRaises(SyncError): updater.make_update_plan([a,b])

    def test_gps_or_unknown_fields_and_old_processing_rejected(self):
        for location in ('root','details'):
            r=self.record()
            (r if location=='root' else r['fit_details'])['position_lat']=123
            with self.assertRaises(SyncError): updater.make_update_plan([r])
        r=self.record(); r['fit_details']['processing']['version']='old'
        with self.assertRaises(SyncError): updater.make_update_plan([r])
        r=self.record(); r['title']={'position_lat':123}
        with self.assertRaises(SyncError): updater.make_update_plan([r])

    def test_existing_id_never_replaces_notes_even_when_incoming_differs(self):
        before=self.target.read_bytes()
        result=updater.apply_test_copy(updater.make_update_plan([self.record(1)]))
        self.assertEqual(result['added'],0)
        self.assertEqual(self.target.read_bytes(),before)

    @unittest.skipUnless(TEST_HISTORY.is_file(), 'Optional private-history preservation check; private data is not distributed')
    def test_full_history_copy_preserves_every_existing_record(self):
        self.target.write_bytes(TEST_HISTORY.read_bytes())
        old=json.loads(self.target.read_text(encoding='utf-8-sig'))
        identifiers={str(a['garmin_activity_id']) for a in old['running_activities']}
        synthetic_id=1
        while str(synthetic_id) in identifiers: synthetic_id+=1
        result=updater.apply_test_copy(updater.make_update_plan([self.record(synthetic_id)]))
        new=json.loads(self.target.read_text(encoding='utf-8'))
        self.assertEqual(result['added'],1)
        self.assertTrue(new['running_activities'][:-1] == old['running_activities'],
                        'An existing private record changed; contents suppressed.')
        for key in old.keys()-{'running_activities','activity_count'}:
            self.assertTrue(new[key] == old[key], 'Existing private metadata changed; contents suppressed.')


if __name__=='__main__': unittest.main()
