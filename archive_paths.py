"""Resumable path moves, hash deduplication and immutable-receipt compatibility.

Only the archive writer mutates this ledger. A move is journaled before rename;
readers accept the old location until the atomic rename has completed.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath

LEDGER = '.System/MigrationMap.json'


def checked(root, relative):
    if not isinstance(relative, str) or not relative or '\\' in relative:
        raise ValueError('Invalid archive path')
    parts = PurePosixPath(relative)
    if parts.is_absolute() or '..' in parts.parts or relative.startswith('/'):
        raise ValueError('Invalid archive path')
    path = (root / relative).resolve()
    path.relative_to(root.resolve())
    return path


def load(root):
    path = checked(root, LEDGER)
    if not path.exists():
        return {'schema': 'field-recognition-paths/1', 'aliases': {}, 'assets': {}, 'events': {}, 'views': {}}
    value = json.loads(path.read_text())
    if value.get('schema') != 'field-recognition-paths/1':
        raise ValueError('Unknown archive path ledger')
    return value


def resolve_name(root, relative, aliases=None):
    aliases = load(root)['aliases'] if aliases is None else aliases
    seen = set()
    while True:
        path = checked(root, relative)
        # During an interrupted move the old file is still authoritative.
        if path.is_file() or relative not in aliases:
            return relative
        if relative in seen:
            raise ValueError('Archive alias cycle')
        seen.add(relative)
        relative = aliases[relative]


def resolve_file(root, relative):
    return checked(root, resolve_name(root, relative))


class ArchivePaths:
    def __init__(self, root):
        self.root = root
        self.data = load(root)
        self.saved = json.dumps(self.data, sort_keys=True)

    def save(self):
        from archive_store import canonical, replace_view
        snapshot = json.dumps(self.data, sort_keys=True)
        if snapshot == self.saved and checked(self.root, LEDGER).is_file():
            return
        replace_view(checked(self.root, LEDGER), canonical(self.data))
        self.saved = snapshot

    def alias(self, source, target):
        checked(self.root, source); checked(self.root, target)
        if source != target:
            self.data['aliases'][source] = target

    def name(self, relative):
        return resolve_name(self.root, relative, self.data['aliases'])

    def move(self, source, target):
        source_path, target_path = checked(self.root, source), checked(self.root, target)
        if source == target:
            return
        if source_path.is_file() and target_path.exists():
            if source_path.read_bytes() != target_path.read_bytes():
                raise ValueError('Archive migration destination conflict')
        self.alias(source, target)
        self.save()  # Must precede rename or removal of a duplicate.
        if source_path.is_file():
            target_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.exists():
                source_path.unlink()
            else:
                source_path.rename(target_path)

    def asset(self, logical, raw):
        """Stage new bytes once; a completed bundle supplies their final location."""
        from archive_store import immutable_write
        digest = hashlib.sha256(raw).hexdigest()
        stored = self.data['assets'].get(digest)
        relative = self.name(stored['path'] if stored else logical)
        if not checked(self.root, relative).is_file():
            relative = '.System/PendingAssets/' + digest + Path(logical).suffix
        immutable_write(checked(self.root, relative), raw)
        result = {'path': relative, 'sha256': digest, 'size_bytes': len(raw)}
        self.data['assets'][digest] = result
        self.alias(logical, relative)
        self.save()
        return result

    def place(self, artifact, target):
        """Move a hash-addressed image into its event; aliases preserve all receipts."""
        digest = artifact['sha256']
        stored = self.data['assets'].get(digest, artifact)
        source = self.name(stored['path'])
        path = checked(self.root, source)
        if source == target and self.data['assets'].get(digest) == (artifact | {'path': target}):
            if path.stat().st_size != artifact['size_bytes']:
                raise ValueError('Archive evidence size changed')
            self.alias(artifact['path'], target)
            return self.data['assets'][digest]
        # Metadata is not evidence of a successful copy.
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest or len(raw) != artifact['size_bytes']:
            raise ValueError('Cannot migrate corrupt archive evidence')
        self.move(source, target)
        self.alias(artifact['path'], target)
        self.data['assets'][digest] = artifact | {'path': target}
        self.save()
        return self.data['assets'][digest]

    def directory(self, key, proposed):
        checked(self.root, proposed)
        # Value/owner edits never rename a measurement already delivered.
        return self.data['events'].setdefault(key, proposed)

    def result_file(self, event, instrument, directory):
        """Keep one stable six-field file per instrument, including later burst members."""
        slots = self.data.setdefault('result_files', {}).setdefault(event, {})
        if instrument not in slots:
            number = len(slots) + 1
            name = 'Result.json' if number == 1 else f'Result{number:02d}.json'
            slots[instrument] = directory + '/' + name
        checked(self.root, slots[instrument])
        return slots[instrument]

    def retire_views(self, views):
        """Remove only byte-verified generated views, never handwritten files."""
        from archive_store import replace_view
        previous = self.data.get('views', {})
        for relative, digest in previous.items():
            path = checked(self.root, relative)
            if relative not in views and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == digest:
                path.unlink()
        for relative, raw in views.items():
            path = checked(self.root, relative)
            if previous.get(relative) == hashlib.sha256(raw).hexdigest() and path.is_file():
                continue
            if not path.is_file() or path.read_bytes() != raw:
                replace_view(path, raw)
        self.data['views'] = {p: hashlib.sha256(raw).hexdigest() for p, raw in views.items()}
        self.save()


def empty_directories(root):
    for path in sorted(root.rglob('*'), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not path.is_symlink():
            try:
                path.rmdir()
            except OSError:
                pass
