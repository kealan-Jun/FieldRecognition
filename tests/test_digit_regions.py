import json
import numpy as np
import pytest

from digit_regions import refine_digits


def line(text,box,confidence=.9):
    x,y,w,h=box
    return {'text':text,'confidence':confidence,'polygon':[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]}


def test_main_digits_exclude_menu_number_and_map_polygons_to_original():
    raw={'lines':[line('1',[15,12,5,6]),line('0.0000',[30,35,60,20]),line('MENU',[15,20,25,8])],
         'actual_model_invocation':True}
    calls=[]
    def predict(image,x,y):
        calls.append(image.shape)
        return {'lines':[line('0.0000',[0,0,image.shape[1]-1,image.shape[0]-1])]}
    result=refine_digits(predict,np.zeros((100,120,3),np.uint8),raw)
    assert [l['text'] for l in result['lines']]==['0.0000']
    assert result['panel_ocr']==raw
    assert len(calls)==1 and calls[0][0]>=64
    assert np.allclose(result['lines'][0]['polygon'],result['digit_region']['polygon'],atol=.001)
    assert result['digit_region']['initial_text']=='0.0000'


def test_missing_decimal_is_never_guessed_and_original_candidate_survives_failed_refine():
    raw={'lines':[line('00000',[40,30,60,20])]}
    out=refine_digits(lambda *a:{'lines':[]},np.zeros((100,120,3),np.uint8),raw)
    assert out['lines'][0]['text']=='00000'
    assert not out['digit_region']['refined_readout_available']


def test_status_text_and_low_confidence_icons_do_not_create_numeric_roi():
    raw={'lines':[line('OFF',[1,1,30,18]),line('1',[10,30,5,6],.1)]}
    out=refine_digits(lambda *a:pytest.fail('No main digit row'),np.zeros((100,120,3),np.uint8),raw)
    assert out['lines']==[] and out['digit_region']['status']=='not_located'


def test_digit_coordinates_include_parent_crop_offset():
    raw={'lines':[line('240',[120,235,60,20])]}
    out=refine_digits(lambda *a:{'lines':[]},np.zeros((100,120,3),np.uint8),raw,100,200)
    assert min(p[0] for p in out['digit_region']['polygon'])>=100
    assert min(p[1] for p in out['digit_region']['polygon'])>=200


def test_partial_digit_text_can_locate_roi_but_is_not_guessed_into_a_reading():
    raw={'lines':[line('5:',[40,30,60,20])]}
    image=np.zeros((100,120,3),np.uint8)
    out=refine_digits(lambda *a:{'lines':[]},image,raw)
    assert out['digit_region']['status']=='located' and not out['lines']
    out=refine_digits(lambda *a:{'lines':[line('51',[0,0,60,20])]},image,raw)
    assert out['lines'][0]['text']=='51'


def test_conflicting_warp_does_not_overwrite_source_without_a_crop_check():
    raw={'lines':[line('390',[40,30,60,20])]}
    image=np.zeros((100,120,3),np.uint8)
    answers=iter([{'lines':[line('290',[0,0,60,20])]}, {'lines':[line('390',[12,12,60,20])]}])
    out=refine_digits(lambda *a:next(answers),image,raw,source_image=image)
    assert out['lines'][0]['text']=='390' and out['digit_consistency']['status']=='resolved'
    assert out['digit_region']['method']=='dominant_numeric_line_source_check_v1'
    json.dumps(out)
    out=refine_digits(lambda *a:{'lines':[line('290',[0,0,60,20])]},image,raw)
    assert out['lines']==[] and out['digit_consistency']['status']=='disagreement'


def test_failed_warp_cannot_promote_a_conflicting_wider_crop():
    raw={'lines':[line('055',[40,30,60,20],.67)]}
    image=np.zeros((100,120,3),np.uint8)
    answers=iter([{'lines':[line('06S',[0,0,60,20],.37)]},
                  {'lines':[line('065',[12,12,60,20],.62)]}])
    out=refine_digits(lambda *a:next(answers),image,raw,source_image=image)
    assert out['lines']==[]
    assert out['digit_consistency']['status']=='disagreement'
    assert out['digit_consistency']['candidates']==['055','065']
    assert out['panel_ocr']==raw


def test_small_lcd_retry_keeps_original_empty_output_and_source_coordinates():
    raw={'status':'completed','lines':[]}
    calls=[]
    def predict(image,x,y):
        calls.append(image.shape[:2])
        return {'status':'completed','lines':[line('02181',[40,30,100,40])]} if len(calls)==1 else {'lines':[]}
    out=refine_digits(predict,np.zeros((100,160,3),np.uint8),raw,400,600,recover_small=True)
    assert calls[0]==(200,320) and len(calls)==2
    assert out['panel_ocr']==raw and out['small_panel_recovery']['scale']==2
    assert out['lines'][0]['text']=='02181'
    assert out['lines'][0]['polygon'][0]==[420,615]


def test_small_panel_no_text_retry_is_bounded_to_one():
    calls=[]
    raw={'status':'completed','lines':[]}
    out=refine_digits(lambda *a:calls.append(1) or raw,np.zeros((100,160,3),np.uint8),raw,recover_small=True)
    assert len(calls)==1 and out['lines']==[]
