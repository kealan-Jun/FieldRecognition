"""Select the dominant numeric line inside an already authorized display ROI."""
import copy
import math
import re
import time

import cv2
import numpy as np

from aliyun_vision import READOUT


def extract_digits(image, region):
    points = np.asarray(region['polygon'], np.float32)
    width, height = region['size']
    target = np.asarray([[0,0],[width-1,0],[width-1,height-1],[0,height-1]], np.float32)
    transform = cv2.getPerspectiveTransform(points, target)
    crop = cv2.warpPerspective(image, transform, (width,height), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    return crop, transform


def refine_digits(predict, image, initial, x=0, y=0, *, source_image=None, source_offset=(0,0)):
    """Use measured glyph geometry; never insert a decimal point or a unit."""
    candidates = []
    for line in initial.get('lines', []):
        polygon = np.asarray(line.get('polygon'), dtype=np.float32)
        text = line.get('text','')
        # A partly misread number can still locate the glyph row. This is only
        # a geometry candidate; ambiguous text is never returned as a reading.
        numeric_like = bool(re.search(r'\d',text) and re.fullmatch(r'[\d\s+\-.,:OoIl|S]+',text))
        if (not (READOUT.fullmatch(text) or numeric_like) or polygon.shape != (4,2)
                or not np.isfinite(polygon).all() or (line.get('confidence') or 0) < .45):
            continue
        width = (np.linalg.norm(polygon[1]-polygon[0]) + np.linalg.norm(polygon[2]-polygon[3])) / 2
        height = (np.linalg.norm(polygon[3]-polygon[0]) + np.linalg.norm(polygon[2]-polygon[1])) / 2
        if min(width,height) < 3:
            continue
        # Main measurement glyphs dominate small menu/status numbers. No fixed
        # screen coordinates, so a tilted or displaced panel remains supported.
        candidates.append((float(height * math.sqrt(width)), line, polygon, width, height))
    output = copy.deepcopy(initial)
    output['panel_ocr'] = copy.deepcopy(initial)
    if not candidates:
        output.update(lines=[], digit_region={'status':'not_located','method':'dominant_numeric_line_v1'})
        return output
    _, selected, polygon, width, height = max(candidates, key=lambda item:item[0])
    points = polygon - [x,y]
    center = points.mean(axis=0)
    points = center + (points-center)*1.15
    points[:,0] = np.clip(points[:,0],0,image.shape[1]-1)
    points[:,1] = np.clip(points[:,1],0,image.shape[0]-1)
    scale = min(4.,max(1.,64./height))
    size = [max(8,round(width*1.15*scale)),max(8,round(height*1.15*scale))]
    region = {'status':'located','method':'dominant_numeric_line_v1',
              'polygon':(points+[x,y]).tolist(),'size':size,'initial_text':selected['text']}
    crop, transform = extract_digits(image, dict(region,polygon=points.tolist()))
    started = time.monotonic()
    refined = predict(crop,0,0)
    lines = []
    inverse = np.linalg.inv(transform)
    for line in refined.get('lines', []):
        if not READOUT.fullmatch(line.get('text','')) or (line.get('confidence') or 0) < .45:
            continue
        line = copy.deepcopy(line)
        if line.get('polygon') is not None:
            mapped = cv2.perspectiveTransform(np.asarray([line['polygon']],np.float32),inverse)[0]
            line['polygon'] = (mapped+[x,y]).tolist()
        lines.append(line)
    # A clipped/warped glyph must not replace a contradictory original reading
    # solely because it came from the second pass. Check a wider source crop.
    best = max(lines,key=lambda line:line.get('confidence',0)) if lines else None
    original_valid = bool(READOUT.fullmatch(selected['text']))
    conflict = best is not None and original_valid and best['text'].strip()!=selected['text'].strip()
    alternative = None
    if source_image is not None and (not best or conflict):
        offset = np.asarray(source_offset)
        lo,hi = polygon.min(0)-offset,polygon.max(0)-offset
        margin=(hi-lo)*.3
        sx,sy=np.maximum(0,np.floor(lo-margin)).astype(int)
        ex,ey=np.minimum([source_image.shape[1],source_image.shape[0]],np.ceil(hi+margin)).astype(int)
        if ex>sx and ey>sy:
            rectangle=cv2.resize(source_image[sy:ey,sx:ex],None,fx=3,fy=3,interpolation=cv2.INTER_CUBIC)
            rectangle=cv2.copyMakeBorder(rectangle,12,12,12,12,cv2.BORDER_REPLICATE)
            check=predict(rectangle,0,0)
            output['digit_ocr_check']=copy.deepcopy(check)
            options=[l for l in check.get('lines',[]) if READOUT.fullmatch(l.get('text','')) and (l.get('confidence') or 0)>=.45]
            if options:
                alternative=copy.deepcopy(max(options,key=lambda l:l.get('confidence',0)))
                if alternative.get('polygon') is not None:
                    alternative['polygon']=((np.asarray(alternative['polygon'])-12)/3+[sx,sy]+offset).tolist()
    if conflict:
        best = next((candidate for candidate in (selected,best) if alternative and
                     candidate['text'].strip()==alternative['text'].strip()),None)
        output['digit_consistency']={'status':'resolved' if best else 'disagreement',
            'candidates':[selected['text'],lines[0]['text']], 'check_text':alternative and alternative['text']}
    elif not best and alternative:
        # A failed perspective pass does not make the wider crop authoritative.
        # Keep conflicting complete readings unresolved (e.g. OFF -> 055/065).
        if original_valid and selected['text'].strip() != alternative['text'].strip():
            conflict = True
            output['digit_consistency'] = {'status':'disagreement',
                'candidates':[selected['text'], alternative['text']], 'check_text':alternative['text']}
        else:
            best=alternative
    if best and alternative and best['text'].strip()==alternative['text'].strip():
        # Save the wider source window actually used to resolve the reading,
        # rather than the clipped perspective crop which disagreed with it.
        region.update(method='dominant_numeric_line_source_check_v1',
            polygon=(np.asarray([[sx,sy],[ex-1,sy],[ex-1,ey-1],[sx,ey-1]])+offset).tolist(),
            size=[int(ex-sx)*3,int(ey-sy)*3])
    # Keep only the strongest complete line in this isolated number window.
    fallback = [copy.deepcopy(selected)] if READOUT.fullmatch(selected['text']) else []
    output['lines'] = [copy.deepcopy(best)] if best else [] if conflict else fallback
    output.update(digit_region=region,digit_ocr=refined,
                  digit_refinement_seconds=round(time.monotonic()-started,3))
    output['digit_region']['refined_readout_available'] = bool(best)
    finished = output.get('digit_ocr_check', {}).get('ocr_finished_at') or refined.get('ocr_finished_at')
    if finished:
        output['ocr_finished_at'] = finished
    return output
