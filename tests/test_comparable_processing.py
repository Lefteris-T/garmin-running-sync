import unittest
from datetime import datetime, timedelta, timezone
from garmin_sync import build_record, plan


class ComparableTests(unittest.TestCase):
    def test_legacy_fields_and_explicit_precision_extension(self):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        session={'sport':'running','start_time':start,'avg_cadence':78,'avg_fractional_cadence':.75}
        records=[{'timestamp':start+timedelta(seconds=s),'cadence':c,'fractional_cadence':f,
                  'speed':v,'distance':d,'heart_rate':h}
                 for s,c,f,v,d,h in [(0,0,0,0,0,140),(7,78,.75,3.123,20,141),(14,80,.25,3.456,42.34,142)]]
        r=build_record({'activityId':1,'activityType':{'typeKey':'running'}},[session],list(reversed(records)))
        self.assertEqual(r['average_cadence_spm'],156)
        p=r['fit_details']['timeseries_15s'][0]
        self.assertEqual(p['distance_m'],42.3)
        self.assertEqual(p['cadence_spm'],158)
        self.assertEqual(p['speed_mps'],3.3)
        precise=r['fit_details']['precision']
        self.assertEqual(precise['average_cadence_spm'],157.5)
        q=precise['timeseries_15s'][0]
        self.assertEqual(q['cadence_spm'],159)
        self.assertEqual(q['distance_m'],42.34)
        self.assertAlmostEqual(q['cadence_spm_including_zero'],106)
        self.assertEqual(r['fit_details']['processing']['version'],'history-compatible-v1')

    def test_zero_only_bucket_has_null_active_cadence_and_speed_but_preserves_zero(self):
        start=datetime(2026,1,1,tzinfo=timezone.utc)
        r=build_record({'activityId':1,'activityType':{'typeKey':'running'}},
                       [{'sport':'running','start_time':start}],
                       [{'timestamp':start,'cadence':0,'speed':0}])
        legacy=r['fit_details']['timeseries_15s'][0]
        precise=r['fit_details']['precision']['timeseries_15s'][0]
        self.assertIsNone(legacy['cadence_spm'])
        self.assertIsNone(legacy['speed_mps'])
        self.assertEqual(precise['cadence_spm_including_zero'],0)
        self.assertEqual(precise['speed_mps_including_zero'],0)

    def test_indoor_cardio_ambiguous_stays_out_of_imports(self):
        meta={'activityId':1,'activityType':{'typeKey':'indoor_cardio'}}
        for sessions in ([],[{'sport':'training'}],[{'sport':'generic'}],[{'sport':'running'},{'sport':'training'}]):
            result=plan({'running_activities':[]},[meta],lambda m: self.fail('not a recognized running type'),lambda m:(sessions,[]))
            self.assertEqual(result['additions'],[])
            self.assertEqual(len(result['review']),1)


if __name__=='__main__': unittest.main()
