"""Single-writer archive upgrade; immutable bytes move, derived views regenerate."""
import hashlib
import json
from pathlib import Path
import time

from archive_events import make_views, prepare
from archive_paths import ArchivePaths, checked, empty_directories
from archive_progress import advance


def publish_events(archive, rows):
    """Deliver changed business files before scanning historical NAS content."""
    pending = {row['seq'] for row in rows if row['published_at'] and not row['readable_at']}
    with archive.core['db']() as conn:
        confirmation = conn.execute("SELECT value FROM archive_meta WHERE key='event_confirmation_pending'").fetchone()
    confirmations = set(json.loads(confirmation[0])) if confirmation else set()
    pending |= confirmations
    if not pending:
        return
    started = time.monotonic()
    root = archive._root()
    paths = ArchivePaths(root)
    bundles, _ = prepare(rows, paths)
    selected = [b for b in bundles if any(v['sequence'] in pending for v in b['history'])]
    receipts = {}
    for bundle in selected:
        for version in bundle['history']:
            seq = version['sequence']
            if seq in receipts:
                continue
            path = checked(root, paths.name(version['receipt']))
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != path.stem.split('-', 1)[-1]:
                raise ValueError('Cannot publish corrupt receipt')
            receipt = json.loads(raw)
            row = next(r for r in rows if r['seq'] == seq)
            if (receipt['sequence'], receipt['entity'], receipt['entity_id'], receipt['document']) != (
                    seq, row['entity'], row['entity_id'], json.loads(row['document'])):
                raise ValueError('Cannot publish mismatched receipt')
            receipts[seq] = receipt
    # Existing physical photo locations remain stable across partial/full passes.
    views = make_views(selected, paths, receipts, preserve_asset_locations=True)
    views = {name: raw for name, raw in views.items()
             if name != 'Readme.html' and '/DailyReport/' not in name}
    paths.retire_views(views, partial=True)
    delivered = pending & {v['sequence'] for b in selected for v in b['history']}
    if delivered:
        timestamp = archive.core['now']()
        with archive.core['db']() as conn:
            conn.executemany("UPDATE archive_outbox SET readable_at=?,readability_basis=? WHERE seq=? AND readable_at IS NULL",
                             [(timestamp, 'archive_writer_event_files_published', seq) for seq in delivered])
            # Confirmation and the durable echo obligation are committed together.
            # If the NAS fails during echo, retry the partial event publication
            # without depending on a successful historical navigation rebuild.
            conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('event_confirmation_pending',?)",
                         (json.dumps(sorted(confirmations | delivered)),))
        # Echo the confirmation into the same event files before slow historical
        # navigation work. An interrupted rebuild must not leave readable_at
        # hidden from the Evidence that was already published successfully.
        with archive.core['db']() as conn:
            refreshed = conn.execute('SELECT * FROM archive_outbox WHERE archived_at IS NOT NULL ORDER BY seq DESC').fetchall()
        keys = {b['key'] for b in selected}
        bundles, _ = prepare(refreshed, paths)
        selected = [b for b in bundles if b['key'] in keys]
        views = make_views(selected, paths, receipts, preserve_asset_locations=True)
        paths.retire_views({name: raw for name, raw in views.items()
                            if name != 'Readme.html' and '/DailyReport/' not in name}, partial=True)
        with archive.core['db']() as conn:
            conn.execute("INSERT OR REPLACE INTO archive_meta VALUES('event_confirmation_pending',?)",
                         (json.dumps(sorted(confirmations - delivered)),))
    with archive.lock:
        archive.status['last_event_publication'] = {'duration_ms': round((time.monotonic()-started)*1000, 2),
            'clock_basis': 'process_monotonic', 'scope': 'changed_event_files_and_timing_confirmation',
            'events': len(selected), 'receipts': len(delivered)}
    advance('business_files_published', len(selected), len(selected))


def retire_legacy(root, paths):
    # Recognize the old generated schema before removing a redundant view.
    for path in (root / 'Records').glob('**/Record.json'):
        data = json.loads(path.read_text())
        relative = path.relative_to(root).as_posix()
        if (data.get('schema') != 'field-recognition-record/2' or not data.get('derived_view')
                or relative not in paths.data['aliases']):
            continue
        path.unlink()
        companion = path.with_name('Readme.html')
        if companion.is_file() and '完整记录 JSON' in companion.read_text():
            companion.unlink()
    for path in (root / 'Browse').glob('**/Readme.html'):
        text = path.read_text()
        if '<title>' in text and ('归档首页' in text or '归档入口' in text) and ('id="records"' in text or 'meta http-equiv="refresh"' in text):
            paths.alias(path.relative_to(root).as_posix(), 'Readme.html')
            paths.save()
            path.unlink()
    catalog = root / 'Browse/Catalog.json'
    if catalog.is_file() and json.loads(catalog.read_text()).get('schema') == 'field-recognition-folders/2':
        paths.alias('Browse/Catalog.json', '.System/Index.json')
        paths.save(); catalog.unlink()
    for name, marker in [('Index.json', 'field-recognition-index/'), ('Audit.html', 'ARCHIVE'),
                         ('00_归档导航.html', '归档入口')]:
        path = root / name
        if not path.is_file():
            continue
        text = path.read_text()
        if marker in text or name == 'Audit.html' and '现场识别' in text:
            paths.alias(name, 'Readme.html' if name.startswith('00_') else '.System/' + name)
            paths.save(); path.unlink()
    old = root / 'LegacyLinks.json'
    if old.is_file():
        mappings = json.loads(old.read_text())
        if isinstance(mappings, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in mappings.items()):
            for source, target in mappings.items():
                paths.data['aliases'].setdefault(source, target)
            paths.save(); old.unlink()


def rebuild(archive, rows):
    from archive_catalog import build_index, render_index
    from archive_store import canonical
    root = archive._root()
    paths = ArchivePaths(root)
    with archive.core['db']() as conn:
        old_links = conn.execute("SELECT value FROM archive_meta WHERE key='legacy_links'").fetchone()
    if old_links:
        for source, target in json.loads(old_links[0]).items():
            paths.data['aliases'].setdefault(source, target)
    # Original receipts and prior integrity reports must remain byte-identical.
    for folder in ('Receipts', 'Integrity'):
        for source in sorted((root / folder).glob('**/*.json')):
            relative = source.relative_to(root).as_posix()
            paths.move(relative, '.System/' + relative)
    receipts = {}
    cache = getattr(archive, 'receipt_cache', {})
    archive.receipt_cache = cache
    for number, row in enumerate(rows, 1):
        path = checked(root, paths.name(row['receipt_path']))
        receipt = cache.get(path.name)
        if receipt is None:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != path.stem.split('-', 1)[-1]:
                raise ValueError('Cannot migrate corrupt receipt')
            receipt = json.loads(raw)
            cache[path.name] = receipt
        if (receipt['sequence'], receipt['entity'], receipt['entity_id'], receipt['document']) != (
                row['seq'], row['entity'], row['entity_id'], json.loads(row['document'])):
            raise ValueError('Cannot migrate mismatched receipt')
        receipts[row['seq']] = receipt
        advance('checking_receipts', number, len(rows))
    archive.receipt_cache = dict(list(cache.items())[-4096:])
    bundles, _ = prepare(rows, paths)
    paths.save()  # Persist stable event directories before moving their photos.
    views = make_views(bundles, paths, receipts)
    advance('building_navigation')
    # Retain orphaned historical evidence without placing it among valid measurements.
    for source in (root / 'Objects').glob('*/*'):
        if not source.is_file() or len(source.stem) != 64:
            continue
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source.stem:
            raise ValueError('Unreferenced archive object is corrupt')
        relative = source.relative_to(root).as_posix()
        stored = paths.data['assets'].get(source.stem)
        target = stored['path'] if stored else '.System/UnassignedAssets/' + source.name
        paths.move(relative, target)
    index = build_index(rows, archive.instance, archive.core['now'](), archive.integrity.snapshot())
    for item in index['items']:
        for key in ('image', 'receipt'):
            if item.get(key):
                item[key] = paths.name(item[key])
    if index['integrity'].get('report_path'):
        index['integrity']['report_path'] = paths.name(index['integrity']['report_path'])
    index['archive_base'] = '../'
    views['.System/Index.json'] = canonical(index)
    views['.System/Audit.html'] = render_index(index)
    for source, target in [('Index.json', '.System/Index.json'), ('Audit.html', '.System/Audit.html'),
                           ('00_归档导航.html', 'Readme.html'), ('Browse/Readme.html', 'Readme.html')]:
        paths.alias(source, target)
    paths.retire_views(views)
    retire_legacy(root, paths)
    paths.save()
    empty_directories(root)
    with archive.core['db']() as conn:
        for key, value in [('readable_files', json.dumps(sorted(views))), ('index_count', str(len(rows))),
                           ('index_version', archive.index_version), ('index_integrity', archive.integrity.snapshot()['report_path'] or '')]:
            conn.execute('INSERT OR REPLACE INTO archive_meta VALUES(?,?)', (key, value))
    return {'events': len(bundles), 'images': len(paths.data['assets']), 'receipts': len(receipts),
            'visible_days': len({b['day'] for b in bundles if b['kind'] != 'SourceMaterials'})}
