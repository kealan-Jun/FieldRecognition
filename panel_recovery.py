"""Bounded exposure/tilt recovery for the registered A/B display detector.

Enhanced images locate boxes only. OCR and evidence always use source pixels.
"""
import math

import cv2
import numpy as np


VERSION = 'exposure_tilt_v1'
GAMMA_LUT = np.uint8((np.arange(256) / 255.) ** 2.2 * 255)


def iou(a, b):
    width = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    height = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    intersection = width * height
    union = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection
    return intersection / union if union > 0 else 0


def source_box(box, matrix, width, height):
    x1, y1, x2, y2 = box
    if matrix is not None:
        corners = np.asarray([[[x1,y1], [x2,y1], [x2,y2], [x1,y2]]], np.float32)
        points = cv2.transform(corners, cv2.invertAffineTransform(matrix))[0]
        x1, y1, x2, y2 = points[:,0].min(), points[:,1].min(), points[:,0].max(), points[:,1].max()
    return [max(0,float(x1)), max(0,float(y1)), min(width,float(x2)), min(height,float(y2))]


def has_display_color(hsv, box):
    """A red windows / B blue LCD context; reject recovered sleeve/desk boxes."""
    x1,y1,x2,y2 = box['xyxy']
    pad = max(2, round(min(x2-x1,y2-y1)*.15))
    crop = hsv[max(0,math.floor(y1)-pad):min(hsv.shape[0],math.ceil(y2)+pad),
               max(0,math.floor(x1)-pad):min(hsv.shape[1],math.ceil(x2)+pad)]
    if not crop.size:
        return False
    hue, saturation, value = cv2.split(crop)
    color = ((hue <= 12) | (hue >= 170)) if box['class_id'] == 0 else ((hue >= 90) & (hue <= 135))
    return float(np.mean(color & (saturation >= 30) & (value >= 35))) >= .06


def detect(image, infer, *, imgsz=960, confidence=.25, photo=False):
    """Four live-video forwards; photo closeups allow eight additional forwards."""
    height,width = image.shape[:2]
    primary = infer(image, imgsz, confidence)
    accepted = [dict(b, localization_method='original') for b in primary['boxes']]
    recovery = {'version':VERSION, 'attempted':False, 'passes':1, 'recovered_count':0}
    diagnostics = {'raw_candidate_count':len(primary['boxes']),
        'raw_count_basis':'model_returned_boxes_after_model_threshold_and_nms',
        'passes':[{'view':'original','candidate_count':len(primary['boxes'])}], 'excluded_candidates':[]}

    def exclude(box, reason):
        diagnostics['excluded_candidates'].append({k:box[k] for k in ('class_id','xyxy','confidence','view') if k in box} | {'reason':reason})

    def output():
        diagnostics['quality_filtered_count'] = len(accepted)
        return {'boxes':accepted, 'speed_ms':speed, 'recovery':recovery, 'diagnostics':diagnostics}

    speed = dict(primary.get('speed_ms', {}))
    # Existing full detections win over augmented duplicates, preserving crops.
    if sum(b['class_id']==0 for b in accepted) >= 2 and any(b['class_id']==1 for b in accepted):
        return output()
    recovery['attempted'] = True
    dark = cv2.LUT(image, GAMMA_LUT)
    views = [('gamma_scale', dark, min(imgsz,800), None)]
    for angle in (6,-6):
        matrix = cv2.getRotationMatrix2D((width/2,height/2), angle, 1.)
        pixels = cv2.warpAffine(dark, matrix, (width,height), borderMode=cv2.BORDER_REPLICATE)
        views.append((f'gamma_tilt_{angle}', pixels, imgsz, matrix))
    candidates = []
    for name,pixels,size,matrix in views:
        result = infer(pixels, size, max(.1,confidence*.4))
        recovery['passes'] += 1
        diagnostics['raw_candidate_count'] += len(result['boxes'])
        diagnostics['passes'].append({'view':name,'candidate_count':len(result['boxes'])})
        for key,value in result.get('speed_ms', {}).items():
            speed[key] = speed.get(key,0) + value
        for box in result['boxes']:
            mapped = source_box(box['xyxy'], matrix, width, height)
            if mapped[2]-mapped[0] >= 4 and mapped[3]-mapped[1] >= 4:
                candidates.append(dict(box, xyxy=mapped, view=name))
            else:
                exclude(dict(box,xyxy=mapped,view=name), 'region_too_small')
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    for box in sorted(candidates, key=lambda b:b['confidence'], reverse=True):
        if box['confidence'] < confidence or len(accepted) >= 6:
            exclude(box, 'below_recovery_confidence' if box['confidence'] < confidence else 'candidate_limit')
            continue
        if any(iou(box['xyxy'], b['xyxy']) >= .4 for b in accepted):
            exclude(box, 'duplicate_region')
            continue
        supporting = sorted({b['view'] for b in candidates if b['class_id']==box['class_id']
                             and iou(box['xyxy'],b['xyxy']) >= .5})
        if len(supporting) < 2 or not has_display_color(hsv,box):
            exclude(box, 'insufficient_supporting_views' if len(supporting) < 2 else 'display_color_mismatch')
            continue
        accepted.append({k:v for k,v in box.items() if k!='view'} |
                        {'localization_method':VERSION, 'supporting_views':supporting})
        recovery['recovered_count'] += 1
    if photo and sum(b['class_id']==0 for b in accepted) < 2:
        # Close photographs can exceed the scale seen during training. Two
        # padded views must agree; this never runs on the live video path.
        closeups=[]
        for scale in (.35,.45):
            matrix=cv2.getRotationMatrix2D((width/2,height/2),0,scale)
            pixels=cv2.warpAffine(image,matrix,(width,height),borderValue=(114,114,114))
            result=detect(pixels,infer,imgsz=imgsz,confidence=confidence)
            recovery['passes']+=result['recovery']['passes']
            nested = result['diagnostics']
            diagnostics['raw_candidate_count'] += nested['raw_candidate_count']
            diagnostics['passes'].extend(dict(p,view=f"closeup_{scale}/{p['view']}") for p in nested['passes'])
            diagnostics['excluded_candidates'].extend(dict(b,view=f"closeup_{scale}/{b.get('view','original')}",
                coordinate_space='scaled_detector_input') for b in nested['excluded_candidates'])
            for key,value in result.get('speed_ms',{}).items():speed[key]=speed.get(key,0)+value
            for box in result['boxes']:
                if box['class_id']==0:
                    closeups.append(dict(box,xyxy=source_box(box['xyxy'],matrix,width,height),view=scale))
        for box in sorted(closeups,key=lambda b:b['confidence'],reverse=True):
            if len(accepted)>=6 or any(iou(box['xyxy'],b['xyxy'])>=.4 for b in accepted):
                exclude(box, 'candidate_limit' if len(accepted)>=6 else 'duplicate_region')
                continue
            support={b['view'] for b in closeups if iou(box['xyxy'],b['xyxy'])>=.5}
            if len(support)<2 or not has_display_color(hsv,box):
                exclude(box, 'insufficient_supporting_views' if len(support)<2 else 'display_color_mismatch')
                continue
            accepted.append({k:v for k,v in box.items() if k!='view'} |
                            {'localization_method':'photo_closeup_scale_v1','supporting_views':sorted(support)})
            recovery['recovered_count']+=1
    return output()
