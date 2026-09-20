"""Bounded pixel check for integer digits on red seven-segment displays.

This is an independent geometry check, not a measured OCR accuracy score.
Separated red bars use a second bounded geometry check. Signed, decimal and
status displays remain with the existing reader.
"""
import copy
import re

import cv2
import numpy as np

from digit_regions import extract_digits

VERSION = 'red-led-segments/1'
SEGMENTS = ((.2, 0, .8, .2), (.65, .15, 1, .48), (.65, .52, 1, .9),
            (.2, .8, .8, 1), (0, .52, .35, .9), (0, .15, .35, .48), (.2, .4, .8, .6))
PATTERNS = {'1111110': '0', '0110000': '1', '1101101': '2', '1111001': '3',
            '0110011': '4', '1011011': '5', '1011111': '6', '1110000': '7',
            '1111111': '8', '1111011': '9'}


def segment_check(crop, digits):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    red = ((hsv[:, :, 0] < 15) | (hsv[:, :, 0] > 165)) & (hsv[:, :, 1] > 80)
    evidence = {'rule_version': VERSION, 'status': 'inconclusive', 'views': []}
    if red.mean() < .15 or min(crop.shape[:2]) < 12:
        return evidence
    channel = crop[:, :, 1]  # Bright LED segments against their red display surround.
    threshold, _ = cv2.threshold(channel, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if threshold < 10 or float(np.percentile(channel, 95)) - threshold < 20:
        return evidence
    for factor in (.85, 1., 1.15):
        _, mask = cv2.threshold(channel, threshold * factor, 255, cv2.THRESH_BINARY)
        _, _, stats, _ = cv2.connectedComponentsWithStats(mask)
        glyphs = [(x, y, w, h) for x, y, w, h, area in stats[1:]
                  if h >= crop.shape[0] * .35 and .30 <= w / h <= .85 and area >= w*h*.12]
        glyphs.sort()
        if len(glyphs) != digits or any(a[0]+a[2] > b[0] for a, b in zip(glyphs, glyphs[1:])):
            continue
        text, fills = '', []
        for x, y, w, h in glyphs:
            glyph = mask[y:y+h, x:x+w]
            levels = [float(np.mean(glyph[int(t*h):max(int(t*h)+1, int(b*h)),
                                          int(l*w):max(int(l*w)+1, int(r*w))] > 0))
                      for l, t, r, b in SEGMENTS]
            # A marginal segment cannot settle a conflicting reading.
            if any(.30 < level < .50 for level in levels):
                break
            value = PATTERNS.get(''.join('1' if level >= .50 else '0' for level in levels))
            if value is None:
                break
            text += value
            fills.append([round(level, 3) for level in levels])
        if len(text) == digits:
            evidence['views'].append({'threshold': round(threshold*factor, 2), 'text': text, 'segment_fill': fills})
    values = {view['text'] for view in evidence['views']}
    if len(values) == 1 and len(evidence['views']) >= 2:
        evidence.update(status='consistent', text=values.pop())
    elif len(values) > 1:
        evidence.update(status='disagreement')
    return evidence


def verify_digits(image, local, x=0, y=0):
    lines = local.get('lines', [])
    region = local.get('digit_region') or {}
    if region.get('status') != 'located':
        return local
    if lines and (len(lines) != 1 or not re.fullmatch(r'\d{2,6}', lines[0].get('text', '').strip())):
        return local
    raw_lines = [line for key in ('panel_ocr', 'digit_ocr', 'digit_ocr_check')
                 for line in local.get(key, {}).get('lines', [])]
    texts = [line.get('text', '').strip() for line in raw_lines or lines]
    if any(re.search(r'[+\-.,°/]', text) or text.upper() == 'OFF' for text in texts):
        return local
    from red_segments import check as check_fragmented, corroborates
    fragmented = check_fragmented(image)
    if fragmented['status'] in {'consistent', 'disagreement'}:
        output = copy.deepcopy(local)
        output.update(digit_segment_check=fragmented, pre_segment_lines=copy.deepcopy(lines),
                      digit_consistency_before_segments=copy.deepcopy(local.get('digit_consistency')))
        numeric = [text for text in texts if re.fullmatch(r'\d{1,6}', text)]
        resolved = fragmented['status'] == 'consistent' and corroborates(fragmented['text'], numeric)
        output['digit_consistency'] = {'status': 'resolved' if resolved else 'disagreement',
            'candidates': list(dict.fromkeys(texts)), 'basis': fragmented['rule_version']}
        output['lines'] = []
        if resolved:
            # Preserve the measured original-pixel window and all previous OCR
            # attempts. Never copy a neighbouring photo's reading or confidence.
            left, top, width, height = fragmented['crop']
            polygon = [[left+x, top+y], [left+x+width-1, top+y],
                       [left+x+width-1, top+y+height-1], [left+x, top+y+height-1]]
            value = fragmented['text']
            output['digit_region_before_segments'] = copy.deepcopy(region)
            output['digit_region'] = {'status': 'located', 'method': fragmented['rule_version'],
                'polygon': polygon, 'size': [width*3, height*3], 'initial_text': texts[0] if texts else None,
                'refined_readout_available': True}
            output['lines'] = [{'text': value, 'value': value, 'numeric_candidates': [value],
                'confidence': None, 'polygon': polygon, 'reading_source': 'led_segment_check',
                'original_ocr_text': texts[0] if texts else None}]
        return output
    if len(lines) != 1:
        return local
    original = lines[0].get('text', '').strip()
    if not re.fullmatch(r'\d{2,6}', original):
        return local
    # Check only the already localized number window, never the rest of the photo.
    polygon = (np.asarray(region['polygon']) - [x, y]).tolist()
    crop, _ = extract_digits(image, region | {'polygon': polygon})
    check = segment_check(crop, len(original))
    output = copy.deepcopy(local)
    output['digit_segment_check'] = check
    if check['status'] == 'consistent' and check['text'] != original:
        # Resolve only the known OCR 1/7 ambiguity when every segment is clear.
        # Other differences stay unresolved for the normal fallback/draft path.
        resolved = all(a == b or {a, b} == {'1', '7'} for a, b in zip(original, check['text']))
        output['digit_consistency'] = {'status': 'resolved' if resolved else 'disagreement',
            'candidates': [original, check['text']], 'basis': VERSION}
        output['lines'] = [lines[0] | {'text': check['text'], 'value': check['text'],
            'numeric_candidates': [check['text']], 'confidence': None,
            'reading_source': 'led_segment_check', 'original_ocr_text': original}] if resolved else []
    elif check['status'] == 'disagreement':
        output['lines'] = []
        output['digit_consistency'] = {'status': 'disagreement', 'basis': VERSION}
    return output
