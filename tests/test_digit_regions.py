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
