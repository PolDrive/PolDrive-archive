"""Organise artwork using original catalogue console identities and image URLs."""
import argparse
from collections import Counter, defaultdict
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from rename_artwork import ROOT, cache_name

CONSOLES = {
    'sony-ps1': ('Sony', 'PlayStation'), 'sony-ps2': ('Sony', 'PlayStation 2'),
    'sony-ps3': ('Sony', 'PlayStation 3'), 'sony-ps4': ('Sony', 'PlayStation 4'),
    'sony-ps5': ('Sony', 'PlayStation 5'), 'sony-psp': ('Sony', 'PlayStation Portable'),
    'sony-psvita': ('Sony', 'PlayStation Vita'),
    'nintendo-nes': ('Nintendo', 'NES'), 'nintendo-snes': ('Nintendo', 'SNES'),
    'nintendo-n64': ('Nintendo', 'Nintendo 64'), 'nintendo-gc': ('Nintendo', 'GameCube'),
    'nintendo-wii': ('Nintendo', 'Wii'), 'nintendo-wiiu': ('Nintendo', 'Wii U'),
    'nintendo-switch': ('Nintendo', 'Nintendo Switch'), 'nintendo-3ds': ('Nintendo', 'Nintendo 3DS'),
    'nintendo-ds': ('Nintendo', 'Nintendo DS'), 'nintendo-gba': ('Nintendo', 'Game Boy Advance'),
    'nintendo-gbc': ('Nintendo', 'Game Boy Color'), 'nintendo-gb': ('Nintendo', 'Game Boy'),
    'nintendo-pokemini': ('Nintendo', 'Pokemon Mini'),
    'sega-megadrive': ('Sega', 'Mega Drive - Genesis'), 'sega-cd': ('Sega', 'Sega CD'),
    'sega-saturn': ('Sega', 'Saturn'), 'sega-dreamcast': ('Sega', 'Dreamcast'),
    'sega-gamegear': ('Sega', 'Game Gear'), 'sega-master': ('Sega', 'Master System'),
    'sega-32x': ('Sega', '32X'),
    'atari-2600': ('Atari', 'Atari 2600'), 'atari-5200': ('Atari', 'Atari 5200'),
    'atari-7800': ('Atari', 'Atari 7800'),
    'microsoft-xbox': ('Microsoft', 'Xbox'), 'microsoft-xbox360': ('Microsoft', 'Xbox 360'),
}
FIELDS = ('boxArtUrl', 'boxartUrl', 'boxCoverUrl', 'posterUrl', 'coverUrl', 'iconUrl',
          'image', 'cover', 'icon', 'landscapeUrl', 'cardUrl', 'bannerUrl',
          'cover_url', 'boxart_url', 'icon_url')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    repo = ROOT.parent / 'wolodrive'
    sys.path.insert(0, str(repo / 'scripts'))
    from enrich_artwork import normalize, SYSTEMS
    from urllib.parse import unquote, urlsplit
    covers = (ROOT / 'assets/covers').resolve()
    report_path = covers / 'console-organisation.json'
    assert not report_path.exists(), 'An organisation report already exists.'
    rename_path = covers / 'game-name-renames.json'
    renames = json.loads(rename_path.read_text(encoding='utf-8'))
    renamed = {r['new']: r for r in renames['renames']}
    originals = {r['old'] for r in renames['renames']}
    url_platforms = defaultdict(set)
    direct_platforms = defaultdict(set)
    title_platforms = defaultdict(set)
    identities = defaultdict(set)
    product_platforms = defaultdict(set)
    hashed = lru_cache(maxsize=None)(cache_name)

    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args])

    def add(url, platform):
        if platform not in CONSOLES or not isinstance(url, str) or not url.startswith(('http://', 'https://')):
            return
        name = hashed(url)
        if name in originals:
            url_platforms[name].add(platform)
            parts = unquote(urlsplit(url).path).split('/')
            for key, system in SYSTEMS.items():
                if system in parts and key in CONSOLES:
                    direct_platforms[name].add(key)

    def record(source, row):
        platform = row.get('platform')
        if platform not in CONSOLES:
            return
        title = row.get('title', '')
        title_platforms[normalize(title)].add(platform)
        identities[source + '|' + row.get('id', '')].add(platform)
        identities[platform + '|' + normalize(title)].add(platform)
        for key in ('id', 'productId', 'titleId'):
            if row.get(key):
                product_platforms[row[key]].add(platform)
        for key in FIELDS:
            add(row.get(key), platform)

    revision = 'fb9a298a'
    paths = git('ls-tree', '-r', '--name-only', revision, 'assets/catalogs').decode().splitlines()
    for path in paths:
        for source in json.loads(git('show', revision + ':' + path)).get('sources', []):
            for row in source.get('packages', []):
                record(source['name'], row)
    for path in git('ls-tree', '-r', '--name-only', revision, 'assets/artwork').decode().splitlines():
        if not path.endswith('.json'):
            continue
        document = json.loads(git('show', revision + ':' + path))
        for identity, match in document.get('matches', {}).items():
            for platform in identities.get(identity, ()):
                add(match.get('url'), platform)
                if isinstance(match.get('fallback'), dict):
                    add(match['fallback'].get('url'), platform)
        for row in document.get('entries', []):
            platform = row.get('platform')
            for candidate in row.get('candidates', []):
                add(candidate.get('url'), platform)
    native = json.loads((repo / 'src/catalogData.json').read_text(encoding='utf-8'))
    for row in native:
        code = row.get('id_code', '')
        platform = 'sony-ps5' if code.startswith('PPSA') else 'sony-ps4' if code.startswith('CUSA') else None
        if platform:
            product_platforms[code].add(platform)
            for key in FIELDS:
                add(row.get(key), platform)
    fixture = repo / 'build/live_artwork.h'
    if fixture.exists():
        for line in fixture.read_text(encoding='utf-8').splitlines():
            row = {k: json.loads(v) for k, v in re.findall(r'\.(\w+)=("(?:[^"\\]|\\.)*")', line)}
            platform = 'sony-ps5' if row.get('id_code', '').startswith('PPSA') else 'sony-ps4' if row.get('id_code', '').startswith('CUSA') else None
            for key in FIELDS:
                add(row.get(key), platform)

    digest_platforms = defaultdict(set)
    for row in renames['renames']:
        digest_platforms[row['sha256']].update(direct_platforms.get(row['old']) or url_platforms.get(row['old'], ()))
    plan = []
    occupied = set()
    counts = Counter()
    unidentified = []
    for source in sorted(covers.iterdir()):
        if source.suffix not in ('.art', '.cover') or not source.is_file():
            continue
        if source.suffix == '.art':
            row = renamed[source.name]
            platforms = direct_platforms.get(row['old']) or url_platforms.get(row['old']) or digest_platforms.get(row['sha256']) or title_platforms.get(normalize(row['title']), set())
        else:
            platforms = product_platforms.get(source.stem, set())
            if not platforms and source.stem.startswith(('PPSA', 'CUSA')):
                platforms = {'sony-ps5' if source.stem.startswith('PPSA') else 'sony-ps4'}
        destinations = []
        for platform in sorted(platforms):
            company, console = CONSOLES[platform]
            destinations.append(Path(company) / console / source.name)
        if not destinations:
            destinations = [Path('Unidentified') / 'Unknown Console' / source.name]
            unidentified.append(source.name)
        for relative in destinations:
            target = (covers / relative).resolve()
            assert target.is_relative_to(covers) and source.resolve().parent == covers
            assert not target.exists()
            assert str(relative).casefold() not in occupied
            occupied.add(str(relative).casefold())
            counts[str(relative.parent)] += 1
        stat = source.stat()
        plan.append({'source': source.name, 'destinations': [p.as_posix() for p in destinations],
                     'bytes': stat.st_size, 'file_id': stat.st_ino})
    report = {'status': 'planned', 'original_images': len(plan), 'organised_images': sum(counts.values()),
              'folders': dict(sorted(counts.items())), 'unidentified': unidentified, 'moves': plan,
              'shared_artwork': 'Hard links place shared artwork in each applicable console folder.'}
    print(json.dumps({k: v for k, v in report.items() if k != 'moves'}, indent=2), flush=True)
    if not args.apply:
        return
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    for index, item in enumerate(plan):
        source = covers / item['source']
        destinations = [covers / p for p in item['destinations']]
        for target in destinations:
            target.parent.mkdir(parents=True, exist_ok=True)
        assert source.resolve().is_relative_to(covers)
        assert all(target.resolve().is_relative_to(covers) for target in destinations)
        source.rename(destinations[0])
        for target in destinations[1:]:
            os.link(destinations[0], target)
        for target in destinations:
            stat = target.stat()
            assert stat.st_size == item['bytes'] and stat.st_ino == item['file_id']
        if (index + 1) % 5000 == 0:
            print(f'Organised {index + 1}/{len(plan)} original images', flush=True)
    assert not list(covers.glob('*.art')) and not list(covers.glob('*.cover'))
    actual = sum(1 for p in covers.rglob('*') if p.is_file() and p.suffix in ('.art', '.cover'))
    assert actual == report['organised_images']
    for item in plan:
        if item['source'] in renamed:
            renamed[item['source']]['current_paths'] = item['destinations']
    rename_path.write_text(json.dumps(renames, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    report['status'] = 'completed-and-verified'
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    print(f'Verified {len(plan)} original images organised into {len(counts)} console folders; {len(unidentified)} unidentified.')


if __name__ == '__main__':
    main()
