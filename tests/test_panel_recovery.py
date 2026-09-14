import copy

import numpy as np
import pytest

from panel_recovery import detect, source_box


def box(score=.6, class_id=0, coords=None):
    return {'class_id':class_id,'confidence':score,'xyxy':coords or [35,30,75,65]}


def run_rescue(image, responses):
    calls=[]
    def infer(pixels,size,threshold):
        calls.append((pixels.copy(),size,threshold))
        return {'boxes':copy.deepcopy(responses[len(calls)-1]),'speed_ms':{'inference':2.}}
    return detect(image,infer),calls


def test_complete_primary_detection_needs_one_pass_and_keeps_original_boxes():
    original=[box(coords=[0,0,20,20]),box(coords=[30,0,50,20]),box(class_id=1)]
    result,calls=run_rescue(np.zeros((100,100,3),np.uint8),[original])
    assert len(calls)==1 and not result['recovery']['attempted']
    assert [r['xyxy'] for r in result['boxes']]==[b['xyxy'] for b in original]
    assert all(r['localization_method']=='original' for r in result['boxes'])


def test_recovery_requires_two_views_preserves_input_and_deduplicates():
    image=np.full((100,100,3),(20,20,180),np.uint8);before=image.copy()
    result,calls=run_rescue(image,[[],[box()], [box(.4)], [box(.5)]])
    assert len(calls)==4 and np.array_equal(before,image)
    assert len(result['boxes'])==result['recovery']['recovered_count']==1
    assert len(result['boxes'][0]['supporting_views'])==3
    assert result['boxes'][0]['xyxy']==[35,30,75,65]
    assert calls[1][1:]==(800,.1)
    assert result['speed_ms']['inference']==8.


@pytest.mark.parametrize('responses,color',[
    ([[],[box()],[],[]],(20,20,180)),  # one transformed view is not enough
    ([[],[box(.15)],[box(.15)],[box(.15)]],(20,20,180)),  # threshold unchanged
    ([[],[box()],[box()],[box()]],(220,220,220)),  # sleeve/background
    ([[],[box(class_id=1)],[box(class_id=1)],[box(class_id=1)]],(20,20,180)),
])
def test_uncorroborated_low_score_or_wrong_display_color_is_rejected(responses,color):
    result,calls=run_rescue(np.full((100,100,3),color,np.uint8),responses)
    assert len(calls)==4 and not result['boxes']


def test_augmented_overlap_does_not_replace_existing_evidence_crop():
    result,_=run_rescue(np.full((100,100,3),(20,20,180),np.uint8),
                        [[box(.3)],[box(.9)],[box(.8)],[box(.7)]])
    assert len(result['boxes'])==1 and result['boxes'][0]['localization_method']=='original'
    assert result['boxes'][0]['confidence']==.3


def test_affine_coordinates_return_to_source_and_clip_to_image():
    matrix=np.asarray([[1,0,12],[0,1,-5]],np.float32)
    assert source_box([17,15,27,25],matrix,100,100)==[5,20,15,30]
    assert source_box([-10,-10,120,120],None,100,100)==[0,0,100,100]
