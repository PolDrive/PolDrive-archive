"""Recover catalogue title/URL mappings from wolodrive's previous static build."""
from collections import defaultdict
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from rename_artwork import ROOT, cache_name, safe_title


def parse_literal(text, start):
    quote = text[start]
    if quote not in "'\"`":
        raise ValueError('Expected a JavaScript string literal')
    output = []
    i = start + 1
    escapes = {'n': '\n', 'r': '\r', 't': '\t', 'b': '\b', 'f': '\f', 'v': '\v', '0': '\0'}
    while i < len(text):
        char = text[i]
        if char == quote:
            return json.loads(''.join(output))
        if char == '\\':
            i += 1
            char = text[i]
            if char in ('u', 'x'):
                length = 4 if char == 'u' else 2
                output.append(chr(int(text[i + 1:i + 1 + length], 16)))
                i += length
            elif char == '\n':
                pass
            else:
                output.append(escapes.get(char, char))
        else:
            output.append(char)
        i += 1
    raise ValueError('Unterminated literal')


def main():
    repo = ROOT.parent / 'wolodrive'
    sys.path.insert(0, str(repo / 'scripts'))
    from enrich_artwork import normalize
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])
    revisions = git('log', '--all', '-10', '--format=%H', '--', 'static-preview/assets').decode().splitlines()
    blobs = {}
    for revision in revisions:
        for line in git('ls-tree', '-r', revision, 'static-preview').decode().splitlines():
            meta, path = line.split('\t', 1)
            if path.endswith('.js'):
                blobs.setdefault(meta.split()[2], path)
    documents = []
    for blob, path in blobs.items():
        text = git('cat-file', 'blob', blob).decode('utf-8')
        count = 0
        for match in re.finditer(r'JSON\.parse\(', text):
            try:
                value = parse_literal(text, match.end())
            except (ValueError, IndexError):
                continue
            documents.append(value)
            count += 1
        print(f'{path}: {count} data blocks', flush=True)
    covers = ROOT / 'assets/covers'
    files = {p.name: p for p in covers.glob('*.art') if re.fullmatch('[0-9a-f]{16}\\.art', p.name)}
    titles = defaultdict(set)
    identities = defaultdict(set)
    records = []
    manifests = []
    hashed = lru_cache(maxsize=None)(cache_name)

    def add(url, title):
        if isinstance(url, str) and url.startswith(('https://', 'http://')) and isinstance(title, str) and title.strip():
            name = hashed(url)
            if name in files:
                titles[name].add(title.strip())

    def walk(value, source=''):
        if isinstance(value, list):
            for item in value:
                walk(item, source)
        elif isinstance(value, dict):
            if any('|' in k and isinstance(v, dict) and 'url' in v for k, v in value.items()):
                manifests.append(value)
                return
            if 'packages' in value:
                source = value.get('name', source)
            title = value.get('title')
            if title:
                records.append((source, value))
                identity = value.get('id', value.get('id_code', ''))
                if identity:
                    identities[source + '|' + identity].add(title)
                if value.get('platform'):
                    identities[value['platform'] + '|' + normalize(title)].add(title)
                for key in ('boxArtUrl', 'boxartUrl', 'boxCoverUrl', 'posterUrl', 'coverUrl', 'iconUrl', 'image', 'cover', 'icon', 'landscapeUrl', 'cardUrl', 'bannerUrl', 'cover_url', 'boxart_url', 'icon_url'):
                    add(value.get(key), title)
            for key, child in value.items():
                if isinstance(child, (list, dict)):
                    walk(child, source)

    for document in documents:
        walk(document)
    for path in sorted((ROOT / 'assets/artwork').glob('*-covers.json')):
        manifests.append(json.loads(path.read_text(encoding='utf-8')).get('matches', {}))
    approved = json.loads(git('show', 'fb9a298a:assets/artwork/igdb-covers.json'))
    manifests.append(approved.get('matches', {}))
    fixture = repo / 'build/live_artwork.h'
    if fixture.exists():
        for line in fixture.read_text(encoding='utf-8').splitlines():
            fields = {key: json.loads(literal) for key, literal in re.findall(r'\.(\w+)=("(?:[^"\\]|\\.)*")', line)}
            for key in ('cover_url', 'boxart_url', 'icon_url'):
                add(fields.get(key), fields.get('title'))
    for manifest in manifests:
        for identity, match in manifest.items():
            if not isinstance(match, dict):
                continue
            for title in identities.get(identity, ()):
                add(match.get('url'), title)
                fallback = match.get('fallback', {})
                if isinstance(fallback, dict):
                    add(fallback.get('url'), title)
    print(f'Recovered {len(records)} records, {len(manifests)} artwork manifests; matched {len(titles)}/{len(files)} remaining files.', flush=True)
    manifest_path = covers / 'game-name-renames.json'
    report = json.loads(manifest_path.read_text(encoding='utf-8'))
    by_digest = {item['sha256']: item['title'] for item in report['renames']}
    for name, path in files.items():
        if name not in titles:
            title = by_digest.get(hashlib.sha256(path.read_bytes()).hexdigest())
            if title:
                titles[name].add(title)
    used = {p.name.casefold() for p in covers.iterdir()}
    plan = []
    for name, options in sorted(titles.items()):
        # All aliases are explicitly linked to this exact image URL in the
        # original catalogue; use the shortest display title for shared art.
        options = sorted(options, key=lambda t: (len(t), t.casefold(), t))
        title = options[0]
        stem = safe_title(title)
        if not stem:
            continue
        target = stem + '.art'
        if target.casefold() in used:
            target = f'{stem} [{Path(name).stem}].art'
        assert target.casefold() not in used
        used.add(target.casefold())
        path = files[name]
        plan.append({'old': name, 'new': target, 'title': title, 'aliases': options,
                     'bytes': path.stat().st_size, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                     'mapping_source': 'wolodrive HEAD^ static-preview'})
    report['renames'].extend(plan)
    report['unresolved'] = [{'file': name, 'titles': []} for name in sorted(files.keys() - titles.keys())]
    report['status'] = 'planned-history-recovery'
    manifest_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    for item in plan:
        source, target = covers / item['old'], covers / item['new']
        assert not target.exists()
        source.rename(target)
    for item in plan:
        path = covers / item['new']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item['sha256']
        assert not (covers / item['old']).exists()
    assert sum(1 for _ in covers.glob('*.art')) == report['original_count']
    report['status'] = 'completed-and-verified'
    manifest_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(f'Verified {len(plan)} additional renames; {len(report["renames"])} total, {len(report["unresolved"])} unresolved.')


if __name__ == '__main__':
    main()
