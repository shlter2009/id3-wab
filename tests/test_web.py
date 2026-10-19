import io
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

import pytest
from mutagen.id3 import ID3

from music_manager.core import Metadata
from music_manager.web import PublicNetwork, create_app

HEADERS = {'X-Requested-With': 'ID3Sit'}
META = {'title': 'Song', 'artist': 'Artist', 'album': 'Album', 'year': '2024', 'genre': 'Rock'}


@pytest.fixture
def audio(tmp_path):
    file = tmp_path / 'Artist - Song.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.2', str(file)], check=True)
    return file


@pytest.fixture
def app(tmp_path, audio):
    class FakeNetwork:
        def search(self, hint):
            return [Metadata(**META)]
        def download(self, url, path, progress=lambda p: None):
            shutil.copyfile(audio, path)
            progress(1)
        def cover(self, release_id):
            return None
    app = create_app({'TESTING': True, 'SECRET_KEY': 'test-secret', 'DATA_ROOT': tmp_path / 'data',
                      'DOWNLOAD_HOSTS': ['example.com'], 'WEB_PASSWORD': ''}, FakeNetwork)
    yield app
    app.extensions['jobs'].executor.shutdown(wait=True)


def upload(client, data, name='Artist - Song.wav'):
    return client.post('/api/jobs/upload', data={'file': (io.BytesIO(data), name)}, headers=HEADERS)


def wait_for(client, job_id, status, timeout=15):
    limit = time.monotonic() + timeout
    while time.monotonic() < limit:
        jobs = client.get('/api/jobs').json['jobs']
        job = next(j for j in jobs if j['id'] == job_id)
        if job['status'] == status:
            return job
        if job['status'] == 'failed' and status != 'failed':
            pytest.fail(job['error'])
        time.sleep(.025)
    pytest.fail(f'Job did not reach {status}')


def confirm(client, job_id, metadata=None, candidate=0):
    return client.post(f'/api/jobs/{job_id}/confirm', json={'metadata': metadata or META, 'candidate': candidate}, headers=HEADERS)


def test_static_site_and_health(app):
    client = app.test_client()
    assert '음악에 정보를 입히다' in client.get('/').text
    assert client.get('/static/app.js').status_code == 200
    assert client.get('/static/style.css').status_code == 200
    assert client.get('/healthz').json['ffmpeg']
    assert "frame-ancestors 'none'" in client.get('/').headers['Content-Security-Policy']


def test_upload_confirm_download_and_duplicate(app, audio):
    client = app.test_client()
    response = upload(client, audio.read_bytes())
    assert response.status_code == 202
    job_id = response.json['job']['id']
    job = wait_for(client, job_id, 'awaiting')
    assert job['candidates'][0]['title'] == 'Song'
    assert not job['download']
    assert confirm(client, job_id).status_code == 202
    ready = wait_for(client, job_id, 'completed')
    download = client.get(ready['download'])
    assert download.status_code == 200
    assert 'attachment' in download.headers['Content-Disposition']
    tags = ID3(io.BytesIO(download.data))
    assert tags.version == (2, 3, 0)
    assert str(tags['TALB']) == 'Album'
    assert str(tags['TIT2']) == 'Song'
    second = upload(client, audio.read_bytes()).json['job']['id']
    wait_for(client, second, 'awaiting')
    confirm(client, second)
    duplicate = wait_for(client, second, 'completed')
    assert duplicate['duplicate']
    assert client.delete(f'/api/jobs/{job_id}', headers=HEADERS).status_code == 200
    assert client.get(duplicate['download']).status_code == 200
    assert client.delete(f'/api/jobs/{second}', headers=HEADERS).status_code == 200
    assert client.get('/api/jobs').json['jobs'] == []


def test_session_isolation(app, audio):
    first, second = app.test_client(), app.test_client()
    job_id = upload(first, audio.read_bytes()).json['job']['id']
    wait_for(first, job_id, 'awaiting')
    assert second.get('/api/jobs').json['jobs'] == []
    assert second.get(f'/api/jobs/{job_id}/download').status_code == 404
    assert confirm(second, job_id).status_code == 404
    assert second.delete(f'/api/jobs/{job_id}', headers=HEADERS).status_code == 404


def test_csrf_and_invalid_json(app):
    client = app.test_client()
    assert client.post('/api/jobs/url', json={}).status_code == 403
    assert client.post('/api/jobs/url', json={}, headers={**HEADERS, 'Origin': 'https://evil.example'}).status_code == 403
    assert client.post('/api/jobs/url', json=[], headers=HEADERS).status_code == 400


def test_upload_restrictions_and_sanitized_name(app, audio):
    client = app.test_client()
    assert upload(client, b'abc', 'file.html').status_code == 400
    assert upload(client, b'').status_code == 400
    response = upload(client, audio.read_bytes(), '../../Artist - Song.wav')
    assert response.status_code == 202
    job = wait_for(client, response.json['job']['id'], 'awaiting')
    assert job['name'] == 'Artist - Song.wav'
    stored = app.extensions['jobs'].jobs[job['id']]
    assert Path(stored.source).resolve().is_relative_to(Path(app.config['DATA_ROOT']).resolve())


def test_url_permission_hosts_and_import(app):
    client = app.test_client()
    assert client.post('/api/jobs/url', json={'url': 'https://example.com/song.wav'}, headers=HEADERS).status_code == 400
    assert client.post('/api/jobs/url', json={'url': 'https://youtube.com/watch?v=a', 'permission': True}, headers=HEADERS).status_code == 400
    result = client.post('/api/jobs/url', json={'url': 'https://example.com/Artist%20-%20Song.wav', 'permission': True}, headers=HEADERS)
    assert result.status_code == 202
    job_id = result.json['job']['id']
    ready = wait_for(client, job_id, 'awaiting')
    assert ready['hint']['artist'] == 'Artist'
    confirm(client, job_id)
    job = wait_for(client, job_id, 'completed')
    assert client.get(job['download']).status_code == 200


def test_failure_retry_and_confirm_state(app, audio):
    client = app.test_client()
    job_id = upload(client, b'not an audio file').json['job']['id']
    wait_for(client, job_id, 'awaiting')
    assert confirm(client, job_id).status_code == 202
    failed = wait_for(client, job_id, 'failed')
    assert 'FFmpeg' in failed['error']
    stored = app.extensions['jobs'].jobs[job_id]
    Path(stored.source).write_bytes(audio.read_bytes())
    assert client.post(f'/api/jobs/{job_id}/retry', headers=HEADERS).status_code == 202
    wait_for(client, job_id, 'awaiting')
    assert confirm(client, job_id, metadata={'title': ''}).status_code == 400
    assert confirm(client, job_id, candidate=999).status_code == 400
    assert confirm(client, job_id, metadata=dict(META, release_id='forged')).status_code == 202
    wait_for(client, job_id, 'completed')
    assert stored.metadata.release_id == ''
    assert confirm(client, job_id).status_code == 409
    assert client.post(f'/api/jobs/{job_id}/retry', headers=HEADERS).status_code == 409


def test_offline_search_keeps_manual_editing(app, audio):
    class Offline:
        def search(self, hint):
            raise ConnectionError('offline')
    app.extensions['jobs'].network_factory = Offline
    client = app.test_client()
    job_id = upload(client, audio.read_bytes()).json['job']['id']
    job = wait_for(client, job_id, 'awaiting')
    assert job['warning'] and not job['candidates']
    assert confirm(client, job_id, candidate=None).status_code == 202
    wait_for(client, job_id, 'completed')


def test_optional_password_gate(app):
    app.config['WEB_PASSWORD'] = '테스트-password'
    client = app.test_client()
    assert client.get('/').status_code == 200
    assert client.get('/api/jobs').status_code == 401
    assert client.post('/api/login', json={'password': 'bad'}, headers=HEADERS).status_code == 401
    assert client.post('/api/login', json={'password': '테스트-password'}, headers=HEADERS).status_code == 200
    assert client.get('/api/jobs').status_code == 200


def test_public_network_rejects_internal_and_unlisted_hosts(monkeypatch):
    network = PublicNetwork(['example.com'])
    with pytest.raises(ValueError, match='허용되지'):
        network.validate('https://other.example/song.mp3')
    monkeypatch.setattr('socket.getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('127.0.0.1', 443))])
    with pytest.raises(ValueError, match='내부'):
        network.validate('https://example.com/song.mp3')
    with pytest.raises(ValueError):
        network.validate('https://user:password@example.com/song.mp3')
    network.http.close()


def image_bytes():
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (60, 60), 'navy').save(stream, 'PNG')
    return stream.getvalue()


def test_artist_rules_and_research(app, audio):
    import json
    client = app.test_client()
    rules = [{'name': 'Drake', 'aliases': ['드레이크']}]
    validated = client.post('/api/artists/validate', json={'artists': rules}, headers=HEADERS)
    assert validated.status_code == 200 and validated.json['artists'] == rules
    assert client.post('/api/artists/validate', json={'artists': [*rules, {'name': 'Drake'}]}, headers=HEADERS).status_code == 400
    job_id = upload(client, audio.read_bytes(), 'Wu-Tang Forever - Drake (128k).wav').json['job']['id']
    assert wait_for(client, job_id, 'awaiting')['hint']['artist'] == 'Wu-Tang Forever'
    result = client.post(f'/api/jobs/{job_id}/research', json={'artists': rules}, headers=HEADERS)
    assert result.status_code == 202
    corrected = wait_for(client, job_id, 'awaiting')
    assert corrected['hint']['title'] == 'Wu-Tang Forever'
    assert corrected['hint']['artist'] == 'Drake'
    query = dict(META, title='Edited title', artist='Edited artist')
    result = client.post(f'/api/jobs/{job_id}/research', json={'artists': rules, 'metadata': query}, headers=HEADERS)
    assert result.status_code == 202
    corrected = wait_for(client, job_id, 'awaiting')
    assert corrected['hint']['title'] == 'Edited title'
    assert corrected['hint']['artist'] == 'Edited artist'
    result = client.post('/api/jobs/upload', data={'file': (io.BytesIO(audio.read_bytes()), 'Wu-Tang Forever - 드레이크 (128k).wav'),
                                                'artists': json.dumps(rules)}, headers=HEADERS)
    hint = wait_for(client, result.json['job']['id'], 'awaiting')['hint']
    assert hint['artist'] == 'Drake' and hint['title'] == 'Wu-Tang Forever'
    assert client.post(f'/api/jobs/{job_id}/research', json={'artists': {}, 'metadata': query}, headers=HEADERS).status_code == 400


def test_manual_cover_upload_preview_and_embedding(app, audio):
    from PIL import Image
    client = app.test_client()
    job_id = upload(client, audio.read_bytes()).json['job']['id']
    wait_for(client, job_id, 'awaiting')
    result = client.post(f'/api/jobs/{job_id}/artwork/upload', data={'file': (io.BytesIO(image_bytes()), 'cover.png')}, headers=HEADERS)
    assert result.status_code == 200
    assert result.json['job']['artwork']['source'] == '직접 올린 이미지'
    picture = client.get(f'/api/jobs/{job_id}/cover')
    assert Image.open(io.BytesIO(picture.data)).format == 'JPEG'
    assert confirm(client, job_id).status_code == 202
    ready = wait_for(client, job_id, 'completed')
    tags = ID3(io.BytesIO(client.get(ready['download']).data))
    assert tags.getall('APIC') and tags.getall('APIC')[0].mime == 'image/jpeg'
    assert client.delete(f'/api/jobs/{job_id}/artwork', headers=HEADERS).status_code == 409
    assert client.post(f'/api/jobs/{job_id}/research', json={'artists': []}, headers=HEADERS).status_code == 409


def test_cover_validation_isolation_and_explicit_removal(app, audio):
    client, stranger = app.test_client(), app.test_client()
    job_id = upload(client, audio.read_bytes()).json['job']['id']
    wait_for(client, job_id, 'awaiting')
    assert stranger.post(f'/api/jobs/{job_id}/artwork/upload', data={'file': (io.BytesIO(image_bytes()), 'cover.png')}, headers=HEADERS).status_code == 404
    assert client.post(f'/api/jobs/{job_id}/artwork/upload', data={'file': (io.BytesIO(b'bad'), 'cover.png')}, headers=HEADERS).status_code == 400
    result = client.post(f'/api/jobs/{job_id}/artwork/upload', data={'file': (io.BytesIO(image_bytes()), 'cover.png')}, headers=HEADERS)
    assert result.status_code == 200
    assert client.delete(f'/api/jobs/{job_id}/artwork', headers=HEADERS).status_code == 200
    assert not client.get('/api/jobs').json['jobs'][0]['artwork']['available']
    confirm(client, job_id)
    ready = wait_for(client, job_id, 'completed')
    assert not ID3(io.BytesIO(client.get(ready['download']).data)).getall('APIC')


def test_preview_cover_fallback_and_identity_change(app, audio):
    from PIL import Image
    data = io.BytesIO()
    Image.new('RGB', (20, 20), 'green').save(data, 'JPEG')
    calls = []
    class CoverNetwork:
        def search(self, hint):
            return [Metadata(**META, release_id='first', other_release_ids=('second',))]
        def cover(self, release_id):
            calls.append(release_id)
            return data.getvalue() if release_id == 'second' else None
    app.extensions['jobs'].network_factory = CoverNetwork
    client = app.test_client()
    job_id = upload(client, audio.read_bytes()).json['job']['id']
    wait_for(client, job_id, 'awaiting')
    result = client.post(f'/api/jobs/{job_id}/artwork/search', json={'metadata': META, 'candidate': 0}, headers=HEADERS)
    assert result.status_code == 202
    ready = wait_for(client, job_id, 'awaiting')
    assert ready['artwork']['available']
    assert ready['artwork']['source'] == '같은 앨범의 다른 발매판'
    assert calls == ['first', 'second']
    # Editing album identity must detach a previously previewed image.
    confirm(client, job_id, metadata=dict(META, album='Changed album'))
    ready = wait_for(client, job_id, 'completed')
    assert not ID3(io.BytesIO(client.get(ready['download']).data)).getall('APIC')
    assert calls == ['first', 'second']


def test_missing_artwork_is_explained_and_not_retried_implicitly(app, audio):
    calls = []
    class MissingCover:
        def search(self, hint):
            return [Metadata(**META, release_id='missing')]
        def cover(self, release_id):
            calls.append(release_id)
            return None
    app.extensions['jobs'].network_factory = MissingCover
    client = app.test_client()
    job_id = upload(client, audio.read_bytes()).json['job']['id']
    wait_for(client, job_id, 'awaiting')
    client.post(f'/api/jobs/{job_id}/artwork/search', json={'metadata': META, 'candidate': 0}, headers=HEADERS)
    ready = wait_for(client, job_id, 'awaiting')
    assert not ready['artwork']['available'] and '커버가 없습니다' in ready['artwork']['message']
    confirm(client, job_id)
    ready = wait_for(client, job_id, 'completed')
    assert '커버가 없습니다' in ready['warning']
    assert calls == ['missing']


def test_artist_aliases_keep_release_link_and_cover(app, audio):
    import json
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (20, 20), 'navy').save(stream, 'JPEG')
    calls = []
    class AliasNetwork:
        def search(self, hint):
            return [Metadata('Song', 'Drake', 'Album', '2024', 'Rock', release_id='release')]
        def cover(self, release_id):
            calls.append(release_id)
            return stream.getvalue()
    app.extensions['jobs'].network_factory = AliasNetwork
    client = app.test_client()
    rules = [{'name': '드레이크', 'aliases': ['Drake']}]
    response = client.post('/api/jobs/upload', data={'file': (io.BytesIO(audio.read_bytes()), 'Song - Drake.wav'),
                                                   'artists': json.dumps(rules)}, headers=HEADERS)
    job_id = response.json['job']['id']
    ready = wait_for(client, job_id, 'awaiting')
    assert ready['hint']['artist'] == '드레이크'
    assert ready['candidates'][0]['artist'] == '드레이크'
    metadata = dict(META, artist='Drake')
    response = client.post(f'/api/jobs/{job_id}/artwork/search', json={'metadata': metadata, 'candidate': 0}, headers=HEADERS)
    assert response.status_code == 202
    ready = wait_for(client, job_id, 'awaiting')
    assert ready['artwork']['available'] and calls == ['release']
    confirm(client, job_id, metadata=metadata)
    ready = wait_for(client, job_id, 'completed')
    tags = ID3(io.BytesIO(client.get(ready['download']).data))
    assert str(tags['TPE1']) == '드레이크'
    assert tags.getall('APIC') and calls == ['release']
