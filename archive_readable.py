"""Chinese business navigation; references immutable evidence without copying photos."""
import hashlib
import html
import json
import posixpath
import re
from pathlib import PurePosixPath

ROOT = '01_业务数据'
ENTRY = '00_归档导航.html'
FOLDERS = {
    'Bindings': ('01_绑定记录', '谁在什么时间绑定了哪台仪器或哪个场景'),
    'Handoffs': ('02_设备交接', '原使用人交出、指定接收人接收的时间和凭证'),
    'InstrumentReadings': ('03_设备面板读数', '按仪器查看每项指标、数值、单位和照片'),
    'VoicePhotos': ('04_语音拍照读数', '从一次语音拍照文件追溯到识别结果'),
    'PhotoDrafts': ('05_测量草稿', '连拍合并后的字段、冲突及校正历史，尚未正式提交'),
    'ExperimentRecords': ('06_已确认实验记录', '确认后写入并读回的正式测量，一次测量一条记录'),
    'WorkbenchReadings': ('07_实验台总览', '同一实验台上各台仪器的读数，逐项注明归属'),
    'Photos': ('08_原始材料', '原始照片及扫码凭证；保留材料不等于确认仪器归属'),
}


def segment(value):
    # Human-readable on Linux/Windows, with no traversal or ambiguous sanitization.
    raw = str(value or '未指定')
    clean = re.sub(r'[^\w\u4e00-\u9fff-]', '_', raw).strip('_')[:48] or '未指定'
    return clean + ('_' + hashlib.sha256(raw.encode()).hexdigest()[:8] if clean != raw else '')


def build_readable(records, page):
    views, groups = {}, {key: [] for key in FOLDERS}
    esc = lambda value: html.escape(str(value if value is not None else '未记录'), quote=True)
    for category, data, canonical_page in records:
        draft = data.get('measurement_draft') or {}
        # The latest draft view becomes a confirmed record, not a second pending item.
        if category == 'PhotoDrafts' and draft.get('status') in {'confirmed', 'rejected'}:
            continue
        if data['entity'] == 'jobs' and not data.get('readings') and not data.get('measurement_records'):
            continue  # Empty processing receipts remain in the audit index.
        sources = draft.get('sources', [])
        operator = data.get('operator') or '、'.join(dict.fromkeys(s['operator'] for s in sources if s.get('operator'))) or '未登记人员'
        target = data.get('target') or '、'.join(dict.fromkeys((f.get('instrument') or {}).get('name', '归属待确认') for f in draft.get('fields', []))) or '归属待确认'
        clock = data['local_time']
        row_id = segment(data['entity'] + '_' + data['entity_id'])
        directory = PurePosixPath(ROOT, FOLDERS[category][0], segment(clock[:10]), segment(operator),
                                 segment(target), segment(clock[11:].replace(':', '-')) + '_' + row_id)
        link = lambda path: esc(posixpath.relpath(path, str(directory)))
        fields = []
        if draft:
            for f in draft.get('fields', []):
                fields.append({'instrument': (f.get('instrument') or {}).get('name'), 'instrument_id': (f.get('instrument') or {}).get('id'),
                    'name': f.get('name'), 'value': f.get('value'), 'unit': f.get('unit'),
                    'original_text': [r.get('raw_text') for r in f.get('original_candidates', [])],
                    'corrected': f.get('corrected', False), 'issues': f.get('recognition_issues', [])})
        else:
            for r in data.get('readings', []):
                fields.append({'instrument': (r.get('instrument') or {}).get('name'), 'instrument_id': (r.get('instrument') or {}).get('id'),
                    'name': r.get('measurement_name'), 'value': r.get('value'), 'unit': r.get('unit'),
                    'original_text': [r.get('text')], 'corrected': False, 'issues': [r['quality_issue']] if r.get('quality_issue') else []})
        scope = data.get('record_scope') or ('legacy_test_only' if data['entity'] == 'jobs' else 'evidence')
        scope_text = {'test_only': '测试识别结果', 'legacy_test_only': '历史测试结果', 'draft': '草稿，尚未提交',
                      'production_confirmed': '已确认实验记录', 'evidence': '原始事实与凭证'}.get(scope, scope)
        summary = {'schema': 'field-recognition-readable/1', 'entity': data['entity'], 'entity_id': data['entity_id'],
            'time_beijing': clock, 'operator': operator, 'camera_id': data.get('camera_id'), 'target': target,
            'record_scope': scope, 'status': data.get('status'), 'source_ref': data.get('source_ref'),
            'values': fields, 'photos': data.get('photos', {}), 'detail_page': canonical_page,
            'receipt_versions': data['receipt_versions'], 'derived_view': True}
        views[str(directory / '记录.json')] = json.dumps(summary, ensure_ascii=False, indent=2).encode()
        body = '<p><a href="' + link(ENTRY) + '">归档导航</a> · <a href="记录.json">本条记录 JSON</a> · <a href="' + link(canonical_page) + '">完整证据与修订历史</a></p>'
        body += '<article><h2>' + esc(target) + '</h2><p>' + esc(clock) + '（北京时间） · ' + esc(operator) + ' · ' + esc(data.get('camera_id')) + '</p><p>' + esc(scope_text) + '</p>'
        if data.get('source_ref'):
            body += '<p>语音照片来源：' + esc(data['source_ref']) + '</p>'
        for key, label in [('started_at', '绑定开始'), ('ended_at', '绑定结束')]:
            if data.get(key):
                body += '<p>' + label + '：' + esc(data[key]) + '</p>'
        handoff = data.get('handoff')
        if handoff:
            body += '<p>' + esc(handoff['source_operator']) + ' → ' + esc(handoff['recipient_operator']) + '</p>'
            for key, label in [('status', '交接状态'), ('released_at', '交出时间'), ('accepted_at', '接收时间')]:
                body += '<p>' + label + '：' + esc(handoff.get(key)) + '</p>'
        if fields:
            body += '<table><thead><tr><th>仪器</th><th>指标</th><th>数值</th><th>单位</th><th>原始识别</th><th>校正</th></tr></thead><tbody>'
            for f in fields:
                body += '<tr>' + ''.join('<td>' + esc(v) + '</td>' for v in
                    (f['instrument'] or '归属待确认', f['name'] or '指标待确认', f['value'], f['unit'] or '未知',
                     '、'.join(str(t) for t in f['original_text'] if t is not None), '已校正' if f['corrected'] else '未修订')) + '</tr>'
            body += '</tbody></table>'
        if draft.get('blockers') and draft.get('status') != 'confirmed':
            body += '<p>待处理：' + esc('；'.join(draft['blockers'])) + '</p>'
        body += '</article>'
        for name, photo in data.get('photos', {}).items():
            body += '<article><a href="' + link(photo['path']) + '"><img loading="lazy" src="' + link(photo['path']) + '" alt="留存照片"></a><p>' + esc({'image':'完整照片', 'original':'原始文件', 'panel':'面板区域'}.get(name, name)) + '</p><small>SHA-256 ' + esc(photo['sha256']) + '</small></article>'
        views[str(directory / '查看记录.html')] = page(target + ' · ' + clock, body)
        groups[category].append({'time': clock, 'operator': operator, 'target': target,
            'path': str(directory / '查看记录.html'), 'entity': data['entity'], 'entity_id': data['entity_id']})

    body = '<p>先选业务类型，再按日期、实验员、仪器查找。日期均为北京时间。</p>'
    body += '<article><h2>每条数据怎么看</h2><p>文件夹顺序：业务类型 → 日期 → 实验员 → 仪器或场景 → 时间与记录编号。</p><p>打开“查看记录.html”核对照片、指标、数值、单位；“记录.json”供程序读取。一次连拍只形成一份测量，内含多张原图和分开的指标。</p></article>'
    body += '<div class="business-grid">'
    for category, (folder, description) in FOLDERS.items():
        entries = sorted(groups[category], key=lambda x: x['time'], reverse=True)
        category_dir = ROOT + '/' + folder
        listing = '<p><a href="../../' + ENTRY + '">返回归档导航</a></p><p>' + esc(description) + '</p><p>' + str(len(entries)) + ' 条业务记录</p>'
        listing += '<label>查找日期、实验员或仪器 <input id="search" placeholder="例如 2026-09-15 徐荣炜"></label><ul id="records">'
        for e in entries:
            listing += '<li><a href="' + esc(posixpath.relpath(e['path'], category_dir)) + '">' + esc(e['time'] + ' · ' + e['operator'] + ' · ' + e['target']) + '</a></li>'
        listing += '</ul><script>document.getElementById("search").oninput=function(){const words=this.value.trim().toLowerCase().split(/\\s+/);document.querySelectorAll("#records li").forEach(e=>e.hidden=!words.every(w=>e.textContent.toLowerCase().includes(w)));};</script>'
        views[category_dir + '/打开目录.html'] = page(folder[3:], listing)
        body += '<article><h2><a href="' + esc(category_dir + '/打开目录.html') + '">' + esc(folder[3:]) + '</a></h2><p>' + esc(description) + '</p><p>' + str(len(entries)) + ' 条 · ' + esc(category_dir) + '</p></article>'
    body += '</div><details><summary>技术目录含义</summary><p>Objects：照片原件，按哈希去重；Receipts：不可覆盖的历史回执；Integrity：完整性检查；Index.json：程序索引；Browse：兼容的分类视图。</p><p>各业务分类引用同一份照片，不重复复制原件。目录是查阅视图，重传不会新增同一条测量；历史修订另有回执保留。</p><a href="Readme.html">全量版本索引</a></details>'
    views[ENTRY] = page('现场识别 · 归档导航', body)
    return views
