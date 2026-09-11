from pathlib import Path
import cv2
import numpy as np
from qr_decode import decode_qr


def test_multiple_labels_and_original_coordinates():
    root=Path(__file__).parents[1]/'static/labels'
    images=[cv2.imread(str(root/(name+'.png'))) for name in ('InstrumentA','InstrumentB')]
    resized=[cv2.resize(im,(400,400)) for im in images]
    frame=np.full((500,1000,3),255,np.uint8)
    frame[50:450,50:450]=resized[0];frame[50:450,550:950]=resized[1]
    values,points,info=decode_qr(frame)
    assert len(values)==2 and len(points)==2
    assert all(0<=x<1000 and 0<=y<500 for polygon in points for x,y in polygon)
    assert not info['identity_inferred']


def test_blank_never_guesses_registered_identity():
    values,points,info=decode_qr(np.full((400,600,3),255,np.uint8))
    assert values==points==[] and info['decoded_count']==0
