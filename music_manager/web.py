"""Local and container-hosted web interface sharing the desktop audio engine."""
from __future__ import annotations

import hmac
import io
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

from flask import Flask, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException
from PIL import Image, ImageOps

from .core import Library, Metadata, Network, confirm_metadata, read_embedded_cover, read_hint, safe_name, same_identity
from .artists import canonical_artist, validate_artists

AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.aiff', '.aif', '.wma'}
ACTIVE = {'queued', 'searching', 'converting', 'covering'}
METADATA_FIELDS = ('title', 'artist', 'album', 'year', 'genre')


class PublicNetwork(Network):
    """Restrict URL imports and redirects to configured public destinations."""
    def __init__(self, hosts):
        super().__init__()
        self.hosts = hosts
        self.http.hooks['response'].append(self._redirect)

    def validate(self, url, audio=True):
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        allowed = self.hosts if audio else ('coverartarchive.org', 'archive.org', '*.archive.org')
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or parsed.port not in {None, 80, 443} or not host):
            raise ValueError('일반 HTTP(S) 직접 오디오 URL을 입력하세요.')
        if not any(host == item or (item.startswith('*.') and host.endswith(item[1:])) for item in allowed):
            raise ValueError('이 다운로드 호스트는 허용되지 않았습니다. 파일을 업로드하거나 관리자에게 호스트 추가를 요청하세요.')
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('내부 네트워크 주소는 가져올 수 없습니다.')

    def _redirect(self, response, **kwargs):
        if response.is_redirect:
            destination = urljoin(response.url, response.headers['Location'])
            # Metadata and artwork redirects stay within their fixed destinations.
            host = (urlparse(response.url).hostname or '').lower()
            if host == 'musicbrainz.org':
                if urlparse(destination).hostname != 'musicbrainz.org':
                    raise ValueError('허용되지 않은 메타데이터 리디렉션입니다.')
            else:
                self.validate(destination, audio=not (host == 'coverartarchive.org' or host.endswith('.archive.org') or host == 'archive.org'))

    def download(self, url, target, progress=lambda p: None, limit=64 * 1024 * 1024, audio=True):
        self.validate(url, audio)
        return super().download(url, target, progress, limit, audio)


@dataclass
class Job:
    id: str
    owner: str
    name: str
    source: str
    remote: bool = False
    status: str = 'queued'
    progress: float = 0
    error: str = ''
    warning: str = ''
    hint: Metadata = field(default_factory=Metadata)
    candidates: list[Metadata] = field(default_factory=list)
    metadata: Metadata | None = None
    output: Path | None = None
    duplicate: bool = False
    has_cover: bool = False
    artists: list[dict] = field(default_factory=list)
    original_hint: Metadata = field(default_factory=Metadata)
    cover_data: bytes | None = None
    cover_for: Metadata | None = None
    cover_source: str = ''
    cover_version: int = 0
    cover_message: str = ''
    cover_disabled: bool = False
    updated: float = field(default_factory=time.time)


class Jobs:
    def __init__(self, root: Path, hosts, network_factory=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.hosts = hosts
        self.network_factory = network_factory or (lambda: PublicNetwork(hosts))
        self.jobs: dict[str, Job] = {}
        self.lock = threading.RLock()
        self.mb_lock = threading.Lock()
        self.last_mb = 0.0
        self.library_locks = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='id3-web')

    def workspace(self, owner):
        return self.root / owner

    def purge(self):
        """Expire idle sessions as a unit, preserving active jobs and their outputs."""
        cutoff = time.time() - 24 * 3600
        owners = {job.owner for job in self.jobs.values()}
        for owner in owners:
            items = [j for j in self.jobs.values() if j.owner == owner]
            if all(j.status not in ACTIVE and j.updated < cutoff for j in items):
                shutil.rmtree(self.workspace(owner), ignore_errors=True)
                for job in items:
                    del self.jobs[job.id]
                self.library_locks.pop(owner, None)
        # After a restart there are no in-memory jobs or live sessions for old files.
        for folder in self.root.iterdir():
            if (folder.is_dir() and re.fullmatch(r'[0-9a-f]{48}', folder.name)
                    and folder.name not in owners and folder.stat().st_mtime < cutoff):
                shutil.rmtree(folder, ignore_errors=True)

    def add(self, owner, name, source='', remote=False):
        with self.lock:
            self.purge()
            if len(self.jobs) >= 100 or sum(j.owner == owner for j in self.jobs.values()) >= 20:
                raise ValueError('작업 한도에 도달했습니다. 완료·실패 항목을 지운 뒤 다시 추가하세요.')
            job = Job(uuid.uuid4().hex, owner, name, source, remote)
            self.jobs[job.id] = job
            self.library_locks.setdefault(owner, threading.Lock())
            return job

    def update(self, job, **values):
        with self.lock:
            for key, value in values.items():
                setattr(job, key, value)
            job.updated = time.time()

    def view(self, job):
        with self.lock:
            return {'id': job.id, 'name': job.name, 'status': job.status,
                    'progress': job.progress, 'error': job.error, 'warning': job.warning,
                    'hint': asdict(job.hint), 'candidates': [asdict(c) for c in job.candidates],
                    'metadata': asdict(job.metadata) if job.metadata else None,
                    'duplicate': job.duplicate,
                    'artwork': {'available': bool(job.cover_data), 'source': job.cover_source,
                                'version': job.cover_version, 'message': job.cover_message,
                                'disabled': job.cover_disabled,
                                'metadata': asdict(job.cover_for) if job.cover_for else None},
                    'download': f'/api/jobs/{job.id}/download' if job.output else None,
                    'cover': f'/api/jobs/{job.id}/cover' if job.output and job.has_cover else None}

    def prepare(self, job, query=None, redownload=True):
        network = None
        try:
            self.update(job, status='searching', progress=.05, error='', warning='')
            network = self.network_factory()
            if job.remote:
                local = self.workspace(job.owner) / job.id / safe_name(job.name)
                local.parent.mkdir(parents=True, exist_ok=True)
                if redownload or not local.is_file():
                    network.download(job.source, local, lambda p: self.update(job, progress=.05 + p * .25))
            else:
                local = Path(job.source)
            original = read_hint(local, job.artists)
            hint = query or original
            self.update(job, hint=hint, original_hint=original, progress=.35)
            try:
                embedded = read_embedded_cover(local) if same_identity(hint, original) else None
            except Exception:
                embedded = None
            self.update(job, cover_data=embedded, cover_for=original if embedded else None,
                        cover_source='원본 내장 커버' if embedded else '',
                        cover_version=job.cover_version + 1, cover_message='', cover_disabled=False)
            candidates = []
            warning = ''
            try:
                with self.mb_lock:
                    time.sleep(max(0, 1.1 - (time.monotonic() - self.last_mb)))
                    self.last_mb = time.monotonic()
                    candidates = network.search(hint)
                    candidates = [replace(candidate, artist=canonical_artist(candidate.artist, job.artists))
                                  for candidate in candidates]
            except Exception:
                warning = '음악 정보 검색에 연결하지 못했습니다. 원본 정보를 확인하거나 직접 입력할 수 있습니다.'
            if job.artists and hint.artist not in {artist['name'] for artist in job.artists}:
                warning = ('아티스트 목록과 일치하는 이름을 찾지 못했습니다. 가수를 직접 확인하세요. ' + warning).strip()
            self.update(job, hint=hint, candidates=candidates, warning=warning,
                        status='awaiting', progress=.4)
        except Exception as exc:
            self.update(job, status='failed', error=str(exc))
        finally:
            if network is not None and hasattr(network, 'http'):
                network.http.close()

    @staticmethod
    def fetch_cover(network, metadata):
        errors = []
        ids = list(dict.fromkeys([metadata.release_id, *metadata.other_release_ids]))
        if not any(ids):
            return None, '', '앨범 후보를 선택한 뒤 커버를 찾거나 이미지를 직접 올려주세요.'
        for index, release_id in enumerate(ids[:4]):
            if not release_id:
                continue
            try:
                picture = network.cover(release_id)
                if picture:
                    return picture, ('선택한 발매판' if index == 0 else '같은 앨범의 다른 발매판'), ''
            except Exception:
                errors.append('온라인 커버 요청에 실패했습니다.')
        return None, '', ('온라인 커버 요청에 실패했습니다. 다시 시도하거나 이미지를 올려주세요.' if errors
                          else '해당 앨범의 커버가 없습니다. 다른 발매판을 선택하거나 이미지를 올려주세요.')

    def preview_cover(self, job, metadata):
        network = None
        try:
            network = self.network_factory()
            picture, source, message = self.fetch_cover(network, metadata)
            if picture is None and same_identity(metadata, job.original_hint):
                local = (self.workspace(job.owner) / job.id / safe_name(job.name)) if job.remote else Path(job.source)
                picture = read_embedded_cover(local)
                if picture:
                    source, message = '원본 내장 커버', ''
            self.update(job, cover_data=picture, cover_for=metadata,
                        cover_source=source, cover_message=message,
                        cover_disabled=False,
                        cover_version=job.cover_version + 1)
        except Exception:
            self.update(job, cover_data=None, cover_for=metadata, cover_source='',
                        cover_version=job.cover_version + 1,
                        cover_message='커버를 확인하지 못했습니다. 다시 시도하거나 이미지를 올려주세요.')
        finally:
            if network is not None and hasattr(network, 'http'):
                network.http.close()
            self.update(job, status='awaiting', progress=.4)

    def convert(self, job):
        network = None
        try:
            network = self.network_factory()
            local = (self.workspace(job.owner) / job.id / safe_name(job.name)) if job.remote else Path(job.source)
            cover = job.cover_data if (job.cover_source == '직접 올린 이미지' or
                    job.cover_for and same_identity(job.metadata, job.cover_for)) else None
            warnings = []
            checked = bool(job.cover_for and same_identity(job.metadata, job.cover_for))
            if not cover and checked and job.cover_message:
                warnings.append(job.cover_message)
            if cover is None and job.metadata.release_id and not checked and not job.cover_disabled:
                try:
                    cover, source, message = self.fetch_cover(network, job.metadata)
                    if cover:
                        self.update(job, cover_source=source)
                    elif message:
                        warnings.append(message)
                except Exception:
                    warnings.append('온라인 커버를 가져오지 못했습니다.')
            if cover is None and not checked and not job.cover_disabled and same_identity(job.metadata, job.original_hint):
                try:
                    cover = read_embedded_cover(local)
                except Exception:
                    warnings.append('원본 커버를 읽지 못했습니다.')
            if cover is None and not warnings and not job.cover_disabled:
                warnings.append('사용 가능한 커버가 없어 커버 없이 저장했습니다.')
            # Serialize library writes for a session; the core also takes an OS lock.
            with self.library_locks[job.owner]:
                library = Library(self.workspace(job.owner) / 'library')
                output, duplicate = library.process(local, job.metadata, cover,
                    lambda p: self.update(job, progress=.45 + p * .55))
            self.update(job, output=output, duplicate=duplicate, status='completed', progress=1,
                        metadata=read_hint(output), has_cover=bool(read_embedded_cover(output)),
                        warning=' '.join(warnings))
        except Exception as exc:
            self.update(job, status='failed', error=str(exc))
        finally:
            if network is not None and hasattr(network, 'http'):
                network.http.close()


def create_app(config=None, network_factory=None):
    app = Flask(__name__, static_folder='static', static_url_path='/static')
    app.config.update(SECRET_KEY=os.environ.get('ID3_WEB_SECRET') or secrets.token_hex(32),
                      MAX_CONTENT_LENGTH=66 * 1024 * 1024,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      SESSION_COOKIE_SECURE=os.environ.get('ID3_HTTPS', '').lower() == 'true',
                      WEB_PASSWORD=os.environ.get('ID3_WEB_PASSWORD', ''),
                      DATA_ROOT=os.environ.get('ID3_WEB_DATA', 'web-data'),
                      DOWNLOAD_HOSTS=os.environ.get('ID3_DOWNLOAD_HOSTS',
                          'archive.org,*.archive.org,upload.wikimedia.org').split(','))
    if config:
        app.config.update(config)
    jobs = Jobs(Path(app.config['DATA_ROOT']).resolve(),
                [h.strip().lower() for h in app.config['DOWNLOAD_HOSTS'] if h.strip()], network_factory)
    app.extensions['jobs'] = jobs

    @app.before_request
    def protect():
        if request.path.startswith('/api/'):
            if request.method != 'GET':
                if request.headers.get('X-Requested-With') != 'ID3Sit':
                    return jsonify(error='잘못된 요청입니다. 웹 화면에서 다시 시도하세요.'), 403
                origin = request.headers.get('Origin')
                if origin and urlparse(origin).netloc != request.host:
                    return jsonify(error='다른 사이트의 요청은 허용되지 않습니다.'), 403
            if app.config['WEB_PASSWORD'] and not session.get('authenticated') and request.path != '/api/login':
                return jsonify(error='사이트 비밀번호를 입력하세요.', login_required=True), 401
            if 'owner' not in session:
                session['owner'] = secrets.token_hex(24)

    @app.after_request
    def headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; media-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        if request.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    @app.errorhandler(Exception)
    def error(exc):
        if isinstance(exc, HTTPException):
            if exc.code == 413:
                return jsonify(error='한 파일당 최대 64 MiB까지 업로드할 수 있습니다.'), 413
            return jsonify(error=exc.description), exc.code
        if isinstance(exc, ValueError):
            return jsonify(error=str(exc)), 400
        app.logger.exception('Web request failed')
        return jsonify(error='요청을 처리하지 못했습니다. 잠시 후 다시 시도하세요.'), 500

    def owned(job_id):
        with jobs.lock:
            job = jobs.jobs.get(job_id)
            if job is None or job.owner != session['owner']:
                from werkzeug.exceptions import NotFound
                raise NotFound('작업을 찾을 수 없습니다.')
            return job

    @app.get('/')
    def index():
        return app.send_static_file('index.html')

    @app.get('/healthz')
    def health():
        return jsonify(status='ok', ffmpeg=bool(shutil.which('ffmpeg'))), 200 if shutil.which('ffmpeg') else 503

    def json_body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('JSON 객체를 입력하세요.')
        return data

    def artist_rules(value):
        return validate_artists(value)

    def chosen_metadata(job, data):
        values = data.get('metadata', {})
        if not isinstance(values, dict) or any(not isinstance(values.get(k), str) or len(values[k]) > 1000 for k in METADATA_FIELDS):
            raise ValueError('메타데이터 입력을 확인하세요.')
        index = data.get('candidate')
        if index is not None and (type(index) is not int or not 0 <= index < len(job.candidates)):
            raise ValueError('선택한 검색 결과가 올바르지 않습니다.')
        values = {k: values[k] for k in METADATA_FIELDS}
        values['artist'] = canonical_artist(values['artist'], job.artists)
        selected = job.candidates[index] if index is not None else job.hint
        selected = replace(selected, artist=canonical_artist(selected.artist, job.artists))
        return confirm_metadata(values, selected)

    @app.post('/api/artists/validate')
    def validate_artist_list():
        return jsonify(artists=artist_rules(json_body().get('artists', [])))

    @app.post('/api/login')
    def login():
        data = json_body()
        if not hmac.compare_digest(str(data.get('password', '')).encode('utf-8'), app.config['WEB_PASSWORD'].encode('utf-8')):
            return jsonify(error='비밀번호가 일치하지 않습니다.'), 401
        session.clear()
        session['authenticated'] = True
        session['owner'] = secrets.token_hex(24)
        return jsonify(ok=True)

    @app.get('/api/jobs')
    def listing():
        with jobs.lock:
            return jsonify(jobs=[jobs.view(j) for j in jobs.jobs.values() if j.owner == session['owner']],
                           allowed_hosts=jobs.hosts)

    @app.post('/api/jobs/upload')
    def upload():
        upload = request.files.get('file')
        if not upload or not upload.filename:
            raise ValueError('오디오 파일을 선택하세요.')
        original = Path(upload.filename.replace('\\', '/')).name
        suffix = Path(original).suffix.lower()
        if suffix not in AUDIO_EXTENSIONS:
            raise ValueError('지원하는 오디오 파일 형식이 아닙니다.')
        name = safe_name(Path(original).stem) + suffix
        try:
            artists = artist_rules(json.loads(request.form.get('artists', '[]')))
        except json.JSONDecodeError as exc:
            raise ValueError('아티스트 목록 형식이 올바르지 않습니다.') from exc
        job = jobs.add(session['owner'], name)
        job.artists = artists
        folder = jobs.workspace(job.owner) / job.id
        local = folder / name
        try:
            folder.mkdir(parents=True)
            size = 0
            with local.open('xb') as handle:
                while chunk := upload.stream.read(65536):
                    size += len(chunk)
                    if size > 64 * 1024 * 1024:
                        raise ValueError('한 파일당 최대 64 MiB까지 업로드할 수 있습니다.')
                    handle.write(chunk)
            if not size:
                raise ValueError('빈 파일입니다.')
            job.source = str(local)
        except Exception:
            with jobs.lock:
                jobs.jobs.pop(job.id, None)
            shutil.rmtree(folder, ignore_errors=True)
            raise
        jobs.executor.submit(jobs.prepare, job)
        return jsonify(job=jobs.view(job)), 202

    @app.post('/api/jobs/url')
    def import_url():
        data = json_body()
        url = str(data.get('url', '')).strip()
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower().rstrip('.')
        if data.get('permission') is not True:
            raise ValueError('다운로드·변환 권한을 확인하세요.')
        if parsed.scheme not in {'http', 'https'} or not host:
            raise ValueError('HTTP(S) 직접 오디오 URL을 입력하세요.')
        # Fail unsupported hosts without occupying a worker or making any request.
        if not any(host == h or h.startswith('*.') and host.endswith(h[1:]) for h in jobs.hosts):
            raise ValueError('허용되지 않은 호스트입니다. 파일 업로드를 이용하세요.')
        name = safe_name(Path(unquote(parsed.path)).name or 'download.audio')
        artists = artist_rules(data.get('artists', []))
        job = jobs.add(session['owner'], name, url, remote=True)
        job.artists = artists
        jobs.executor.submit(jobs.prepare, job)
        return jsonify(job=jobs.view(job)), 202

    @app.post('/api/jobs/<job_id>/confirm')
    def confirm(job_id):
        job = owned(job_id)
        data = json_body()
        with jobs.lock:
            if job.status != 'awaiting':
                return jsonify(error='현재 작업은 메타데이터 확인 단계가 아닙니다.'), 409
            meta = chosen_metadata(job, data)
            jobs.update(job, metadata=meta, status='converting', progress=.45, error='')
        jobs.executor.submit(jobs.convert, job)
        return jsonify(job=jobs.view(job)), 202

    @app.post('/api/jobs/<job_id>/retry')
    def retry(job_id):
        job = owned(job_id)
        data = request.get_json(silent=True)
        if data is not None and not isinstance(data, dict):
            raise ValueError('JSON 객체를 입력하세요.')
        artists = artist_rules(data['artists']) if data and 'artists' in data else job.artists
        with jobs.lock:
            if job.status != 'failed':
                return jsonify(error='실패한 작업만 재시도할 수 있습니다.'), 409
            jobs.update(job, artists=artists, status='queued', progress=0, error='')
        jobs.executor.submit(jobs.prepare, job)
        return jsonify(job=jobs.view(job)), 202

    @app.post('/api/jobs/<job_id>/research')
    def research(job_id):
        job, data = owned(job_id), json_body()
        artists = artist_rules(data.get('artists', []))
        query = None
        if data.get('metadata') is not None:
            values = data['metadata']
            if not isinstance(values, dict) or any(not isinstance(values.get(k), str) or len(values[k]) > 1000 for k in METADATA_FIELDS):
                raise ValueError('검색할 곡 정보를 확인하세요.')
            if not values['title'].strip():
                raise ValueError('검색할 곡 제목을 입력하세요.')
            query = Metadata(**{k: values[k].strip() for k in METADATA_FIELDS})
            query.artist = canonical_artist(query.artist, artists)
        with jobs.lock:
            if job.status != 'awaiting':
                return jsonify(error='정보 확인 대기 중인 곡만 다시 검색할 수 있습니다.'), 409
            jobs.update(job, artists=artists, status='queued', candidates=[], error='')
        jobs.executor.submit(jobs.prepare, job, query, False)
        return jsonify(job=jobs.view(job)), 202

    @app.post('/api/jobs/<job_id>/artwork/search')
    def find_artwork(job_id):
        job, data = owned(job_id), json_body()
        with jobs.lock:
            if job.status != 'awaiting':
                return jsonify(error='현재 작업이 끝난 뒤 커버를 확인하세요.'), 409
            metadata = chosen_metadata(job, data)
            jobs.update(job, status='covering', cover_message='')
        jobs.executor.submit(jobs.preview_cover, job, metadata)
        return jsonify(job=jobs.view(job)), 202

    @app.post('/api/jobs/<job_id>/artwork/upload')
    def upload_artwork(job_id):
        job = owned(job_id)
        if job.status != 'awaiting':
            return jsonify(error='현재 작업이 끝난 뒤 이미지를 올려주세요.'), 409
        image_file = request.files.get('file')
        if not image_file:
            raise ValueError('이미지 파일을 선택하세요.')
        data = image_file.stream.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise ValueError('커버 이미지는 최대 10 MiB입니다.')
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 16_000_000:
                    raise ValueError('JPEG·PNG·WebP 이미지와 최대 1,600만 화소를 지원합니다.')
                image.verify()
            with Image.open(io.BytesIO(data)) as image:
                image = ImageOps.exif_transpose(image).convert('RGB')
                image.thumbnail((1024, 1024))
                result = io.BytesIO()
                image.save(result, format='JPEG', quality=88)
        except (OSError, Image.DecompressionBombError) as exc:
            raise ValueError('이미지를 읽지 못했습니다. JPEG·PNG·WebP 파일을 선택하세요.') from exc
        with jobs.lock:
            if job.status != 'awaiting':
                return jsonify(error='현재 작업이 끝난 뒤 이미지를 올려주세요.'), 409
            jobs.update(job, cover_data=result.getvalue(), cover_for=None, cover_source='직접 올린 이미지',
                        cover_disabled=False, cover_message='', cover_version=job.cover_version + 1)
        return jsonify(job=jobs.view(job))

    @app.delete('/api/jobs/<job_id>/artwork')
    def remove_artwork(job_id):
        job = owned(job_id)
        with jobs.lock:
            if job.status != 'awaiting':
                return jsonify(error='현재 작업이 끝난 뒤 커버를 변경하세요.'), 409
            jobs.update(job, cover_data=None, cover_for=None, cover_source='', cover_disabled=True,
                        cover_message='커버 없이 저장하도록 선택했습니다.', cover_version=job.cover_version + 1)
        return jsonify(job=jobs.view(job))

    @app.delete('/api/jobs/<job_id>')
    def remove(job_id):
        job = owned(job_id)
        with jobs.lock:
            if job.status in ACTIVE:
                return jsonify(error='진행 중인 작업은 완료 후 삭제할 수 있습니다.'), 409
            del jobs.jobs[job.id]
            shutil.rmtree(jobs.workspace(job.owner) / job.id, ignore_errors=True)
            # Outputs are shared for duplicate detection; delete only unreferenced ones.
            if job.output and not any(j.output == job.output for j in jobs.jobs.values()):
                job.output.unlink(missing_ok=True)
            if not any(j.owner == job.owner for j in jobs.jobs.values()):
                shutil.rmtree(jobs.workspace(job.owner), ignore_errors=True)
                jobs.library_locks.pop(job.owner, None)
        return jsonify(ok=True)

    @app.get('/api/jobs/<job_id>/download')
    def download(job_id):
        job = owned(job_id)
        if job.status != 'completed' or not job.output or not job.output.is_file():
            return jsonify(error='아직 다운로드할 파일이 없습니다.'), 409
        jobs.update(job)
        return send_file(job.output, mimetype='audio/mpeg', as_attachment=request.args.get('play') != '1',
                         download_name=job.output.name, conditional=True)

    @app.get('/api/jobs/<job_id>/cover')
    def cover(job_id):
        job = owned(job_id)
        if job.status != 'completed' and job.cover_data:
            return send_file(io.BytesIO(job.cover_data), mimetype='image/jpeg')
        if not job.output:
            return '', 404
        picture = read_embedded_cover(job.output)
        return send_file(io.BytesIO(picture), mimetype='image/jpeg') if picture else ('', 404)

    return app


def main():
    from waitress import serve
    host = os.environ.get('HOST', '127.0.0.1')
    port = int(os.environ.get('PORT', '8000'))
    serve(create_app(), host=host, port=port, threads=6, max_request_body_size=66 * 1024 * 1024)


if __name__ == '__main__':
    main()
