"""Human-readable NAS folders derived from acknowledged immutable receipts."""
import hashlib
import html
import json
import posixpath
import re
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo


CATEGORIES = {
    'Bindings': ('绑定记录', '按绑定开始日期、相机查谁在何时绑定仪器或场景。'),
    'InstrumentReadings': ('仪器读数', '按仪器编号、拍摄日期查读数；Unassigned 为归属待确认。'),
    'VoicePhotos': ('语音拍照读数', '按相机、拍摄日期查原语音照片文件及识别结果。'),
    'WorkbenchReadings': ('实验台总览', '按实验台、日期汇总仪器读数和未确定仪器归属的读数。'),
    'Photos': ('照片留存', '按照片日期、相机查看原图、面板图及 SHA-256。'),
}


def token(value):
    value = str(value or 'Unassigned')
    return value if re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value) else hashlib.sha256(value.encode()).hexdigest()[:24]


def local_time(value):
    return datetime.fromisoformat(value).astimezone(ZoneInfo('Asia/Shanghai'))


def artifact_path(digest, suffix='.png'):
    if re.fullmatch(r'[a-f0-9]{64}', digest or '') and suffix in {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tiff'}:
        return f'Objects/{digest[:2]}/{digest}{suffix}'
    return None


def page(title, body):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>'+html.escape(title)+'</title><style>body{max-width:1100px;margin:32px auto;padding:0 20px;font:15px/1.7 system-ui;color:#183c43;background:#f4f8f8}'
        'a{color:#246a70}article{background:white;border:1px solid #dce7e8;border-radius:12px;padding:24px;margin:16px 0}'
        'dt{color:#61787e}dd{margin:0 0 12px;overflow-wrap:anywhere}img{max-width:100%;max-height:650px;object-fit:contain}li{margin:10px 0}small{color:#61787e}</style>'
        '<h1>'+html.escape(title)+'</h1>'+body+'</html>').encode()


def build_views(rows):
    """No NAS reads or source mutations. Relative links work with file:// and HTTP."""
    latest, versions = {}, {}
    for row in rows:
        key = (row['entity'], row['entity_id'])
        versions.setdefault(key, []).append({'sequence': row['seq'], 'receipt': row['receipt_path']})
        if key not in latest or row['seq'] > latest[key]['seq']:
            latest[key] = row
    captures = {ident: json.loads(row['document']) for (entity, ident), row in latest.items() if entity == 'scans'}
    views, catalog = {}, {key: [] for key in CATEGORIES}
    esc = lambda value: html.escape(str(value if value is not None else '未记录'))

    def record(category, parts, title, data):
        directory = PurePosixPath('Browse', category, *map(token, parts))
        link = lambda p: html.escape(posixpath.relpath(p, str(directory)), quote=True)
        data = {'schema': 'field-recognition-browse/1', 'derived_view': True,
                'timezone': 'Asia/Shanghai', 'category': category, **data}
        views[str(directory / 'Record.json')] = json.dumps(data, ensure_ascii=False, indent=2).encode()
        operator = {'Demo验证': '样张验证'}.get(data.get('operator'), data.get('operator'))
        camera = {'DemoSampleCamera': '样张相机', 'SampleCamera': '样张相机'}.get(data.get('camera_id'), data.get('camera_id'))
        is_relation = data['entity'] in {'bindings', 'scene_visits'}
        display_time = lambda value: local_time(value).strftime('%Y-%m-%d %H:%M:%S') if value else None
        fields = [('时间', data['local_time']), ('实验员', operator), ('相机', camera),
                  ('仪器 / 场景', data.get('target')), ('结果状态', data.get('status')),
                  ('读数原文', '、'.join(str(r.get('text', '')) for r in data.get('readings', [])) or '未读到数字 / 非读数记录'),
                  ('照片来源', data.get('source_ref')), ('绑定开始' if is_relation else '识别开始', display_time(data.get('started_at'))),
                  ('绑定结束', display_time(data.get('ended_at'))), ('识别完成', display_time(data.get('finished_at')))]
        body = '<p><a href="'+link('Browse/Readme.html')+'">分类入口</a> · <a href="Record.json">完整分类记录 JSON</a></p><article><dl>'
        body += ''.join('<dt>'+esc(k)+'</dt><dd>'+esc(v)+'</dd>' for k, v in fields if v is not None)
        body += '</dl><p>读数及仪器归属需结合原图核对；原始历史记录不因分类重写。</p></article>'
        image = data.get('photos', {}).get('image')
        if image:
            body += '<article><a href="'+link(image['path'])+'"><img src="'+link(image['path'])+'" alt="识别时的完整照片"></a>'
            for name, photo in data['photos'].items():
                body += '<p><a href="'+link(photo['path'])+'">'+esc({'image':'完整照片', 'original':'原始文件', 'panel':'面板识别图'}.get(name, name))+'</a><br><small>SHA-256 '+esc(photo['sha256'])+'</small></p>'
            body += '</article>'
        body += '<article><h2>原始回执版本</h2><ul>'+''.join('<li><a href="'+link(v['receipt'])+'">版本 '+str(v['sequence'])+'</a></li>' for v in data['receipt_versions'])+'</ul></article>'
        views[str(directory / 'Readme.html')] = page(title, body)
        catalog[category].append({'title': title, 'local_time': data['local_time'], 'record': str(directory / 'Record.json'),
                                  'page': str(directory / 'Readme.html'), 'entity_id': data['entity_id']})

    for (entity, ident), row in latest.items():
        if entity not in {'bindings', 'scene_visits', 'jobs', 'scans'}:
            continue
        doc = json.loads(row['document'])
        capture = doc if entity == 'scans' else captures.get(doc.get('capture_id') or doc.get('scan_id'), {})
        external = doc.get('external_photo') or capture.get('external_photo') or {}
        moment = (doc.get('started_at') if entity in {'bindings', 'scene_visits'} else None) or external.get('captured_at') or (
            doc.get('video_observation') or {}).get('observed_at') or capture.get('received_at') or doc.get('submitted_at') or row['recorded_at']
        local = local_time(moment); day = local.date().isoformat(); camera = doc.get('camera_id')
        photos = {}
        for name, digest, suffix in [('image', capture.get('image_sha256'), '.png'),
                                     ('original', capture.get('source_sha256'), PurePosixPath(capture.get('original_blob') or '').suffix),
                                     ('panel', doc.get('crop_image_sha256'), '.png')]:
            path = artifact_path(digest, suffix)
            if path:
                photos[name] = {'path': path, 'sha256': digest}
        from reading_results import build_readings
        readings = doc.get('readings')
        if readings is None:
            readings = build_readings(doc) if entity == 'jobs' else []
        asset = doc.get('instrument') or {}
        workbench = doc.get('workbench') or {'name': asset.get('scene') or (doc.get('scene') or {}).get('name')}
        target = asset.get('name') or (doc.get('scene') or {}).get('name') or '归属待确认'
        common = {'entity': entity, 'entity_id': ident, 'camera_id': camera, 'operator': doc.get('operator'),
            'event_at': moment, 'local_time': local.strftime('%Y-%m-%d %H:%M:%S'), 'target': target,
            'started_at': doc.get('started_at'), 'ended_at': doc.get('ended_at'), 'finished_at': doc.get('finished_at'),
            'status': doc.get('status') or ('已结束' if doc.get('ended_at') else '已记录'),
            'capture_id': capture.get('capture_id'), 'job_id': doc.get('job_id'), 'binding_id': doc.get('binding_id'),
            'binding_ids': doc.get('binding_ids', []), 'instrument': doc.get('instrument'),
            'instrument_candidates': doc.get('instrument_candidates', []), 'workbench': workbench,
            'readings': readings, 'source_ref': external.get('source_ref'), 'external_capture_id': external.get('capture_id'),
            'captured_at': external.get('captured_at'), 'source_written_at': external.get('source_written_at'),
            'input_mode': 'voice_photo' if external else 'video' if doc.get('request_trigger') == 'video_stream' else 'other',
            'timing': doc.get('timing'), 'photos': photos, 'receipt_versions': sorted(versions[(entity, ident)], key=lambda v:v['sequence'])}
        title = common['local_time']+' · '+target
        if entity in {'bindings', 'scene_visits'}:
            record('Bindings', [day, camera, ident], title+' · '+str(doc.get('operator') or '未登记人员').replace('Demo验证', '样张验证'), common)
        elif entity == 'jobs':
            groups = {}
            for reading in readings:
                groups.setdefault((reading.get('instrument') or {}).get('id') or 'Unassigned', []).append(reading)
            if not groups:
                candidates = doc.get('instrument_candidates') or []
                sole = asset.get('id') or (candidates[0]['instrument']['id'] if len(candidates) == 1 else 'Unassigned')
                groups[sole] = []
            for instrument_id, values in groups.items():
                record('InstrumentReadings', [instrument_id, day, ident], title, common | {'readings': values, 'folder_instrument_id': instrument_id})
            if external:
                filename = PurePosixPath(external.get('source_ref') or external.get('capture_id') or ident).name
                record('VoicePhotos', [camera, day, local.strftime('%H-%M-%S')+'_'+ident], title+' · '+filename, common)
            record('WorkbenchReadings', [workbench.get('id') or workbench.get('name'), day, ident], title, common)
        elif photos:
            record('Photos', [day, camera, local.strftime('%H-%M-%S')+'_'+ident], common['local_time']+' · 照片', common)

    body = '<p>每条记录包含时间、实验员、相机、原始照片链接和回执。打开分类后选择具体记录。日期按北京时间。</p>'
    for key, (title, description) in CATEGORIES.items():
        items = sorted(catalog[key], key=lambda item:item['local_time'], reverse=True)
        folder = 'Browse/'+key+'/Readme.html'
        links = ''.join('<li><a href="'+html.escape(posixpath.relpath(item['page'], 'Browse/'+key))+'">'+esc(item['title'])+'</a></li>' for item in items)
        views[folder] = page(title, '<p><a href="../Readme.html">返回分类入口</a></p><p>'+esc(description)+'</p><p>'+str(len(items))+' 条记录</p><ul>'+links+'</ul>')
        body += '<article><h2><a href="'+key+'/Readme.html">'+title+'</a></h2><p>'+description+'</p><code>Browse/'+key+'/</code><p>'+str(len(items))+' 条记录</p></article>'
    body += '<p>照片原件统一保存在 <code>Objects/</code>，各分类引用同一份文件，避免重复存储。完整历史回执在 <code>Receipts/</code>；巡检在 <code>Integrity/</code>。Browse 是可重建的查阅视图，原始回执才是历史依据。</p><p><a href="../Readme.html">全量检索与完整性巡检</a> · <a href="Catalog.json">分类目录 JSON</a></p>'
    views['Browse/Readme.html'] = page('NAS 文件夹查阅指南', body)
    views['Browse/Catalog.json'] = json.dumps({'schema':'field-recognition-folders/1', 'categories':catalog}, ensure_ascii=False, indent=2).encode()
    return views
