"""One release-pinned authority per research scope; no financial authority."""
from datetime import timedelta
from pathlib import Path
from .domain import Refused, digest, strict_json, utc

MR = 'MR-20260926-1'
PHASE2 = 'PHASE2-20260926-1'
SUPERSEDED = 'SUPERSEDED_FOR_MODEL_RECONSTRUCTION'
MR_HASH = 'ae644e7fa113b7d5177626d43408cd6ac4f6f3913d5a679a076ec1793a534074'
WINDOWS = {'TRAIN':['2026-09-27T00:00:00Z','2026-10-11T00:00:00Z'],
           'CALIBRATION':['2026-10-11T00:00:00Z','2026-10-18T00:00:00Z'],
           'VALIDATION':['2026-10-18T00:00:00Z','2026-11-01T00:00:00Z']}
FAMILIES = {'structural':'MR-STRUCTURAL-1','regime':'MR-REGIME-1'}
CATALOG_HASH = '94c68ee0663d9308383bc80623befcf4b7f55d67a2ee58f9c89f9ab75fc1bacb'


def assert_no_overlap(claims):
    active=[]
    for claim in claims:
        if claim['status'] not in ('ACTIVE',SUPERSEDED): raise Refused('unknown protocol authority status')
        if claim['status']!='ACTIVE': continue
        if not claim['markets'] or not claim['windows']: raise Refused('missing protocol authority scope')
        for start,end in claim['windows']:
            if end is not None and utc(start)>=utc(end): raise Refused('invalid authority window')
        active.append(claim)
    for i,a in enumerate(active):
        for b in active[i+1:]:
            if not set(a['markets']) & set(b['markets']): continue
            if any((ae is None or utc(bs)<utc(ae)) and (be is None or utc(ast)<utc(be))
                   for ast,ae in a['windows'] for bs,be in b['windows']):
                raise Refused('PROTOCOL_AUTHORITY_CONFLICT')
    return active


def assert_registry(plan):
    if digest(plan)!=MR_HASH: raise Refused('MR_PROTOCOL_HASH_MISMATCH')
    if plan['protocol_id']!=MR or plan['windows']!=WINDOWS: raise Refused('MR_STAGE_BOUNDARY_MISMATCH')
    windows=list(plan['windows'].values())
    if any(utc(a)<utc(d) and utc(c)<utc(b) for i,(a,b) in enumerate(windows) for c,d in windows[i+1:]):
        raise Refused('MR_STAGE_OVERLAP')
    if len(plan['candidates'])!=2 or {c['family']:c['id'] for c in plan['candidates']}!=FAMILIES:
        raise Refused('MR_TWO_FAMILY_MULTIPLICITY_REQUIRED')
    if plan['acceptance']['block_ci_confidence']!=.975 or plan['oos_days']!=28:
        raise Refused('MR_MULTIPLICITY_OR_OOS_CHANGED')
    return plan


def authority():
    catalog=strict_json(Path(__file__).with_name('PROTOCOL_AUTHORITY.json').read_bytes())
    historical=strict_json(Path(__file__).resolve().parents[1].joinpath('TRAINING_PROTOCOL.json').read_bytes())
    claims=[dict(c) for c in catalog['claims']]
    for claim in claims:
        if claim['protocol_id']==PHASE2: claim['status']=historical.get('authority_status','ACTIVE')
    active=assert_no_overlap(claims)
    if historical.get('authority_status')!=SUPERSEDED or historical.get('superseded_by')!=MR:
        raise Refused('PROTOCOL_AUTHORITY_CONFLICT')
    if digest(historical['protocol'])!=historical['protocol_hash'] or historical['protocol_hash']!='751652ff4e2f5a9e9aa223952a655a34cc598dcc8d179f564d597c18002f41df':
        raise Refused('HISTORICAL_PROTOCOL_HASH_MISMATCH')
    if digest(catalog)!=CATALOG_HASH: raise Refused('PROTOCOL_AUTHORITY_HASH_MISMATCH')
    mr=[c for c in active if 'KXBTC15M' in c['markets']]
    if len(mr)!=1 or mr[0]['protocol_id']!=MR: raise Refused('PROTOCOL_AUTHORITY_CONFLICT')
    plan=assert_registry(strict_json(Path(__file__).with_name('CHALLENGER_REGISTRY.json').read_bytes()))
    return plan


def require_active(protocol_id):
    authority()
    if protocol_id!=MR: raise Refused(SUPERSEDED)


def reject_mr_rows(rows):
    """Legacy diagnostic functions cannot accept any MR lineage."""
    authority()
    for row in rows:
        if (row.get('protocol_id')==MR or row.get('protocol_hash')==MR_HASH
                or row.get('candidate_family') in FAMILIES.values() or row.get('feature_schema')=='MR-FEATURES-1'
                or row.get('features',{}).get('feature_schema')=='MR-FEATURES-1'):
            raise Refused('SUPERSEDED_PROTOCOL_CANNOT_CONSUME_MR_ROWS')


def oos_window(locked_at):
    require_active(MR)
    lock=utc(locked_at)
    if lock<utc(WINDOWS['VALIDATION'][1]): raise Refused('MR_LOCK_BEFORE_VALIDATION_END')
    start=(lock+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0)
    return start.isoformat(),(start+timedelta(days=28)).isoformat()


def stage_at(at, locked_at=None):
    require_active(MR)
    point=utc(at)
    for stage,(start,end) in WINDOWS.items():
        if utc(start)<=point<utc(end): return stage
    if locked_at is not None:
        start,end=oos_window(locked_at)
        if utc(start)<=point<utc(end): return 'FUTURE_OOS'
    raise Refused('MR_DECISION_OUTSIDE_REGISTERED_STAGE')


def binding(at, family, locked_at=None):
    if family not in FAMILIES: raise Refused('MR_UNREGISTERED_FAMILY')
    return {'protocol_id':MR,'protocol_hash':MR_HASH,'candidate_family':FAMILIES[family],
            'feature_schema':'MR-FEATURES-1','stage':stage_at(at,locked_at)}


def assert_row(row, stage, family=None):
    require_active(MR)
    f=row['features']
    expected=binding(f['decision_at'],family or next((k for k,v in FAMILIES.items() if v==row.get('candidate_family')),None))
    if any(row.get(k)!=v for k,v in expected.items()) or row.get('stage')!=stage:
        raise Refused('MR_ROW_PROTOCOL_BINDING_MISMATCH')
    if row.get('source_protocol_id')!=MR or row.get('prior_candidate_uses')!=[]:
        raise Refused('MR_CONSUMED_OR_FOREIGN_PROTOCOL_ROW')
