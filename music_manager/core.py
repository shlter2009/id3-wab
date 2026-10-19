from __future__ import annotations

import json
import io
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import requests
from mutagen import File
from mutagen.mp3 import MP3
from mutagen.id3 import APIC, ID3, TALB, TCON, TDRC, TIT2, TPE1
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .artists import canonical_artist, clean_title, filename_hint, search_key, split_album_hint
from .release import VERSION


@dataclass
class Metadata:
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    release_id: str = ""
    score: int = 0
    duration: float | None = None
    other_release_ids: tuple[str, ...] = ()
    release_group_id: str = ''
    credited_artists: tuple[str, ...] = ()


def artist_matches(candidate: Metadata, expected: str) -> bool:
    """Recognize a credited artist, including explicit featuring credits."""
    names = [candidate.artist, *candidate.credited_artists]
    names.extend(re.split(r'\s+(?:feat\.?|ft\.?|featuring|with)\s+', candidate.artist, flags=re.I))
    return search_key(expected) in {search_key(name) for name in names}


class MusicBrainzRetry(Retry):
    def get_backoff_time(self):
        # urllib3's first retry normally has no delay.
        return max(1.1, super().get_backoff_time())


def session() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=0.7, status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.mount("http://", HTTPAdapter(max_retries=retry))
    s.mount("https://musicbrainz.org/", HTTPAdapter(max_retries=MusicBrainzRetry(
        total=3, backoff_factor=0.7, status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"], respect_retry_after_header=False)))
    s.headers["User-Agent"] = f"ID3SitWeb/{VERSION} (https://github.com/shlter2009/id3-wab)"
    return s


class Network:
    def __init__(self):
        self.http = session()
        self.last_mb = 0.0
        self.search_queries: list[str] = []

    def json(self, url, params=None):
        # MusicBrainz allows at most one request per second per client.
        time.sleep(max(0, 1.1 - (time.monotonic() - self.last_mb)))
        self.last_mb = time.monotonic()
        r = self.http.get(url, params=params, timeout=(10, 25))
        r.raise_for_status()
        return r.json()

    def download(self, url: str, target: Path, progress=lambda value: None,
                 limit=512 * 1024 * 1024, audio=True):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            raise ValueError("인증정보 없는 HTTP(S) 직접 파일 URL을 입력하세요.")
        if any(host == domain or host.endswith('.' + domain)
               for domain in ('youtube.com', 'youtu.be', 'googlevideo.com', 'youtube-nocookie.com')):
            raise ValueError("YouTube 페이지/스트림 추출은 지원하지 않습니다. 공식 제공자가 허용한 직접 오디오 파일을 사용하세요.")
        with self.http.get(url, stream=True, timeout=(10, 30)) as r:
            r.raise_for_status()
            # Validate redirect destination as well.
            final_host = (urlparse(r.url).hostname or '').lower().rstrip('.')
            if audio and any(final_host == d or final_host.endswith('.' + d)
                             for d in ('youtube.com', 'youtu.be', 'googlevideo.com', 'youtube-nocookie.com')):
                raise ValueError("YouTube 스트림으로 리디렉션되는 URL은 지원하지 않습니다.")
            mime = r.headers.get("Content-Type", "").split(";")[0].lower()
            if audio and (mime.startswith("text/") or mime in {"application/json", "application/xhtml+xml"}):
                raise ValueError("오디오 파일 대신 웹페이지 또는 API 응답을 받았습니다.")
            total = int(r.headers.get("Content-Length", 0))
            if total > limit:
                raise ValueError("파일 크기 제한을 초과했습니다.")
            size = 0
            with target.open("wb") as f:
                for chunk in r.iter_content(65536):
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("파일 크기 제한을 초과했습니다.")
                    f.write(chunk)
                    progress(min(1, size / total) if total else 0)
            if not size:
                raise ValueError("빈 파일입니다.")

    def search(self, hint: Metadata) -> list[Metadata]:
        """Try both filename orders before a bounded, title-only fallback.

        These are suggestions for user confirmation, never automatic matches.
        Scores from different MusicBrainz queries aren't directly comparable.
        """
        self.search_queries.clear()
        hint = replace(hint, title=clean_title(hint.title), artist=clean_title(hint.artist))
        if not hint.title:
            return []
        direct = self._search_recordings(hint)
        def exact(candidate, query):
            return (search_key(candidate.title) == search_key(query.title) and
                    (not query.artist or artist_matches(candidate, query.artist)))
        if any(exact(candidate, hint) for candidate in direct):
            return self._rank(direct, hint)
        results = list(direct)
        if hint.artist and search_key(hint.artist) != search_key(hint.title):
            reverse = replace(hint, title=hint.artist, artist=hint.title)
            swapped = self._search_recordings(reverse)
            if any(exact(candidate, reverse) for candidate in swapped):
                return self._rank(swapped, reverse)
            results.extend(swapped)
        results.extend(self._search_recordings(replace(hint, artist='')))
        # An artist's name alone is not evidence that a same-named SONG is
        # relevant. Reversed results must match both sides of the filename.
        relevant = [candidate for candidate in results if search_key(candidate.title) == search_key(hint.title)
                    or hint.artist and search_key(candidate.title) == search_key(hint.artist)
                    and artist_matches(candidate, hint.title)]
        unique = {}
        for candidate in relevant:
            key = (search_key(candidate.title), search_key(candidate.artist), candidate.release_id,
                   candidate.album, candidate.year)
            unique.setdefault(key, candidate)
        return self._rank(list(unique.values()), hint)[:30]

    @staticmethod
    def _rank(candidates, hint):
        return sorted(candidates, key=lambda candidate: (
            search_key(candidate.title) != search_key(hint.title),
            bool(hint.artist) and not artist_matches(candidate, hint.artist),
            bool(hint.album) and search_key(candidate.album) != search_key(hint.album),
            (abs(candidate.duration - hint.duration) if candidate.duration is not None
             and hint.duration is not None else float('inf')),
            -candidate.score))

    def _search_recordings(self, hint):
        def escape(text):
            return re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r'\\\1', text)
        query = f'recording:"{escape(hint.title)}"'
        if hint.artist:
            query += f' AND artist:"{escape(hint.artist)}"'
        self.search_queries.append(query)
        data = self.json("https://musicbrainz.org/ws/2/recording/",
                         {"query": query, "fmt": "json", "limit": 10})
        results = []
        for rec in data.get("recordings", []):
            artist = ''.join(a.get('name', a.get('artist', {}).get('name', '')) + a.get('joinphrase', '')
                             for a in rec.get('artist-credit', []))
            releases = rec.get("releases", []) or [{}]
            releases = sorted(releases, key=lambda r: (
                bool(hint.album) and r.get('title', '').casefold() != hint.album.casefold(),
                r.get('release-group', {}).get('primary-type') != 'Album',
                'Compilation' in r.get('release-group', {}).get('secondary-types', []),
                r.get('date') or '9999'))
            for release in releases[:5]:
                tags = rec.get('tags', [])
                genre = max(tags, key=lambda t: t.get('count', 0)).get('name', '') if tags else ''
                results.append(Metadata(rec.get('title', ''), artist, release.get('title', ''),
                                        (release.get('date') or rec.get('first-release-date') or '')[:4],
                                        genre, release.get('id', ''), int(rec.get('score', 0)),
                                        rec['length'] / 1000 if rec.get('length') else None,
                                        tuple(r['id'] for r in releases if r.get('id') != release.get('id')
                                              and r.get('id') and release.get('release-group', {}).get('id')
                                              and r.get('release-group', {}).get('id') == release['release-group']['id'])[:3],
                                        release.get('release-group', {}).get('id', ''),
                                        tuple(a.get('name', a.get('artist', {}).get('name', ''))
                                              for a in rec.get('artist-credit', []))))
        return results

    def cover(self, release_id: str) -> bytes | None:
        return self._cover('release', release_id)

    def cover_group(self, group_id: str) -> bytes | None:
        return self._cover('release-group', group_id)

    def _cover(self, entity: str, entity_id: str) -> bytes | None:
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', entity_id):
            return None
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cover'
            try:
                self.download(f'https://coverartarchive.org/{entity}/{entity_id}/front-500',
                              path, limit=10 * 1024 * 1024, audio=False)
            except requests.HTTPError as exc:
                if exc.response is not None and exc.response.status_code == 404:
                    return None
                raise
            with Image.open(path) as img:
                img.verify()
            with Image.open(path) as img:
                output = io.BytesIO()
                img.convert('RGB').save(output, format='JPEG')
                return output.getvalue()


def read_hint(path: Path, artists=(), *, filename: str | None = None) -> Metadata:
    artist, title = filename_hint(Path(filename).stem if filename else path.stem, artists)
    title, album = split_album_hint(title)
    hint = Metadata(title=title, artist=artist, album=album)
    try:
        audio = File(path, easy=True)
        if audio is not None:
            for name, key in [('title', 'title'), ('artist', 'artist'), ('album', 'album'),
                              ('year', 'date'), ('genre', 'genre')]:
                values = audio.get(key, [])
                # WAV exposes raw ID3 frames even when File(easy=True) is used.
                if isinstance(audio.tags, ID3):
                    frame = audio.tags.get({'title': 'TIT2', 'artist': 'TPE1', 'album': 'TALB',
                                            'year': 'TDRC', 'genre': 'TCON'}[name])
                    values = frame.text if frame is not None else []
                if values:
                    value = str(values[0])
                    if name == 'year':
                        value = value[:4] if re.match(r'^\d{4}', value) else ''
                    setattr(hint, name, value)
            hint.duration = audio.info.length
    except Exception:
        pass  # FFmpeg supplies the definitive validation for unrecognized inputs.
    hint.title = clean_title(hint.title) or title
    hint.artist = canonical_artist(clean_title(hint.artist) or artist, artists)
    return hint


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value).strip(' .')
    # Most Unix filesystems limit a filename to 255 bytes, not characters.
    # Reserve room for the other component, separator, collision suffix and extension.
    value = value.encode('utf-8')[:100].decode('utf-8', errors='ignore').rstrip(' .')
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = '_' + value
    return value or 'Unknown'


def confirm_metadata(values: dict[str, str], selected: Metadata) -> Metadata:
    """Detach a release's cover if the user changes its identifying fields."""
    values = {key: clean_title(value) if key in {'title', 'artist'} else value.strip()
              for key, value in values.items()}
    if not values['title'] or not values['artist']:
        raise ValueError('제목과 아티스트를 입력하세요.')
    if values['year'] and not re.fullmatch(r'[0-9]{4}', values['year']):
        raise ValueError('발매 연도는 네 자리 숫자로 입력하세요.')
    same_release = same_identity(Metadata(**values), selected)
    return Metadata(**values, release_id=selected.release_id if same_release else '',
                    other_release_ids=selected.other_release_ids if same_release else (),
                    release_group_id=selected.release_group_id if same_release else '')


def same_identity(first: Metadata, second: Metadata) -> bool:
    def normalized(metadata, key):
        value = getattr(metadata, key)
        return (clean_title(value) if key in {'title', 'artist'} else value).strip().casefold()
    return all(normalized(first, key) == normalized(second, key)
               for key in ('title', 'artist', 'album', 'year'))


def read_embedded_cover(path: Path) -> bytes | None:
    """Keep an existing front cover when the original metadata is retained."""
    audio = File(path)
    if audio is None:
        return None
    pictures = getattr(audio, 'pictures', [])
    tags = audio.tags
    if isinstance(tags, ID3):
        pictures = tags.getall('APIC')
    if pictures:
        front = next((picture for picture in pictures if picture.type == 3), pictures[0])
        data = front.data
    elif tags is not None and tags.get('covr'):
        data = bytes(tags['covr'][0])
    elif tags is not None and tags.get('metadata_block_picture'):
        import base64
        from mutagen.flac import Picture
        data = Picture(base64.b64decode(tags['metadata_block_picture'][0], validate=True)).data
    else:
        return None
    if len(data) > 10 * 1024 * 1024:
        raise ValueError('원본 커버가 10 MiB 제한을 초과했습니다.')
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        output = io.BytesIO()
        image.convert('RGB').save(output, format='JPEG')
        return output.getvalue()


def write_tags(path: Path, meta: Metadata, cover: bytes | None):
    tags = ID3()
    tags.add(TIT2(encoding=1, text=meta.title))
    tags.add(TPE1(encoding=1, text=meta.artist))
    tags.add(TALB(encoding=1, text=meta.album))
    if meta.year:
        tags.add(TDRC(encoding=1, text=meta.year))
    if meta.genre:
        tags.add(TCON(encoding=1, text=meta.genre))
    if cover:
        tags.add(APIC(encoding=1, mime='image/jpeg', type=3, desc='Cover', data=cover))
    tags.save(path, v2_version=3)


class Library:
    """Process-locked content index and atomic output publication."""
    def __init__(self, output: Path):
        self.output = output
        output.mkdir(parents=True, exist_ok=True)
        self.index_path = output / '.id3-sit-index.json'
        self.index = self._load_index()
        self.updated_from: Path | None = None

    def _load_index(self):
        if not self.index_path.exists():
            return {}
        try:
            data = json.loads(self.index_path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or any(
                not re.fullmatch(r'[0-9a-f]{64}', key) or not isinstance(value, str)
                or '/' in value or '\\' in value or not value.lower().endswith('.mp3')
                for key, value in data.items()
            ):
                raise ValueError('invalid index format')
            return data
        except (ValueError, UnicodeError) as exc:
            raise RuntimeError(f'중복 인덱스가 손상되었습니다: {self.index_path}. '
                               '파일을 보존한 뒤 백업에서 복구하거나 다른 저장 폴더를 선택하세요.') from exc

    @contextmanager
    def _locked(self):
        # The persistent file is deliberately never unlinked: removing it can
        # let another process lock a different inode while a worker still owns it.
        with (self.output / '.id3-sit.lock').open('a+b') as lock:
            if lock.tell() == 0:
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError('이 저장 폴더를 다른 작업이 사용 중입니다. 잠시 후 재시도하세요.') from exc
            try:
                yield
            finally:
                lock.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _save_index(self, data):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.output,
                                             prefix='.id3-sit-index-', suffix='.tmp',
                                             delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.index_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def process(self, source: Path, meta: Metadata, cover=None, progress=lambda p: None,
                *, update_duplicate=False):
        meta = replace(meta, title=clean_title(meta.title), artist=clean_title(meta.artist))
        if not meta.title.strip() or not meta.artist.strip():
            raise ValueError('제목과 아티스트는 필수입니다.')
        ffmpeg = shutil.which('ffmpeg')
        if not ffmpeg:
            raise RuntimeError('FFmpeg를 찾을 수 없습니다. 설치 후 PATH를 확인하세요.')
        with self._locked():
            self.updated_from = None
            # Another instance may have updated the index since construction.
            self.index = self._load_index()
            return self._process(source.resolve(), meta, cover, progress, ffmpeg, update_duplicate)

    def _fingerprint(self, source: Path, ffmpeg: str):
        # Let FFmpeg hash decoded PCM as a stream; no large PCM temp file is needed.
        result = self._run([ffmpeg, '-nostdin', '-v', 'error',
                            '-protocol_whitelist', 'file,pipe', '-i', str(source),
                            '-map', '0:a:0', '-vn', '-ac', '2', '-ar', '44100',
                            '-c:a', 'pcm_s16le', '-f', 'hash', '-hash', 'sha256', '-'])
        match = re.fullmatch(r'SHA256=([0-9a-f]{64})\s*', result)
        if not match:
            raise RuntimeError('FFmpeg 오디오 지문을 읽을 수 없습니다.')
        return match.group(1)

    def _process(self, source, meta, cover, progress, ffmpeg, update_duplicate=False):
        key = self._fingerprint(source, ffmpeg)
        known = self.index.get(key)
        previous = self.output / known if known and (self.output / known).is_file() else None
        if previous:
            if not update_duplicate:
                progress(1)
                return previous, True
            # Retag the existing MP3 so repeated imports do not undo corrections
            # or encode the same audio a second time.
            source = previous
        with tempfile.TemporaryDirectory(dir=self.output) as folder:
            progress(0.4)
            converted = Path(folder) / 'converted.mp3'
            try:
                already_mp3 = isinstance(File(source), MP3)
            except Exception:
                already_mp3 = False
            codec = ['-c:a', 'copy'] if already_mp3 else ['-c:a', 'libmp3lame', '-q:a', '2']
            self._run([ffmpeg, '-nostdin', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                       '-i', str(source), '-map', '0:a:0', '-vn', '-map_metadata', '-1',
                       *codec, str(converted)])
            write_tags(converted, meta, cover)
            # Also recognize a generated MP3 if the user imports it later.
            output_key = self._fingerprint(converted, ffmpeg)
            progress(0.9)
            stem = f'{safe_name(meta.artist)} - {safe_name(meta.title)}'
            backup = Path(folder) / 'previous.mp3'
            if previous:
                shutil.copyfile(previous, backup)
            target = self.output / f'{stem}.mp3'
            n = 2
            while target != previous:
                try:
                    # Exclusive reservation avoids overwriting an existing track.
                    with target.open('xb'):
                        pass
                    break
                except FileExistsError:
                    target = self.output / f'{stem} ({n}).mp3'
                    n += 1
            try:
                os.replace(converted, target)
                updated = {k: target.name if value == known and previous else value
                           for k, value in self.index.items()}
                updated[key] = updated[output_key] = target.name
                self._save_index(updated)
            except Exception:
                if previous and target == previous:
                    os.replace(backup, previous)
                else:
                    target.unlink(missing_ok=True)
                raise
            if previous and previous != target:
                previous.unlink()
            self.updated_from = previous
            self.index = updated
            progress(1)
            return target, bool(previous)

    @staticmethod
    def _run(args):
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        result = subprocess.run(args, capture_output=True, text=True, timeout=1800,
                                encoding='utf-8', errors='replace', creationflags=flags)
        if result.returncode:
            raise RuntimeError('FFmpeg 변환 실패: ' + result.stderr[-1800:])
        return result.stdout
