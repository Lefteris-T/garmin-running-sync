import copy
import unittest
from datetime import datetime, timedelta, timezone
from garmin_sync import build_record, plan, type_counts, type_category, type_key
from processing_audit import historical_processing_audit


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.start = datetime(2026,1,1,tzinfo=timezone.utc)
        self.meta = {'activityId': 1, 'activityType': {'typeKey': 'running'}}
        self.session = {'sport':'running', 'start_time':self.start, 'avg_cadence':78,
                        'avg_fractional_cadence':.75}
        self.records = [{'timestamp':self.start+timedelta(seconds=s), 'distance':s*3,
                         'cadence':78, 'fractional_cadence':.75, 'heart_rate':140, 'speed':3}
                        for s in range(30)]

    def test_audit_distinguishes_fraction_and_last_distance_without_changing_history(self):
        old = build_record(self.meta,[self.session],self.records)
        old['average_cadence_spm'] = 156.
        for i,p in enumerate(old['fit_details']['timeseries_15s']):
            p['distance_m'] = (i*15+14)*3
            p['cadence_spm'] = 156.
        before = copy.deepcopy(old)
        audit = historical_processing_audit(old,[self.session],self.records)
        self.assertEqual(audit['summary_cadence']['integer_only_spm'],156.)
        self.assertEqual(audit['summary_cadence']['including_fraction_spm'],157.5)
        scores = audit['candidate_errors']
        self.assertEqual(scores['distance_m']['session_start/native/last']['max_absolute_difference'],0)
        self.assertGreater(scores['distance_m']['session_start/native/mean']['mean_absolute_difference'],0)
        self.assertEqual(scores['cadence_spm']['session_start/integer_only/mean']['max_absolute_difference'],0)
        self.assertEqual(old,before)

    def test_type_counts_distinguish_explicit_nonrunning_unknown_and_running(self):
        rows = [{'activityType':{'typeKey':k}} for k in ('running','walking','walking','other','future_run')]
        counts = {(x['category'],x['type']):x['count'] for x in type_counts(rows)}
        self.assertEqual(counts,{('running','running'):1,('non_running','walking'):2,
                                 ('unknown','other'):1,('unknown','future_run'):1})
        self.assertEqual(type_category(rows[-1]),'unknown')
        self.assertEqual(type_key({'activityType':{'typeKey':'private title 123'}}),'<missing-or-invalid>')

    def test_outliers_report_evidence_without_changing_positive_measurements(self):
        old=build_record(self.meta,[self.session],self.records)
        old['fit_details']['timeseries_15s'][0]['cadence_spm']=200
        before=copy.deepcopy(self.records)
        audit=historical_processing_audit(old,[self.session],self.records)
        self.assertEqual(audit['cadence_outlier_count'],1)
        outlier=audit['cadence_outlier_buckets'][0]
        self.assertEqual(outlier['min_positive_native_cadence'],78)
        self.assertEqual(outlier['difference_spm'],-44)
        self.assertEqual(self.records,before)

    def test_unknown_fit_running_is_discovered_without_mutating_metadata(self):
        meta = dict(self.meta,activityType={'typeKey':'future_run'})
        before = copy.deepcopy(meta)
        result = plan({'running_activities':[]},[meta],lambda m: self.fail('not used'),
                      lambda m: ([self.session],self.records))
        self.assertEqual(len(result['additions']),1)
        self.assertEqual(result['additions'][0]['activity_type'],'running')
        self.assertEqual(len(result['unknown_resolutions']),1)
        self.assertEqual(meta,before)

    def test_explicit_nonrunning_not_downloaded_and_ambiguous_fit_stays_review(self):
        rows = [dict(self.meta,activityId=i,activityType={'typeKey':k}) for i,k in ((1,'walking'),(2,'other'))]
        called=[]
        def inspect(meta):
            called.append(meta['activityId'])
            return ([dict(self.session,sport='generic')],[])
        result=plan({'running_activities':[]},iter(rows),lambda m: self.fail('not used'),inspect)
        self.assertEqual(called,[2])
        self.assertEqual(len(result['excluded_non_running']),1)
        self.assertEqual(len(result['review']),1)
        self.assertEqual(len(result['type_counts']),2)

    def test_unknown_explicit_fit_nonrunning_excluded_but_training_ambiguous(self):
        meta=dict(self.meta,activityType={'typeKey':'other'})
        for sport,excluded in (('cycling',1),('training',0)):
            result=plan({'running_activities':[]},[meta],lambda m: self.fail('not used'),
                        lambda m: ([dict(self.session,sport=sport)],[]))
            self.assertEqual(len(result['excluded_non_running']),excluded)
            self.assertEqual(len(result['additions']),0)


if __name__ == '__main__': unittest.main()
