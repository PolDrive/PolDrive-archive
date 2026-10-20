"""Rename locally identified artwork; preserve unknown files and an undo manifest."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def cache_name(url):
    value = 14695981039346656037
    for byte in url.encode('utf-8'):
        value = ((value ^ byte) * 1099511628211) & 0xffffffffffffffff
    return f'{value:016x}.art'


def safe_title(title):
    title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', ' - ', title)
    title = re.sub(r'\s+', ' ', title).strip(' .')
    title = title[:140].rstrip(' .')
    if re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', title, re.I):
        title = '_' + title
    return title


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    covers = ROOT / 'assets/covers'
    manifest = covers / 'game-name-renames.json'
    if manifest.exists():
        raise SystemExit('An undo manifest already exists; preserve it before another run.')
    files = {p.name: p for p in covers.glob('*.art')}
    titles = defaultdict(set)
    identities = {}

    def add(url, title):
        if isinstance(url, str) and url.startswith(('http://', 'https://')) and title:
            name = cache_name(url)
            if name in files:
                titles[name].add(title.strip())

    catalog = ROOT.parent / 'wolodrive/src/catalogData.json'
    if catalog.exists():
        for row in json.loads(catalog.read_text(encoding='utf-8')):
            for key in ('cover_url', 'boxart_url', 'icon_url'):
                add(row.get(key), row.get('title'))
    review = json.loads((ROOT / 'assets/artwork/review-queue.json').read_text(encoding='utf-8'))
    for row in review['entries']:
        identities[row['identity']] = row['title']
        for candidate in row.get('candidates', []):
            url = candidate.get('url', '')
            if urlsplit(url).hostname == 'thumbnails.libretro.com' and '/Named_Boxarts/' in url:
                add(url, Path(unquote(urlsplit(url).path)).stem)
            elif candidate.get('confidence', 0) >= 95 and 'exact-platform' in candidate.get('provider', ''):
                add(url, row['title'])
    for path in sorted((ROOT / 'assets/artwork').glob('*-covers.json')):
        for identity, row in json.loads(path.read_text(encoding='utf-8')).get('matches', {}).items():
            if identity in identities:
                add(row.get('url'), identities[identity])

    used = {name.casefold() for name in files}
    plan = []
    unresolved = []
    for name, path in sorted(files.items()):
        options = sorted(titles.get(name, ()), key=lambda value: (len(value), value))
        # Regional filenames and their catalogue title may both describe the
        # same scan. Prefer the specific title read from the cover URL.
        regional = [t for t in options if re.search(r'\((USA|Europe|Japan|World|Australia|Asia)(?:[, )])', t)]
        if regional:
            options = regional
        if len(options) != 1 or not safe_title(options[0]):
            unresolved.append({'file': name, 'titles': options})
            continue
        title = options[0]
        stem = safe_title(title)
        target = stem + '.art'
        if target.casefold() in used:
            target = f'{stem} [{path.stem}].art'
        if target.casefold() in used:
            raise SystemExit(f'Filename collision: {target}')
        used.add(target.casefold())
        plan.append({'old': name, 'new': target, 'title': title,
                     'bytes': path.stat().st_size,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    print(json.dumps({'rename': len(plan), 'unresolved': len(unresolved), 'sample': plan[:5]}, ensure_ascii=True))
    if not args.apply:
        return
    report = {'status': 'planned', 'original_count': len(files), 'renames': plan, 'unresolved': unresolved}
    manifest.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    for item in plan:
        source, target = covers / item['old'], covers / item['new']
        if target.exists():
            raise SystemExit(f'Target appeared during rename: {target}')
        source.rename(target)
    for item in plan:
        target = covers / item['new']
        assert target.stat().st_size == item['bytes']
        assert hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256']
        assert not (covers / item['old']).exists()
    assert sum(1 for _ in covers.glob('*.art')) == len(files)
    report['status'] = 'completed-and-verified'
    manifest.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(f'Verified {len(plan)} renames; {len(unresolved)} unresolved files preserved. Undo map: {manifest}')


if __name__ == '__main__':
    main()
