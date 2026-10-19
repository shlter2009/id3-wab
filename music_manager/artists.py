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


# Recognize technical units, never bare numbers or version/featured credits.
QUALITY_UNIT = r'(?:\d{2,4}\s*(?:kbps|kb/s|kbits?/s|k)|(?:8|16|24|32)\s*bit|\d{2,3}(?:\.\d+)?\s*khz)'
QUALITY_GROUP = re.compile(r'[\[(]\s*' + QUALITY_UNIT +
                           r'(?:[\s,;/+_-]+' + QUALITY_UNIT + r')*\s*[\])]', re.I)
# Bare "24K Magic" and "100K" can be real titles. A bare k suffix is
# removed only for common audio bitrates; explicit kbps units are unambiguous.
BITRATES = r'(?:32|40|48|56|64|80|96|112|128|144|160|192|224|256|288|320|384|448|512|640|768|1024|1411|1536)'
BARE_QUALITY = (r'(?:\d{2,4}\s*(?:kbps|kb/s|kbits?/s)|' + BITRATES +
                r'\s*k|(?:8|16|24|32)\s*bit|\d{2,3}(?:\.\d+)?\s*khz)')
QUALITY = re.compile(r'(?<![^\W_])' + BARE_QUALITY + r'(?![^\W_])', re.I)
LYRIC_LABEL = r'(?:official\s+)?(?:lyrics?|가사|한글가사|자막)(?:[\s/&·_-]*(?:lyrics?|가사|한글가사|자막))*'
LYRIC_SUFFIX = re.compile(r'(?:\s*[\[(]\s*' + LYRIC_LABEL + r'\s*[\])]|\s*[|ㅣᅵ┃│]\s*' + LYRIC_LABEL + r')\s*$', re.I)
VERSION_LABEL = re.compile(r'\b(?:live|remix|remaster(?:ed)?|edit|acoustic|instrumental|demo|version|feat|ft|mix|sped|slowed|reverb|part|pt|mono|stereo|radio|extended|cover)\b', re.I)


def clean_title(value: str) -> str:
    text = unicodedata.normalize('NFKC', value)
    text = QUALITY.sub(' ', QUALITY_GROUP.sub(' ', text))
    text = re.sub(r'\(\s*\)|\[\s*\]', ' ', text)
    text = LYRIC_SUFFIX.sub('', text)
    return re.sub(r'\s+', ' ', text).strip(' \t\r\n_-–—|,;/')


def split_album_hint(title: str) -> tuple[str, str]:
    """A trailing filename [album] is a hint, never a replacement for ID3."""
    match = re.fullmatch(r'(.+?)\s*\[([^\[\]]+)\]', title)
    if match and not VERSION_LABEL.search(match[2]) and not re.fullmatch(r'\d{4}', match[2].strip()):
        return match[1].strip(), match[2].strip()
    return title, ''


def search_key(value: str) -> str:
    """Compare spelling/punctuation without erasing live/remix identity."""
    return ' '.join(re.findall(r'[^\W_]+', identity(clean_title(value))))


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
