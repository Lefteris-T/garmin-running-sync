"""Compare processing hypotheses using decoded allowlisted FIT fields only."""
from datetime import datetime
from garmin_sync import cadence, nonnegative, number, preferred


def historical_processing_audit(old, sessions, records):
    if len(sessions) != 1 or not isinstance(sessions[0].get('start_time'), datetime):
        return {'status': 'insufficient session data'}
    session = sessions[0]
    start = session['start_time']
    valid = [r for r in records if isinstance(r.get('timestamp'), datetime)]
    valid.sort(key=lambda r: r['timestamp'])
    origins = {'session_start': start}
    if valid: origins['first_record'] = valid[0]['timestamp']
    candidates = {'distance_m': {}, 'cadence_spm': {}, 'speed_mps': {}, 'hr_bpm': {}}
    old_series = {p['elapsed_sec']: p for p in old['fit_details']['timeseries_15s']}
    getters = {
        'distance_m': {'native': lambda r: nonnegative(r.get('distance'))},
        'cadence_spm': {'integer_only': lambda r: None if nonnegative(r.get('cadence')) is None else 2*nonnegative(r['cadence']),
                        'including_fraction': cadence},
        'speed_mps': {'preferred': lambda r: preferred(r, 'enhanced_speed', 'speed'),
                      'base_only': lambda r: nonnegative(r.get('speed'))},
        'hr_bpm': {'native': lambda r: nonnegative(r.get('heart_rate'))}}
    for origin_name, origin in origins.items():
        buckets = {}
        for r in valid:
            elapsed = (r['timestamp']-origin).total_seconds()
            if elapsed >= 0: buckets.setdefault(int(elapsed//15)*15, []).append(r)
        for field, variants in getters.items():
            for variant, getter in variants.items():
                for method in ('mean', 'last', 'first', 'mean_positive'):
                    diffs = []
                    for elapsed, group in buckets.items():
                        expected = number(old_series.get(elapsed, {}).get(field))
                        values = [v for r in group if (v := getter(r)) is not None]
                        if method == 'mean_positive': values = [v for v in values if v > 0]
                        if expected is None or not values: continue
                        actual = values[-1] if method == 'last' else values[0] if method == 'first' else sum(values)/len(values)
                        diffs.append(actual-expected)
                    candidates[field][f'{origin_name}/{variant}/{method}'] = {
                        'comparable_buckets': len(diffs),
                        'mean_absolute_difference': sum(abs(d) for d in diffs)/len(diffs) if diffs else None,
                        'max_absolute_difference': max(map(abs,diffs)) if diffs else None,
                        'mean_signed_difference': sum(diffs)/len(diffs) if diffs else None}
    base = nonnegative(session.get('avg_cadence'))
    prior = number(old.get('average_cadence_spm'))
    outliers=[]
    buckets={}
    for r in valid:
        elapsed=(r['timestamp']-start).total_seconds()
        if elapsed>=0: buckets.setdefault(int(elapsed//15)*15,[]).append(r)
    for elapsed,group in sorted(buckets.items()):
        expected=number(old_series.get(elapsed,{}).get('cadence_spm'))
        measured=[v for r in group if (v:=nonnegative(r.get('cadence'))) is not None]
        positive=[v for v in measured if v>0]
        if expected is None or not positive: continue
        actual=2*sum(positive)/len(positive)
        if abs(actual-expected)>.050001:
            outliers.append({'elapsed_sec':elapsed,'historical_spm':expected,'integer_positive_mean_spm':actual,
                             'difference_spm':actual-expected,'positive_sample_count':len(positive),
                             'zero_sample_count':measured.count(0),'missing_sample_count':len(group)-len(measured),
                             'min_positive_native_cadence':min(positive),'max_positive_native_cadence':max(positive)})
    return {'summary_cadence': {'history': prior, 'integer_only_spm': None if base is None else base*2,
                               'including_fraction_spm': cadence(session, average=True)},
            'candidate_errors': candidates,
            'cadence_outlier_count':len(outliers), 'cadence_outlier_buckets':outliers[:50],
            'cadence_outlier_limit':50,
            'status': 'comparison only; no automatic processing change'}
