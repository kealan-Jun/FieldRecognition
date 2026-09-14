import copy

from panel_confirmation import PanelConfirmation


def region(text='0.0000',instrument='B',slot='0',box=None):
    return {'instrument_id':instrument,'panel_id':slot,'bbox':box or [0,0,100,40],
            'local_ocr':{'lines':[{'text':text,'panel_id':slot}] if text else []}}


def observe(voter,n,regions,scope='session1',now=None):
    return voter.observe(regions,(n,),str(n),n*.5 if now is None else now,scope)


def test_two_of_three_tolerates_one_wrong_frame_and_requires_current_value():
    v=PanelConfirmation()
    assert not observe(v,0,[region()])
    assert not observe(v,1,[region('8.0000')])
    r=region();lines=observe(v,2,[r])
    assert lines[0]['text']=='0.0000' and lines[0]['temporal_confirmation']['votes']==2
    assert v.changed(r)
    v.mark_saved([r]);assert not v.changed(r)
    assert not observe(v,3,[region('8.8888')])  # Never show an old majority on a new value.


def test_same_frame_never_counts_twice_and_expired_votes_are_discarded():
    v=PanelConfirmation()
    for t in (0,.2,.4):
        assert not observe(v,1,[region()],now=t)
    assert not observe(v,2,[region()],now=2)
    assert observe(v,3,[region()],now=2.5)


def test_each_instrument_and_each_window_votes_independently():
    v=PanelConfirmation()
    first=[region('100','A','0'),region('200','A','1'),region('0.0000','B','0')]
    assert not observe(v,0,copy.deepcopy(first))
    second=copy.deepcopy(first);second[0]['local_ocr']['lines'][0]['text']='999'
    lines=observe(v,1,second)
    assert [l['text'] for l in lines]==['200','0.0000']
    assert not v.changed(second[0]) and v.changed(second[1]) and v.changed(second[2])


def test_new_binding_scope_absent_panel_or_large_motion_cannot_reuse_votes():
    for interrupt in ('scope','absent','motion'):
        v=PanelConfirmation();observe(v,0,[region()])
        if interrupt=='scope':
            assert not observe(v,1,[region()],scope='session2')
        elif interrupt=='absent':
            observe(v,1,[]);assert not observe(v,2,[region()])
        else:
            assert not observe(v,1,[region(box=[200,200,300,240])])
