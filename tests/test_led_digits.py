import copy

import cv2
import numpy as np
import pytest

from led_digits import PATTERNS, segment_check, verify_digits


def display(text):
    image = np.full((70, len(text)*44+12, 3), (15, 20, 125), np.uint8)
    boxes = [(5,0,27,8), (22,5,30,25), (22,25,30,45), (5,42,27,50),
             (0,25,8,45), (0,5,8,25), (5,21,27,29)]
    for i, char in enumerate(text):
        pattern = next(k for k, v in PATTERNS.items() if v == char)
        for flag, (x1,y1,x2,y2) in zip(pattern, boxes):
            if flag == '1':
                cv2.rectangle(image, (12+i*44+x1,10+y1), (12+i*44+x2,10+y2), (235,245,255), -1)
    return image


def local(text, image):
    h,w = image.shape[:2]
    return {'lines': [{'text': text, 'confidence': .99}],
            'digit_region': {'status': 'located', 'polygon': [[0,0],[w-1,0],[w-1,h-1],[0,h-1]], 'size': [w,h]}}


def test_visible_top_segment_resolves_one_seven_without_overwriting_raw_ocr():
    image=display('72'); raw=local('12', image); before=copy.deepcopy(raw)
    result=verify_digits(image, raw)
    assert result['lines'][0]['text']=='72'
    assert result['lines'][0]['original_ocr_text']=='12'
    assert result['lines'][0]['confidence'] is None  # Do not invent calibrated accuracy.
    assert result['digit_segment_check']['status']=='consistent'
    assert len(result['digit_segment_check']['views'])>=2
    assert raw==before


@pytest.mark.parametrize('text', ['56','78','90'])
def test_unambiguous_segment_patterns_agree(text):
    result=segment_check(display(text), len(text))
    assert result['status']=='consistent' and result['text']==text


def test_marginal_segment_geometry_keeps_ocr_instead_of_claiming_a_correction():
    image=display('23');raw=local('23',image)
    result=verify_digits(image,raw)
    assert result['digit_segment_check']['status']=='inconclusive'
    assert result['lines']==raw['lines']


def test_other_numeric_disagreement_requires_fallback_instead_of_replacement():
    image=display('72')
    result=verify_digits(image,local('82',image))
    assert not result['lines'] and result['digit_consistency']['status']=='disagreement'


@pytest.mark.parametrize('text', ['OFF', '-12', '1.2', '12 g'])
def test_noninteger_status_sign_decimal_or_unit_is_not_reinterpreted(text):
    image=display('72');raw=local(text,image)
    assert verify_digits(image,raw) is raw


def test_plain_background_cannot_correct_ocr_and_offset_coordinates_are_supported():
    image=np.full((70,100,3),128,np.uint8);raw=local('12',image)
    assert verify_digits(image,raw)['lines']==raw['lines']
    image=display('72');raw=local('12',image)
    raw['digit_region']['polygon']=(np.asarray(raw['digit_region']['polygon'])+[300,400]).tolist()
    assert verify_digits(image,raw,300,400)['lines'][0]['text']=='72'
