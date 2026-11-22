import _guard

import ast
import json
from pathlib import Path
import pytest
import yaml

from angareion.decision import (FLOW_DEPTH_MAX, Record, fold, snapshot_for, plan_submit, build_payload,
                                parse_payload, PayloadError, SubmitRejected, related_count,
                                _PayloadLoader, _TooDeep)
from angareion.messages import Message, MessageValidationError

P = 'mystore-pm-w1'
H = 'mystore-impl-w1-1'
H2 = 'mystore-impl-w1-2'
CH = 'team-test'

def uid(n): return f'{n:026d}'
def question(**kw):
    base = dict(answerer=H, title='題 😀', background='', body='內容',
                options=[{'key': 'A', 'text': '甲'}, {'key': 'B', 'text': '乙'}],
                recommended='A', reason='')
    return dict(base, **kw)
def rec(n, op, *, sender=P, time=0, position=None, **fields):
    if op == 'ask': fields = dict(ask_channel=CH, asker=sender, **fields)
    to = fields.get('asker', P) if op in ('submit', 'snapshot') else H
    content = yaml.safe_dump(dict(decision=1, op=op, **fields), allow_unicode=True, sort_keys=False)
    m = Message(from_=sender, to=to, channel=CH, id=uid(n), at='2026-01-01T00:00:00+00:00',
                datatype='decision', mentions=[to] if op == 'submit' else [], content=content)
    return Record(m, n if position is None else position, f'2026-01-01T00:{time//60:02d}:{time%60:02d}+00:00')
def ask(n, **kw): return rec(n,'ask',**question(**kw))
def update(n, ref, **kw): return rec(n,'update',ref=uid(ref),**question(**kw))
def submit(n, ref, base=None, sender=H, **kw):
    return rec(n,'submit',sender=sender,asker=P,items=[dict(ref=uid(ref),base=uid(base or ref),title='t',**kw)])
def normal(n, time=120):
    message = Message(from_=P,to=H,channel=CH,id=uid(n),content='message')
    return Record(message,n,f'2026-01-01T00:{time//60:02d}:{time%60:02d}+00:00')
def render(ch, asker, items): return yaml.safe_dump(items, allow_unicode=True)
def codes(result): return [x['code'] for x in result.ignored]

def test_c6_binding():
    r=ask(1); r.message.from_=H
    result=fold([r],use_snapshot=False)
    assert codes(result)==['binding_mismatch'] and result.decisions==[]

def test_c8_arrival_order_and_versions():
    records=[ask(1),update(2,1),update(3,1)]
    a=fold(records,use_snapshot=False).decisions[0]
    for r in records: r.message.at='1990' if r.position==3 else '2099'
    b=fold(records,use_snapshot=False).decisions[0]
    assert a==b and a['version']==uid(3) and a['revisions']==3

def test_c9_owner():
    r=update(2,1); r.message.from_=H
    w=rec(3,'withdraw',sender=H,ref=uid(1))
    result=fold([ask(1),r,w],use_snapshot=False)
    assert codes(result)==['not_owner','not_owner'] and result.decisions[0]['status']=='open'

@pytest.mark.parametrize('sender,base,choice,expected', [
    (H,1,'A','not_answerer'), (H2,1,'A','stale_base'), (H2,2,'C','bad_item'), (H2,2,'A',None)])
def test_c10_c27_submit_order(sender,base,choice,expected):
    result=fold([ask(1),update(2,1,answerer=H2),submit(3,1,base,sender,choice=choice)],use_snapshot=False)
    assert codes(result)==([expected] if expected else [])
    assert result.decisions[0]['status']==('open' if expected else 'answered')

def test_c11_duplicate_and_race():
    first=submit(2,1,choice='A'); second=submit(3,1,choice='B')
    result=fold([ask(1),first,second],use_snapshot=False)
    assert result.decisions[0]['submitted']['choice']=='A' and codes(result)==['closed_ref']
    item=yaml.safe_load(first.message.content)['items'][0]
    first.message.content=yaml.safe_dump(dict(decision=1,op='submit',asker=P,items=[item,item]))
    result=fold([ask(1),first],use_snapshot=False)
    assert result.ignored[0]['code']=='duplicate_ref' and result.ignored[0]['item']==1

def test_c15_bad_payload_and_partial_submit():
    a=ask(1); b=ask(2); c=ask(3)
    s=submit(4,1,choice='A'); items=yaml.safe_load(s.message.content)['items']
    items += [dict(ref=uid(2),base=uid(8),title='x',choice='A'),dict(ref=uid(3),base=uid(3),title='x',note='😀')]
    s.message.content=yaml.safe_dump(dict(decision=1,op='submit',asker=P,items=items),allow_unicode=True)
    result=fold([a,b,c,s],use_snapshot=False)
    assert [(d['ref'],d['item'],d['code']) for d in result.ignored]==[(uid(2),1,'stale_base')]
    assert [d['status'] for d in result.decisions]==['answered','open','answered']
    bad=ask(5); bad.message.content='decision: 2\nop: ask'
    assert codes(fold([bad],use_snapshot=False))==['unsupported_version']

def test_payload_strict_and_unicode():
    p=build_payload('ask',ask_channel=CH,asker=P,**question())
    m=ask(1).message; m.content=p
    assert parse_payload(m)['title']=='題 😀'
    with pytest.raises(MessageValidationError): build_payload('ask',ask_channel=CH,asker=P,extra=1,**question())
    m.content='[bad'
    with pytest.raises(PayloadError): parse_payload(m)

# -- #482 PR-1: flow nesting is capped at the scanner (FLOW_DEPTH_MAX) --

def _ask_with_deep_unknown_key(nest):
    # A complete ask, so that only the nesting depth can make it bad.
    return build_payload('ask',ask_channel=CH,asker=P,**question())+'extra: '+nest+'\n'

@pytest.mark.parametrize('nest', [
    '['*(FLOW_DEPTH_MAX+1)+']'*(FLOW_DEPTH_MAX+1),
    '['*40000,
    '{'*(FLOW_DEPTH_MAX+1),
    '{['*(FLOW_DEPTH_MAX//2+1),
    '{'*40000,
])
def test_482_flow_nested_past_the_cap_is_bad_payload_and_the_rest_still_folds(nest):
    bad=ask(2); bad.message.content=_ask_with_deep_unknown_key(nest)
    result=fold([ask(1),bad,ask(3)],use_snapshot=False)
    assert codes(result)==['bad_payload']
    assert [d['status'] for d in result.decisions]==['open','open']

def test_482_a_legal_ask_with_a_cap_deep_unknown_key_still_reads():
    r=ask(1)
    r.message.content=_ask_with_deep_unknown_key('['*FLOW_DEPTH_MAX+']'*FLOW_DEPTH_MAX)
    result=fold([r],use_snapshot=False)
    assert result.ignored==[] and result.decisions[0]['title']=='題 😀'

def test_482_the_cap_is_32():
    # The design (#482 section 1.2) fixes 32: legal payloads reach depth 5 at most, so the cap
    # keeps a 6x margin. A different number is a design change, not a tweak.
    assert FLOW_DEPTH_MAX==32

def test_482_the_cap_covers_flow_mappings_and_mixed_nesting():
    for nest in ('{'*(FLOW_DEPTH_MAX+1), '{['*(FLOW_DEPTH_MAX//2+1), '['*FLOW_DEPTH_MAX+'{'):
        with pytest.raises(_TooDeep):
            yaml.load(nest,Loader=_PayloadLoader)

def test_482_the_scanner_stops_deep_nesting_before_recursion():
    with pytest.raises(_TooDeep):
        yaml.load('['*40000,Loader=_PayloadLoader)
    assert yaml.load('['*FLOW_DEPTH_MAX+']'*FLOW_DEPTH_MAX,Loader=_PayloadLoader)is not None
    with pytest.raises(_TooDeep):
        yaml.load('['*(FLOW_DEPTH_MAX+1)+']'*(FLOW_DEPTH_MAX+1),Loader=_PayloadLoader)

def test_482_a_json_style_submit_is_still_accepted():
    s=submit(2,1,choice='A')
    s.message.content=json.dumps(yaml.safe_load(s.message.content),ensure_ascii=False)
    result=fold([ask(1),s],use_snapshot=False)
    assert codes(result)==[] and result.decisions[0]['status']=='answered'

def snap(records, n=9):
    content=snapshot_for(records,P); assert content
    m=Message(from_=P,to=P,channel=CH,id=uid(n),content=content,datatype='decision')
    return Record(m,n,'2026-01-01T00:03:00+00:00')

def test_c16_c26_snapshot_projection_and_tombstone():
    initial=[ask(1),update(2,1),ask(3),normal(4)]
    s=snap(initial)
    p=yaml.safe_load(s.message.content)
    assert p['through']==uid(3) and p['items'][0]['version']==uid(2)
    later=update(10,1)
    records=initial+[s,later]
    fast=fold(records,use_snapshot=True); full=fold(records,use_snapshot=False)
    assert fast.snapshot_used[P]==uid(9) and fast.folded==2
    assert fast.decisions==[q for q in full.decisions if q['status']=='open']
    assert fast.seen==full.seen and fast.closed==full.closed
    initial=[ask(1),rec(2,'withdraw',ref=uid(1)),ask(3),normal(4)]
    s=snap(initial)
    duplicate=ask(1); duplicate=Record(duplicate.message,10,duplicate.received_at)
    result=fold(initial+[s,duplicate],use_snapshot=True)
    assert result.snapshot_used[P]==uid(9) and result.folded==2
    assert uid(1) in result.closed and 'duplicate_id' in codes(result)

def test_c17_snapshot_stale():
    initial=[ask(1),ask(3),normal(4)]; s=snap(initial)
    late=rec(2,'withdraw',ref=uid(1))
    result=fold([initial[0],late,*initial[1:],s],use_snapshot=True)
    assert result.snapshot_used[P] is None and 'snapshot_stale' in codes(result) and uid(1) in result.closed

def test_c19_cutoff():
    records=[ask(1),ask(2),ask(3),normal(4,100)]
    records[1]=Record(records[1].message,2,'2026-01-01T00:01:30+00:00')
    records[2]=Record(records[2].message,3,'2026-01-01T00:00:10+00:00')
    assert yaml.safe_load(snapshot_for(records,P))['through']==uid(1)
    assert snapshot_for([ask(1)],P) is None
    assert yaml.safe_load(snapshot_for([ask(1),normal(2,61)],P))['through']==uid(1)

def test_c20_import_graph():
    source=Path(__file__).parents[2]/'src/angareion/decision.py'
    tree=ast.parse(source.read_text())
    names=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    names += [x.name for n in ast.walk(tree) if isinstance(n,(ast.Import, ast.ImportFrom)) for x in n.names]
    assert not any(n.split('.')[-1] in {'backend','log_cache','cli','github'} for n in names)

@pytest.mark.parametrize('kind',['snapshot','none','bad'])
def test_c28_complete_defense(kind):
    records=[ask(1),submit(2,1,choice='A'),ask(3)]
    if kind=='snapshot': records += [normal(4),snap(records+[normal(4)])]
    if kind=='bad':
        s=snap(records+[normal(4)]); p=yaml.safe_load(s.message.content)
        p['through']=uid(99); s.message.content=yaml.safe_dump(p); records.append(s)
    partial=fold(records,use_snapshot=True); complete=fold(records,use_snapshot=False)
    assert not partial.complete and complete.complete
    if kind=='snapshot': assert partial.snapshot_used[P]==uid(9) and uid(1) in partial.closed
    else: assert partial.snapshot_used[P] is None and [x['id'] for x in partial.decisions]==[uid(3)]
    if kind=='bad': assert 'bad_snapshot' in codes(partial)
    entries=[dict(ref=uid(1),base=uid(1),choice='A'),dict(ref=uid(3),base=uid(3),choice='A')]
    with pytest.raises(ValueError) as error: plan_submit({CH:partial},H,entries,render=render)
    assert type(error.value) is ValueError
    plan=plan_submit({CH:complete},H,entries,render=render)
    assert plan.skipped==[uid(1)] and [x['ref'] for x in plan.batches[0][2]]==[uid(3)]

def test_c29_duplicate_and_base():
    result=fold([ask(1),submit(2,1,choice='A'),ask(3)],use_snapshot=False)
    same=dict(ref=uid(1),base=uid(1),choice='A',note=None)
    assert plan_submit({CH:result},H,[same],render=render).skipped==[uid(1)]
    with pytest.raises(SubmitRejected) as e: plan_submit({CH:result},H,[same,same],render=render)
    assert e.value.problems==[(uid(1),'duplicate_ref')]
    with pytest.raises(SubmitRejected) as e: plan_submit({CH:result},H,[dict(same,base=uid(8))],render=render)
    assert e.value.problems==[(uid(1),'closed_ref')]

@pytest.mark.parametrize('change', ['update','submit','withdraw','duplicate'])
def test_c16_prefix_interleaving(change):
    initial=[ask(1),update(2,1),ask(3)]
    if change=='update': tail=update(5,1)
    elif change=='submit': tail=submit(5,1,2,choice='A')
    elif change=='withdraw': tail=rec(5,'withdraw',ref=uid(1))
    else:
        u=update(2,1); tail=Record(u.message,5,u.received_at)
    records=initial+[tail,normal(6)]
    # Move the tail into the unstable window; through must be the unique marker.
    records[3]=Record(tail.message,5,'2026-01-01T00:01:50+00:00')
    s=snap(records)
    payload=yaml.safe_load(s.message.content)
    assert payload['through']==uid(3)
    q=next(x for x in payload['items'] if x['id']==uid(1))
    assert q['version']==uid(2) and q['revisions']==2 and set(q['versions'])=={uid(1),uid(2)}
    fast=fold(records+[s],use_snapshot=True); full=fold(records+[s],use_snapshot=False)
    assert fast.snapshot_used[P]==uid(9) and fast.folded==2
    assert fast.decisions==[x for x in full.decisions if x['status']=='open']
    assert fast.seen==full.seen and fast.closed==full.closed

def test_c29_atomic_preflight_and_chunking():
    result=fold([ask(i) for i in range(1,5)],use_snapshot=False)
    entries=[dict(ref=uid(i),base=uid(i),note='x'*1000) for i in range(1,5)]
    plan=plan_submit({CH:result},H,entries,render=lambda ch,p,items:'x'*(len(items)*33000))
    assert [len(batch[2]) for batch in plan.batches]==[1,1,1,1]
    with pytest.raises(SubmitRejected) as e:
        plan_submit({CH:result},H,[entries[0],dict(entries[1],base=uid(99)),entries[0]],render=render)
    assert e.value.problems==[(uid(2),'stale_base'),(uid(1),'duplicate_ref')]

def test_c27_submit_note_boundaries():
    for note, accepted in [('',False),('x'*4001,False),('x'*4000,True),('😀',True)]:
        result=fold([ask(1),submit(2,1,note=note,choice=None)],use_snapshot=False)
        assert (result.decisions[0]['status']=='answered') is accepted
        if not accepted: assert codes(result)==['bad_item']

@pytest.mark.parametrize('kind',['ask','update','submit'])
def test_c26_replayed_record_after_snapshot(kind):
    q=ask(1)
    if kind=='ask':
        original=q; prefix=[q,rec(2,'withdraw',ref=uid(1)),ask(3)]; expected='duplicate_id'
    elif kind=='update':
        original=update(2,1); prefix=[q,original,update(3,1),ask(4)]; expected='duplicate_id'
    else:
        original=submit(2,1,choice='A'); prefix=[q,original,ask(3)]; expected='closed_ref'
    marker=normal(7)
    s=snap(prefix+[marker],9)
    replay=Record(original.message,10,original.received_at)
    records=prefix+[marker,s,replay]
    fast=fold(records,use_snapshot=True); full=fold(records,use_snapshot=False)
    assert fast.snapshot_used[P]==uid(9) and fast.folded==2
    assert fast.decisions==[x for x in full.decisions if x['status']=='open']
    assert fast.seen==full.seen and fast.closed==full.closed and expected in codes(fast)
    if kind=='ask': assert next(x for x in full.decisions if x['id']==uid(1))['status']=='withdrawn'
    if kind=='submit': assert next(x for x in full.decisions if x['id']==uid(1))['submitted']['choice']=='A'
    if kind=='update': assert next(x for x in full.decisions if x['id']==uid(1))['version']==uid(3)

def test_c17_bad_snapshot_falls_back_to_previous():
    prefix=[ask(1),ask(2),normal(3)]
    s=snap(prefix,4)
    bad=snap(prefix,5)
    p=yaml.safe_load(bad.message.content); p['through']=uid(99); bad.message.content=yaml.safe_dump(p)
    fast=fold(prefix+[s,bad],use_snapshot=True)
    assert fast.snapshot_used[P]==uid(4) and 'bad_snapshot' in codes(fast)
    assert [q['id'] for q in fast.decisions]==[uid(1),uid(2)]

def test_c16_seeded_snapshot_equivalence():
    import random
    rng=random.Random(468)
    for run in range(20):
        prefix=[ask(1),ask(2),update(3,1),ask(4)]
        actions=[update(10,1),submit(11,1,3,choice='A'),rec(12,'withdraw',ref=uid(2)),ask(13)]
        rng.shuffle(actions)
        tail=[Record(x.message,20+i,'2026-01-01T00:01:50+00:00') for i,x in enumerate(actions)]
        records=prefix+tail+[normal(30)]
        s=snap(records,40)
        fast=fold(records+[s],use_snapshot=True); full=fold(records+[s],use_snapshot=False)
        assert fast.snapshot_used[P]==uid(40) and fast.folded==len(tail)+1
        assert fast.decisions==[x for x in full.decisions if x['status']=='open']
        assert fast.seen==full.seen and fast.closed==full.closed
        assert not fast.complete and full.complete

def test_c11_withdraw_submit_order():
    w=rec(2,'withdraw',ref=uid(1)); s=submit(3,1,choice='A')
    result=fold([ask(1),w,s],use_snapshot=False)
    assert result.decisions[0]['status']=='withdrawn' and codes(result)==['closed_ref']
    result=fold([ask(1),Record(s.message,2,s.received_at),Record(w.message,3,w.received_at)],use_snapshot=False)
    assert result.decisions[0]['status']=='answered' and codes(result)==['closed_ref']

def test_c16_old_bad_record_is_not_replayed():
    bad=ask(1); bad.message.content='broken: ['
    records=[bad,ask(2),normal(3)]
    s=snap(records,4)
    result=fold(records+[s],use_snapshot=True)
    assert result.snapshot_used[P]==uid(4) and result.folded==1 and result.ignored==[]

@pytest.mark.parametrize('change',['update','submit','withdraw','duplicate'])
def test_c16_change_after_snapshot(change):
    initial=[ask(1),update(2,1),ask(3),normal(4)]
    s=snap(initial,9)
    if change=='update': tail=update(10,1)
    elif change=='submit': tail=submit(10,1,2,choice='A')
    elif change=='withdraw': tail=rec(10,'withdraw',ref=uid(1))
    else:
        u=update(2,1); tail=Record(u.message,10,u.received_at)
    records=initial+[s,tail]
    fast=fold(records,use_snapshot=True); full=fold(records,use_snapshot=False)
    assert yaml.safe_load(s.message.content)['through']==uid(3)
    assert fast.snapshot_used[P]==uid(9) and fast.folded==2
    assert fast.decisions==[q for q in full.decisions if q['status']=='open']
    assert fast.seen==full.seen and fast.closed==full.closed

def test_c29_note_only_skip_normalization():
    result=fold([ask(1),submit(2,1,note='x')],use_snapshot=False)
    same={'ref':uid(1),'base':uid(1),'choice':None,'note':'x'}
    assert plan_submit({CH:result},H,[same],render=render).skipped==[uid(1)]
    with pytest.raises(SubmitRejected) as exc:
        plan_submit({CH:result},H,[dict(same,note='y')],render=render)
    assert exc.value.problems==[(uid(1),'closed_ref')]


@pytest.mark.parametrize("content", [
    "[" * 600 + "]" * 600,
    "{a: " * 600 + "0" + "}" * 600,
    "[" * 600,
    "x: " + "[" * 600 + "]" * 600,
    "x: " + "9" * 6000,
])
def test_bad_deep_payload_does_not_abort_fold(content):
    bad = ask(2)
    bad.message.content = content
    for use_snapshot in (False, True):
        result = fold([ask(1), bad], use_snapshot=use_snapshot)
        assert [q['id'] for q in result.decisions] == [uid(1)]
        assert codes(result) == ['bad_payload']
    assert snapshot_for([ask(1), bad, normal(3)], P) is not None


def test_submit_title_is_not_used_by_fold():
    record = submit(2, 1, choice='A')
    payload = yaml.safe_load(record.message.content)
    del payload['items'][0]['title']
    record.message.content = yaml.safe_dump(payload)
    result = fold([ask(1), record], use_snapshot=False)
    assert result.decisions[0]['status'] == 'answered'
    assert not result.ignored
    with pytest.raises(MessageValidationError):
        build_payload('submit', asker=P, items=payload['items'])


@pytest.mark.parametrize('fault', ['to', 'mentions', 'yaml', 'op'])
def test_c15_whole_bad_payload(fault):
    record = submit(2, 1, choice='A')
    if fault == 'to':
        record.message.to = H2
    elif fault == 'mentions':
        record.message.mentions = []
    elif fault == 'yaml':
        record.message.content = '[bad'
    else:
        record.message.content = 'decision: 1\nop: unknown\n'
    result = fold([ask(1), record], use_snapshot=False)
    assert result.decisions[0]['status'] == 'open'
    assert len(result.ignored) == 1
    assert result.ignored[0]['code'] == 'bad_payload'
    assert result.ignored[0]['item'] is None


def test_nel_title_read_back_differently_is_bad_payload_not_crash():
    # PyYAML writes '\x85' inside a single-quoted scalar as whitespace/newline, so the
    # ask reads back with title '\n' (invalid).  A snapshot item with the same title
    # must not turn that into an exception on any path.
    bad_ask = ask(1, title='\x85\x85')
    content = yaml.safe_dump(dict(
        decision=1, op='snapshot', asker=P, through=uid(99), covered=1,
        items=[dict(id=uid(1), version=uid(1), revisions=1, versions=[uid(1)],
                    asked_at='2026-01-01T00:00:00+00:00', updated_at='2026-01-01T00:00:00+00:00',
                    answerer=H, title='\x85\x85', background='', body='內容',
                    options=[{'key': 'A', 'text': '甲'}, {'key': 'B', 'text': '乙'}],
                    recommended='A', reason='')],
        closed=[]), allow_unicode=False, sort_keys=False)
    snapshot = Record(Message(from_=P, to=P, channel=CH, id=uid(3), content=content, datatype='decision'),
                      3, '2026-01-01T00:03:00+00:00')
    records = [bad_ask, normal(2), snapshot]
    for use_snapshot in (True, False):
        result = fold(records, use_snapshot=use_snapshot)
        assert result.decisions == []
        assert (uid(1), 'bad_payload') in [(x['record'], x['code']) for x in result.ignored]
    assert snapshot_for(records, P) is not None


def test_c15_binding_precedes_stale_and_empty_answer():
    record = submit(3, 1, base=99, choice='A')
    payload = yaml.safe_load(record.message.content)
    payload['asker'] = H2
    record.message.to = H2
    record.message.mentions = [H2]
    record.message.content = yaml.safe_dump(payload)
    assert codes(fold([ask(1), record], use_snapshot=False)) == ['binding_mismatch']
    assert codes(fold([ask(1), submit(2, 1)], use_snapshot=False)) == ['bad_item']


def test_c19_exact_stability_boundary_and_duplicate_through():
    assert yaml.safe_load(snapshot_for([ask(1), normal(2, 60)], P))['through'] == uid(1)
    duplicate = Record(ask(1).message, 2, ask(1).received_at)
    assert snapshot_for([ask(1), duplicate, normal(3)], P) is None


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'self', 'from', 'to', 'yaml'])
def test_c17_invalid_snapshot_cases(fault):
    initial = [ask(1), ask(2), normal(3)]
    s = snap(initial, 4)
    payload = yaml.safe_load(s.message.content)
    if fault == 'missing':
        payload['through'] = uid(99)
    elif fault == 'duplicate':
        initial.insert(2, Record(ask(2).message, 2.5, ask(2).received_at))
    elif fault == 'self':
        payload['through'] = uid(4)
    elif fault == 'from':
        s.message.from_ = H
    elif fault == 'to':
        s.message.to = H
    if fault == 'yaml':
        s.message.content = 'op: snapshot\nitems: ['
    else:
        s.message.content = yaml.safe_dump(payload, sort_keys=False)
    result = fold(initial + [s], use_snapshot=True)
    assert result.snapshot_used[P] is None
    assert 'bad_snapshot' in codes(result)
    full = fold(initial + [s], use_snapshot=False)
    assert result.decisions == [q for q in full.decisions if q['status'] == 'open']
    assert result.seen == full.seen and result.closed == full.closed and not result.complete


def test_c16_duplicate_update_through_fallback():
    u = update(2, 1)
    initial = [ask(1), u, normal(3)]
    s = snap(initial, 5)
    again = Record(u.message, 4, u.received_at)
    result = fold(initial + [again, s], use_snapshot=True)
    full = fold(initial + [again, s], use_snapshot=False)
    assert result.snapshot_used[P] is None and 'bad_snapshot' in codes(result)
    assert result.decisions == [q for q in full.decisions if q['status'] == 'open']
    assert result.seen == full.seen and result.closed == full.closed and not result.complete


def test_multi_asker_snapshot_filters_items_and_closed():
    p2 = 'mystore-pm-w2'
    other_open = rec(2, 'ask', sender=p2, **question())
    other_closed = rec(3, 'ask', sender=p2, **question())
    records = [ask(1), other_open, other_closed, rec(4, 'withdraw', sender=p2, ref=uid(3)), normal(5)]
    payload = yaml.safe_load(snapshot_for(records, P))
    assert [q['id'] for q in payload['items']] == [uid(1)]
    assert payload['closed'] == []
    payload2 = yaml.safe_load(snapshot_for(records, p2))
    assert [q['id'] for q in payload2['items']] == [uid(2)]
    assert payload2['closed'] == [uid(3)]


def test_c26_ask_replay_after_submit_and_distinct_duplicate_content():
    initial = [ask(1), submit(2, 1, choice='A'), ask(3), normal(4)]
    s = snap(initial, 5)
    replay = Record(ask(1).message, 6, ask(1).received_at)
    result = fold(initial + [s, replay], use_snapshot=True)
    assert 'duplicate_id' in codes(result) and uid(1) in result.closed
    changed = ask(1, title='changed')
    changed = Record(changed.message, 2, changed.received_at)
    full = fold([ask(1), changed], use_snapshot=False)
    assert full.decisions[0]['title'] == '題 😀' and codes(full) == ['duplicate_id']


def test_c26_withdraw_before_ask_then_replay_after_snapshot():
    withdrawal = rec(1, 'withdraw', ref=uid(2), reason='done')
    initial = [withdrawal, ask(2), ask(3), normal(4)]
    s = snap(initial, 5)
    replay = Record(withdrawal.message, 6, withdrawal.received_at)
    fast = fold(initial + [s, replay], use_snapshot=True)
    full = fold(initial + [s, replay], use_snapshot=False)
    assert uid(2) in fast.closed and fast.closed == full.closed
    assert next(q for q in full.decisions if q['id'] == uid(2))['withdrawn']['reason'] == 'done'


def test_c28_c29_closed_and_rerun_details():
    result = fold([ask(1), submit(2, 1, choice='A'), ask(3), rec(4, 'withdraw', ref=uid(3)),
                   ask(5), update(6, 5), submit(7, 5, 6, choice='B')], use_snapshot=False)
    assert result.decisions[0]['submitted']['submitted_at'] == '2026-01-01T00:00:00+00:00'
    for answerer, entry in [(H, dict(ref=uid(3),base=uid(3),choice='A')),
                            (H, dict(ref=uid(1),base=uid(1),choice='B')),
                            (H2, dict(ref=uid(1),base=uid(1),choice='A')),
                            (H, dict(ref=uid(5),base=uid(5),choice='B'))]:
        with pytest.raises(SubmitRejected) as exc:
            plan_submit({CH:result}, answerer, [entry, dict(ref=uid(99),base=uid(99),choice='A')], render=render)
        assert exc.value.problems[0][1] == 'closed_ref'
    assert plan_submit({CH:result}, H, [dict(ref=uid(5),base=uid(6),choice='B')], render=render).skipped == [uid(5)]
    assert plan_submit({CH:result}, H, [dict(ref=uid(1),base=uid(1),choice='A')], render=render).skipped == [uid(1)]


def test_ordering_and_message_length_boundary():
    first = Record(ask(1).message, 1, '2026-01-01T00:00:10+00:00')
    second = Record(ask(2).message, 2, '2026-01-01T00:00:00+00:00')
    result = fold([first, second], use_snapshot=False)
    assert [q['id'] for q in result.decisions] == [uid(2), uid(1)]
    entries = [dict(ref=uid(i), base=uid(i), choice='A') for i in (1,2)]
    plan = plan_submit({CH:result}, H, entries, render=lambda *args: 'x'*65536)
    assert [i['ref'] for i in plan.batches[0][2]] == [uid(2), uid(1)]
    with pytest.raises(MessageValidationError):
        plan_submit({CH:result}, H, entries[:1], render=lambda *args: 'x'*65537)


def test_snapshot_preserves_forged_tombstone_in_next_projection():
    initial = [ask(1), normal(2)]
    s = snap(initial, 3)
    payload = yaml.safe_load(s.message.content)
    payload['closed'] = [uid(90)]
    s.message.content = yaml.safe_dump(payload, sort_keys=False)
    initial[1] = normal(2, 0)
    s = Record(s.message, 3, '2026-01-01T00:00:00+00:00')
    next_payload = yaml.safe_load(snapshot_for(initial + [s, normal(4)], P))
    assert uid(90) in next_payload['closed']


def test_seeded_multi_asker_multiple_snapshots():
    import random
    p2 = 'mystore-pm-w2'
    for seed in range(20):
        rng = random.Random(seed)
        records = [ask(1), rec(2, 'ask', sender=p2, **question()), ask(3)]
        for i in range(4, 14):
            kind = rng.choice(['update', 'withdraw', 'bad', 'ask', 'submit'])
            if kind == 'update':
                r = update(i, rng.choice([1, 2, 3]))
            elif kind == 'withdraw':
                r = rec(i, 'withdraw', sender=rng.choice([P,p2,H]), ref=uid(rng.choice([1,2,3])))
            elif kind == 'bad':
                r = ask(i); r.message.content = '[broken'
            elif kind == 'ask':
                r = rec(i, 'ask', sender=rng.choice([P,p2]), **question())
            else:
                r = submit(i, rng.choice([1,2,3]), choice=rng.choice(['A','Z']))
            records.append(r)
        records.append(normal(14))
        for asker, number in [(P,15),(p2,16),(P,17),(p2,18)]:
            content = snapshot_for(records + [normal(30)], asker)
            if content:
                m = Message(from_=asker,to=asker,channel=CH,id=uid(number),content=content,datatype='decision')
                records.append(Record(m, number, '2026-01-01T00:00:00+00:00'))
        fast = fold(records, use_snapshot=True)
        full = fold(records, use_snapshot=False)
        assert fast.decisions == [q for q in full.decisions if q['status']=='open']
        assert fast.seen == full.seen and fast.closed == full.closed
        assert not fast.complete and full.complete


def test_equal_time_sorting_by_channel_then_id_and_batch_by_ref():
    a, b, c = ask(3), ask(2), ask(1)
    b.message.channel = 'team-other'
    body = yaml.safe_load(b.message.content)
    body['ask_channel'] = 'team-other'
    b.message.content = yaml.safe_dump(body)
    result = fold([a, b, c], use_snapshot=False)
    assert [(q['channel'], q['id']) for q in result.decisions] == [
        ('team-other', uid(2)), (CH, uid(1)), (CH, uid(3))
    ]
    entries = [dict(ref=uid(n), base=uid(n), choice='A') for n in (3, 1)]
    plan = plan_submit({CH: result}, H, entries, render=render)
    assert [item['ref'] for item in plan.batches[0][2]] == [uid(1), uid(3)]



def options(n):
    return [{'key': chr(65 + i), 'text': f'選{i}'} for i in range(n)]


def test_option_count_boundary():
    assert build_payload('ask', ask_channel=CH, asker=P, **question(options=options(9)))
    with pytest.raises(MessageValidationError):
        build_payload('ask', ask_channel=CH, asker=P, **question(options=options(10)))
    m = ask(1).message
    m.content = yaml.safe_dump(dict(decision=1, op='ask', ask_channel=CH, asker=P,
                                    **question(options=options(10))), allow_unicode=True)
    with pytest.raises(PayloadError):
        parse_payload(m)


def test_title_length_boundary():
    assert build_payload('ask', ask_channel=CH, asker=P, **question(title='題' * 120))
    with pytest.raises(MessageValidationError):
        build_payload('ask', ask_channel=CH, asker=P, **question(title='題' * 121))
    m = ask(1).message
    m.content = yaml.safe_dump(dict(decision=1, op='ask', ask_channel=CH, asker=P,
                                    **question(title='題' * 121)), allow_unicode=True)
    with pytest.raises(PayloadError):
        parse_payload(m)
    m.content = yaml.safe_dump(dict(decision=1, op='ask', ask_channel=CH, asker=P,
                                    **question(title='題' * 120)), allow_unicode=True)
    assert parse_payload(m)['title'] == '題' * 120


def test_ask_channel_must_match_message_channel():
    r = ask(1)
    body = yaml.safe_load(r.message.content)
    body['ask_channel'] = 'team-other'
    r.message.content = yaml.safe_dump(body, allow_unicode=True, sort_keys=False)
    result = fold([r], use_snapshot=False)
    assert codes(result) == ['binding_mismatch'] and result.decisions == []


def test_snapshot_item_versions_must_match_revisions():
    initial = [ask(1), normal(2)]
    s = snap(initial, 3)
    payload = yaml.safe_load(s.message.content)
    payload['items'][0]['revisions'] = 2
    s.message.content = yaml.safe_dump(payload, sort_keys=False)
    result = fold(initial + [s], use_snapshot=True)
    assert result.snapshot_used[P] is None and 'bad_snapshot' in codes(result)
    assert [q['id'] for q in result.decisions] == [uid(1)]


def test_plan_submit_rejects_unknown_entry_key():
    result = fold([ask(1)], use_snapshot=False)
    with pytest.raises(SubmitRejected) as exc:
        plan_submit({CH: result}, H, [dict(ref=uid(1), base=uid(1), choice='A', title='x')], render=render)
    assert exc.value.problems == [(uid(1), 'bad_item')]


def test_snapshot_for_rejects_naive_timestamps():
    records = [Record(ask(1).message, 1, '2026-01-01T00:00:00'),
               Record(normal(2, 300).message, 2, '2026-01-01T00:05:00')]
    assert snapshot_for(records, P) is None


def test_recommended_must_be_an_option():
    with pytest.raises(MessageValidationError):
        build_payload('ask', ask_channel=CH, asker=P, **question(recommended='Z'))
    m = ask(1).message
    m.content = yaml.safe_dump(dict(decision=1, op='ask', ask_channel=CH, asker=P,
                                    **question(recommended='Z')), allow_unicode=True)
    with pytest.raises(PayloadError):
        parse_payload(m)
    assert codes(fold([Record(m, 1, '2026-01-01T00:00:00+00:00')], use_snapshot=False)) == ['bad_payload']


def test_bad_record_between_two_askers_cutoffs_is_diagnosed():
    # P's snapshot covers up to pos 1, p2's up to pos 3; the bad record at pos 2 lies
    # between the two cutoffs, so the skip rule (position <= min cutoff) must not hide it.
    p2 = 'mystore-pm-w2'
    r1 = ask(1)
    bad = ask(2)
    bad.message.content = '[broken'
    r3 = rec(3, 'ask', sender=p2, **question())
    records_for_p = [r1, normal(9, 120)]
    records_for_p2 = [r1, bad, r3, normal(9, 200)]
    sp = Message(from_=P, to=P, channel=CH, id=uid(4), datatype='decision',
                 content=snapshot_for(records_for_p, P))
    s2 = Message(from_=p2, to=p2, channel=CH, id=uid(5), datatype='decision',
                 content=snapshot_for(records_for_p2, p2))
    records = [r1, bad, r3, Record(sp, 4, '2026-01-01T00:03:00+00:00'),
               Record(s2, 5, '2026-01-01T00:03:00+00:00')]
    result = fold(records, use_snapshot=True)
    assert result.snapshot_used == {P: uid(4), p2: uid(5)}
    assert (uid(2), 'bad_payload') in [(x['record'], x['code']) for x in result.ignored]


def test_withdraw_reason_must_be_a_string():
    with pytest.raises(MessageValidationError):
        build_payload('withdraw', ref=uid(1), reason=5)
    w = rec(2, 'withdraw', ref=uid(1), reason=5)
    result = fold([ask(1), w], use_snapshot=False)
    assert codes(result) == ['bad_payload'] and result.decisions[0]['status'] == 'open'


def test_record_after_cutoff_is_parsed_and_applied():
    initial = [ask(1), normal(2)]
    s = snap(initial, 3)
    result = fold(initial + [s, ask(5)], use_snapshot=True)
    assert result.snapshot_used[P] == uid(3)
    assert [q['id'] for q in result.decisions] == [uid(1), uid(5)]
    assert result.folded == 2


def _same_id_before_snapshot(earlier):
    # `earlier` shares id uid(5) with the snapshot after it (first-come wins, design §6.4).
    # The snapshot's `through` is ask 1; ask 7 is the last unique decision, so snapshot_for uses it.
    return [earlier,
            rec(1, 'ask', position=2, **question()),
            rec(5, 'snapshot', position=3, asker=P, through=uid(1), covered=2, items=[], closed=[uid(90)]),
            rec(7, 'ask', position=4, **question()),
            normal(9, 300)]


@pytest.mark.parametrize('kind', ['legal_ask', 'broken'])
def test_snapshot_for_survives_same_id_earlier_record(kind):
    earlier = rec(5, 'ask', position=1, **question())
    if kind == 'broken':
        earlier.message.content = '[broken'
    records = _same_id_before_snapshot(earlier)
    result = fold(records, use_snapshot=True)
    assert result.snapshot_used == {P: uid(5)}
    assert [q['id'] for q in result.decisions] == [uid(7)]
    out = yaml.safe_load(snapshot_for(records, P))
    assert out['through'] == uid(7) and out['covered'] == 4
    assert [i['id'] for i in out['items']] == [uid(7)]
    assert out['closed'] == [uid(90)]


def test_related_count_counts_own_asks_and_owned_updates():
    records = [rec(1, 'ask', sender=H2, **question()), update(2, 1), ask(3)]
    assert related_count(records, P) == 1
    assert related_count(records, H2) == 2


def test_related_count_submit_item_counts_only_when_ref_is_mine():
    records = [ask(1), submit(5, 1), rec(7, 'submit', sender=H, asker=P,
                                         items=[dict(ref=uid(99), base=uid(99), title='t')])]
    assert related_count(records, P) == 2


def test_related_count_skips_non_string_submit_ref_without_raising():
    records = [ask(1), rec(8, 'submit', sender=H, asker=P, items=[dict(ref=[1], base=uid(1))]),
               rec(9, 'submit', sender=H, asker=P, items=['not a mapping', dict(ref=None, base=uid(1))])]
    assert related_count(records, P) == 1


@pytest.mark.parametrize('content', ['[broken', "decision: 2\nop: ask\n"])
def test_related_count_skips_unreadable_payloads_without_raising(content):
    bad = ask(2)
    bad.message.content = content
    assert related_count([ask(1), bad], P) == 1


def test_related_count_starts_after_the_current_snapshot():
    initial = [ask(1), ask(3), normal(4)]
    records = initial + [snap(initial), ask(10)]
    assert related_count([ask(1), ask(3), ask(10)], P) == 3
    assert related_count(records, P) == 1
    assert related_count(initial + [snap(initial)], P) == 0


# `fold` (the log's `_item_code`) and `plan_submit` (the CLI preflight) judge one submit item
# by two chains. They must give the same first code for every combination below, except the
# non-ULID ref, which DIVERGES names on both sides (design 482 §3). The `answered` fixture
# commits choice B, not A: with the same answerer, base, choice and no note, plan_submit would
# skip the item as a re-run, and fold would say closed_ref. That case is not in this matrix.
CONSISTENCY_STATES = {
    'open': lambda: [ask(1)],
    'answered': lambda: [ask(1), submit(2, 1, choice='B')],
    'withdrawn': lambda: [ask(1), rec(2, 'withdraw', ref=uid(1))],
}
CONSISTENCY_REFS = {'correct': uid(1), 'nonexistent': uid(50), 'nonulid': 'not-a-ulid'}
CONSISTENCY_SENDERS = {'answerer': H, 'other': H2}
CONSISTENCY_BASES = {'correct': uid(1), 'stale': uid(7)}
CONSISTENCY_CHOICES = {'valid': 'A', 'invalid': 'C'}
DIVERGES = {'nonulid': ('bad_item', 'unknown_ref')}  # (fold, plan_submit)

def consistency_expected(state, ref, sender, base, choice):
    """The (fold, plan_submit) first codes for one item; None when it is accepted."""
    if ref == 'nonulid':
        return DIVERGES['nonulid']
    if ref == 'nonexistent':
        return 'unknown_ref', 'unknown_ref'
    if state != 'open':
        return 'closed_ref', 'closed_ref'
    if sender == 'other':
        code = 'not_answerer'
    elif base == 'stale':
        code = 'stale_base'
    elif choice == 'invalid':
        code = 'bad_item'
    else:
        code = None
    return code, code

@pytest.mark.parametrize('state,ref,sender,base,choice', [
    (state, ref, sender, base, choice)
    for state in CONSISTENCY_STATES for ref in CONSISTENCY_REFS
    for sender in CONSISTENCY_SENDERS for base in CONSISTENCY_BASES
    for choice in CONSISTENCY_CHOICES])
def test_consistency_fold_and_plan_agree(state, ref, sender, base, choice):
    fold_code, plan_code = consistency_expected(state, ref, sender, base, choice)
    entry = dict(ref=CONSISTENCY_REFS[ref], base=CONSISTENCY_BASES[base], choice=CONSISTENCY_CHOICES[choice])
    before = fold(CONSISTENCY_STATES[state](), use_snapshot=False)
    after = fold(CONSISTENCY_STATES[state]() + [rec(10, 'submit', sender=CONSISTENCY_SENDERS[sender], asker=P, items=[entry])], use_snapshot=False)
    assert codes(after) == ([fold_code] if fold_code else [])
    try:
        plan = plan_submit({CH: before}, CONSISTENCY_SENDERS[sender], [entry], render=render)
    except SubmitRejected as e:
        assert [x for x, _ in e.problems] == [CONSISTENCY_REFS[ref]]
        assert [code for _, code in e.problems] == [plan_code]
    else:
        assert plan_code is None and plan.skipped == [] and len(plan.batches) == 1

@pytest.mark.parametrize('state,expected', [
    ('open', ['duplicate_ref']),
    ('answered', ['closed_ref', 'duplicate_ref']),
    ('withdrawn', ['closed_ref', 'duplicate_ref']),
])
def test_consistency_duplicate_ref(state, expected):
    entry = dict(ref=uid(1), base=uid(1), choice='A')
    before = fold(CONSISTENCY_STATES[state](), use_snapshot=False)
    after = fold(CONSISTENCY_STATES[state]() + [rec(10, 'submit', sender=H, asker=P, items=[entry, entry])], use_snapshot=False)
    assert codes(after) == expected
    with pytest.raises(SubmitRejected) as e:
        plan_submit({CH: before}, H, [entry, entry], render=render)
    assert [code for _, code in e.value.problems] == expected
