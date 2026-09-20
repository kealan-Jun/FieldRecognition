import copy
import numpy as np
from lcd_digits import refine_lcd, _wide_fixed_decimal_retry
from test_digit_regions import line

RULE={'measurement_ranges':{'质量':{'decimal_places':4,'fixed_decimal_display':True}}}


def test_fixed_decimal_wide_retry_recovers_leading_glyph_with_two_views():
    panel=np.zeros((131,187,3),np.uint8)
    result={'lines':[], 'digit_region':{
        'status':'located', 'polygon':[[184,249],[269,249],[269,281],[184,281]],
        'initial_text':'1SS8'}}
    replies=iter((
        {'lines':[line('11698',[12,24,300,80],.62)]},
        {'lines':[line('11698',[12,24,300,80],.65)]},
        {'lines':[line('1698',[12,24,300,80],.85)]},
    ))
    out=_wide_fixed_decimal_retry(lambda *args: next(replies), panel, result, 100, 200, 4)
    assert out['lines'][0]['text']=='11698'
    assert out['lines'][0]['reading_source']=='fixed_decimal_wide_consensus'
    assert out['digit_consistency']['supporting_views']==2
    assert out['digit_region']['method']=='fixed_decimal_wide_consensus_v1'
    assert len(out['fixed_decimal_wide_ocr']['attempts'])==3


def test_fixed_decimal_wide_retry_does_not_promote_single_candidate():
    panel=np.zeros((131,187,3),np.uint8)
    result={'lines':[], 'digit_region':{
        'status':'located', 'polygon':[[184,249],[269,249],[269,281],[184,281]],
        'initial_text':'1SS8'}}
    replies=iter((
        {'lines':[line('11698',[12,24,300,80],.62)]},
        {'lines':[line('1698',[12,24,300,80],.85)]},
        {'lines':[line('116S',[12,24,300,80],.71)]},
    ))
    out=_wide_fixed_decimal_retry(lambda *args: next(replies), panel, result, 100, 200, 4)
    assert out['lines']==[]


def test_fixed_decimal_wide_retry_uses_numeric_view_to_resolve_lcd_s_alias():
    panel=np.zeros((131,187,3),np.uint8)
    result={'lines':[], 'digit_region':{
        'status':'located', 'polygon':[[184,249],[269,249],[269,281],[184,281]],
        'initial_text':'1SS8'}}
    replies=iter((
        {'lines':[line('116S8',[12,24,300,80],.62)]},
        {'lines':[line('116S8',[12,24,300,80],.65)]},
        {'lines':[line('11698',[12,24,300,80],.55)]},
    ))
    out=_wide_fixed_decimal_retry(lambda *args: next(replies), panel, result, 100, 200, 4)
    assert out['lines'][0]['text']=='11698'
    assert out['digit_consistency']['exact_numeric_views']==1
    assert out['digit_consistency']['normalized_text']=='11698'


def test_lcd_enlargement_keeps_raw_source_and_maps_digit_evidence():
    initial={'status':'completed','lines':[line('115071',[30,30,70,20])]}
    calls=[]
    def predict(im,x,y):
        calls.append(im.shape)
        return {'status':'completed','lines':[line('1115071',[20,20,120,40])]}
    out=refine_lcd(predict,np.zeros((100,160,3),np.uint8),initial,400,500,instrument=RULE)
    assert out['lines'][0]['text']=='1115071'
    assert out['panel_ocr']==initial and len(calls)==2
    assert calls[0][:2]==(200,320)
    assert min(p[0] for p in out['lines'][0]['polygon'])>=400


def test_lcd_contrast_needs_agreement_with_source_not_a_guessed_number(monkeypatch):
    import lcd_digits
    monkeypatch.setattr(lcd_digits,'refine_digits',lambda *a,**kw:{'lines':[line('02',[0,0,20,20])]})
    initial={'status':'completed','lines':[line('02129',[430,530,70,20])]}
    replies=iter([{'status':'completed','lines':[line('02:29',[60,60,140,40])]},
                  {'status':'completed','lines':[line('02129',[90,90,210,60])]}])
    out=refine_lcd(lambda *a:next(replies),np.zeros((100,160,3),np.uint8),initial,400,500,instrument=RULE)
    assert out['lines'][0]['text']=='02129'
    assert out['lines'][0]['polygon'][0]==[430,530]
    assert out['digit_consistency']['status']=='resolved'
    # Same enhanced text alone cannot turn a washed-out photo into a reading.
    empty={'status':'completed','lines':[]};responses=iter([empty,{'lines':[line('02129',[90,90,210,60])]}])
    out=refine_lcd(lambda *a:next(responses),np.zeros((100,160,3),np.uint8),empty,instrument=RULE)
    assert not out['lines'] and out['digit_consistency']['status']=='unresolved'


def test_readable_unit_does_not_trigger_an_unnecessary_contrast_retry(monkeypatch):
    import lcd_digits
    result={'lines':[line('32.4223 g',[10,10,60,20])]}
    monkeypatch.setattr(lcd_digits,'refine_digits',lambda *a,**kw:copy.deepcopy(result))
    calls=[]
    out=refine_lcd(lambda *a:calls.append(1) or {'lines':[]},np.zeros((100,160,3),np.uint8),{'status':'completed'},instrument=RULE)
    assert len(calls)==1 and out['lines'][0]['text']=='32.4223 g'
