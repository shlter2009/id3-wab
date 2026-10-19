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
