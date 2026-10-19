"""Validated user artist dictionaries and conservative filename recognition."""
from __future__ import annotations

import re
import unicodedata


def identity(value: str) -> str:
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def validate_artists(value) -> list[dict]:
    if not isinstance(value, list) or len(value) > 200:
        raise ValueError('아티스트는 최대 200명까지 등록할 수 있습니다.')
    output, owners = [], {}
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('아티스트 목록 형식이 올바르지 않습니다.')
        name, aliases = item.get('name'), item.get('aliases', [])
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError('정식 이름은 1~120자로 입력하세요.')
        if not isinstance(aliases, list) or len(aliases) > 20:
            raise ValueError('별칭은 한 아티스트당 최대 20개입니다.')
        name = name.strip()
        names, seen = [], set()
        for alias in [name, *aliases]:
            if not isinstance(alias, str) or not 1 <= len(alias.strip()) <= 120:
                raise ValueError('별칭은 각각 1~120자로 입력하세요.')
            alias, key = alias.strip(), identity(alias)
            if any(ord(char) < 32 for char in alias):
                raise ValueError('이름과 별칭에는 줄바꿈 등 제어 문자를 사용할 수 없습니다.')
            if key in owners:
                raise ValueError(f'이름 또는 별칭 “{alias}”이(가) 다른 아티스트와 겹칩니다.')
            if key not in seen:
                names.append(alias)
                seen.add(key)
        owners.update({key: name for key in seen})
        output.append({'name': name, 'aliases': names[1:]})
    return output


# Strip only technical quality annotations; live/remix/featured credits are identity.
QUALITY = re.compile(r'\s*[\[(]\s*(?:\d{2,4}\s*k(?:bps)?|\d{2,3}\s*kb/s|\d{2}\s*bit|\d{2,3}(?:\.\d+)?\s*khz)\s*[\])]\s*', re.I)


def clean_title(value: str) -> str:
    return re.sub(r'\s+', ' ', QUALITY.sub(' ', value)).strip()


def filename_hint(stem: str, artists=()) -> tuple[str, str]:
    text = clean_title(stem)
    parts = [part.strip() for part in re.split(r'\s+[-–—|]\s+', text) if part.strip()]
    known = {identity(alias): artist['name'] for artist in artists
             for alias in [artist['name'], *artist.get('aliases', [])]}
    matches = [(i, known[identity(part)]) for i, part in enumerate(parts) if identity(part) in known]
    if matches:
        names = {name for _, name in matches}
        remaining = [part for i, part in enumerate(parts) if i not in {index for index, _ in matches}]
        if len(names) == 1 and remaining:
            return matches[0][1], ' - '.join(remaining)
        # Two known names could be collaborators or a song named after an artist.
        return '', text
    if len(parts) >= 2:
        return parts[0], ' - '.join(parts[1:])
    return '', text


def canonical_artist(value: str, artists=()) -> str:
    key = identity(value)
    for artist in artists:
        if any(identity(alias) == key for alias in [artist['name'], *artist.get('aliases', [])]):
            return artist['name']
    return value
