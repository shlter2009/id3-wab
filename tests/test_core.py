import io
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from mutagen.id3 import ID3
from PIL import Image

from music_manager.core import Library, Metadata, Network, confirm_metadata, read_hint, safe_name, write_tags


@pytest.fixture
def wav(tmp_path):
    if not shutil.which('ffmpeg'):
        pytest.skip('FFmpeg is required for audio integration tests')
    path = tmp_path / 'Artist - Song.wav'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:duration=0.3', str(path)], check=True)
    return path


def cover():
    out = io.BytesIO()
    Image.new('RGB', (32, 32), 'blue').save(out, 'JPEG')
    return out.getvalue()


def test_conversion_tags_cover_and_duplicate(tmp_path, wav):
    library = Library(tmp_path / 'out')
    meta = Metadata('노래', '가수', '앨범', '2024', 'Rock')
    path, duplicate = library.process(wav, meta, cover())
    assert not duplicate
    assert path.name == '가수 - 노래.mp3'
    tags = ID3(path)
    assert tags.version == (2, 3, 0)
    assert str(tags['TIT2']) == '노래'
    assert str(tags['TPE1']) == '가수'
    assert str(tags['TALB']) == '앨범'
    assert str(tags['TDRC']) == '2024'
    assert str(tags['TCON']) == 'Rock'
    assert tags.getall('APIC')[0].data == cover()
    assert read_hint(path).duration > 0
    # The duplicate index survives a new process / Library instance.
    second, duplicate = Library(library.output).process(wav, Metadata('Other', 'Other'))
    assert duplicate and second == path
    assert len(list(library.output.glob('*.mp3'))) == 1


def test_collision_and_original_preservation(tmp_path, wav):
    output = tmp_path / 'out'
    output.mkdir()
    existing = output / 'Artist - Song.mp3'
    existing.write_bytes(b'existing user file')
    original = wav.read_bytes()
    path, _ = Library(output).process(wav, Metadata('Song', 'Artist'))
    assert path.name == 'Artist - Song (2).mp3'
    assert existing.read_bytes() == b'existing user file'
    assert wav.read_bytes() == original


def test_bad_audio_does_not_publish(tmp_path):
    bad = tmp_path / 'bad.wav'
    bad.write_text('not audio')
    library = Library(tmp_path / 'out')
    with pytest.raises(RuntimeError, match='FFmpeg'):
        library.process(bad, Metadata('Song', 'Artist'))
    # Only the persistent OS lock file may remain after a failed operation.
    assert {p.name for p in library.output.iterdir()} == {'.id3-sit.lock'}


def test_required_fields(tmp_path):
    with pytest.raises(ValueError, match='필수'):
        Library(tmp_path).process(tmp_path / 'missing', Metadata())


@pytest.mark.parametrize('text', ['../hello', 'a:b/c\\d', 'CON', 'NUL', '  ...'])
def test_safe_names(text):
    name = safe_name(text)
    assert name and '/' not in name and '\\' not in name and ':' not in name
    assert not name.endswith(('.', ' '))
    assert name.upper() not in {'CON', 'NUL'}


@pytest.mark.parametrize('url', ['https://youtube.com/watch?v=x', 'https://youtu.be/x',
                                  'https://x.googlevideo.com/a', 'file:///tmp/a',
                                  'https://user:password@example.com/a'])
def test_unsupported_urls(url, tmp_path):
    with pytest.raises(ValueError):
        Network().download(url, tmp_path / 'file')


@pytest.fixture
def server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == '/missing':
                self.send_error(404)
                return
            body = b'<html>page</html>' if self.path == '/page' else b'RIFF audio bytes'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html' if self.path == '/page' else 'audio/wav')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_port}'
    httpd.shutdown()
    thread.join()
    httpd.server_close()


def test_download_validation_and_recovery(server, tmp_path):
    network = Network()
    with pytest.raises(ValueError, match='웹페이지'):
        network.download(server + '/page', tmp_path / 'page')
    with pytest.raises(Exception):
        network.download(server + '/missing', tmp_path / 'missing')
    with pytest.raises(ValueError, match='크기'):
        network.download(server + '/audio', tmp_path / 'big', limit=2)
    target = tmp_path / 'good'
    network.download(server + '/audio', target)
    assert target.read_bytes() == b'RIFF audio bytes'


def test_search_preserves_alternative_releases(monkeypatch):
    network = Network()
    captured = []
    def fake_json(url, params):
        captured.append(params)
        return {'recordings': [{'title': 'Song', 'score': '98', 'length': 180000,
                               'artist-credit': [{'name': 'Artist'}],
                               'releases': [{'id': 'a', 'title': 'Album', 'date': '2020-01-01'},
                                            {'id': 'b', 'title': 'Compilation'}]}]}
    monkeypatch.setattr(network, 'json', fake_json)
    results = network.search(Metadata('Song', 'Artist'))
    assert len(results) == 2
    assert results[0].year == '2020'
    assert results[0].duration == 180
    assert results[0].artist == 'Artist'
    assert 'artist:' in captured[0]['query']


def test_cover_validation(monkeypatch):
    network = Network()
    def download(url, path, **kwargs):
        path.write_bytes(cover())
    monkeypatch.setattr(network, 'download', download)
    image = network.cover('12345678-1234-1234-1234-123456789012')
    assert Image.open(io.BytesIO(image)).format == 'JPEG'
    assert network.cover('../unsafe') is None


def test_retry_policy_and_timeouts():
    network = Network()
    policy = network.http.get_adapter('https://').max_retries
    assert policy.total == 3
    assert 429 in policy.status_forcelist
    assert policy.allowed_methods == ['GET']




def test_long_unicode_filename_and_reserved_windows_names(tmp_path, wav):
    library = Library(tmp_path / 'out')
    target, _ = library.process(wav, Metadata('노래' * 100, '가수' * 100))
    assert len(target.name.encode('utf-8')) <= 220
    assert str(ID3(target)['TIT2']) == '노래' * 100
    assert safe_name('CON.txt') == '_CON.txt'
    assert safe_name('LPT1.data') == '_LPT1.data'
    assert safe_name('😀' * 200).encode('utf-8').decode('utf-8')


@pytest.mark.parametrize('contents', ['[]', '{broken', '{"bad-key":"song.mp3"}',
                                      '{"' + 'a' * 64 + '": "../song.mp3"}'])
def test_invalid_index_is_preserved_with_actionable_error(tmp_path, contents):
    index = tmp_path / '.id3-sit-index.json'
    index.write_text(contents, encoding='utf-8')
    with pytest.raises(RuntimeError, match='인덱스가 손상'):
        Library(tmp_path)
    assert index.read_text(encoding='utf-8') == contents


def test_index_utf8_and_rollback_on_disk_error(tmp_path, wav, monkeypatch):
    import os
    library = Library(tmp_path / 'out')
    target, _ = library.process(wav, Metadata('노래', '가수'))
    index_before = library.index_path.read_bytes()
    assert '가수' in index_before.decode('utf-8')
    index_state = dict(library.index)
    # Remove only this test's generated output to force a fresh publication.
    target.unlink()
    real_replace = os.replace

    def fail_index(source, destination):
        if destination == library.index_path:
            raise OSError('simulated disk failure')
        return real_replace(source, destination)
    monkeypatch.setattr(os, 'replace', fail_index)
    with pytest.raises(OSError, match='disk failure'):
        library.process(wav, Metadata('New title', 'New artist'))
    assert library.index_path.read_bytes() == index_before
    assert library.index == index_state
    assert not list(library.output.glob('*.mp3'))
    assert not list(library.output.glob('*.tmp'))
    monkeypatch.setattr(os, 'replace', real_replace)
    result, duplicate = library.process(wav, Metadata('New title', 'New artist'))
    assert result.is_file() and not duplicate


def test_instances_refresh_index_and_lock_output(tmp_path, wav):
    library = Library(tmp_path / 'out')
    other = Library(library.output)
    with library._locked():
        with pytest.raises(RuntimeError, match='사용 중'):
            other.process(wav, Metadata('Song', 'Artist'))
    result, _ = library.process(wav, Metadata('Song', 'Artist'))
    target, duplicate = other.process(wav, Metadata('Other name', 'Other artist'))
    assert duplicate and target == result
    assert len(list(library.output.glob('*.mp3'))) == 1


def test_generated_mp3_reimport_is_duplicate(tmp_path, wav):
    library = Library(tmp_path / 'out')
    target, _ = library.process(wav, Metadata('Song', 'Artist'))
    output, duplicate = library.process(target, Metadata('Other title', 'Artist'))
    assert duplicate and output == target
    assert len(list(library.output.glob('*.mp3'))) == 1


def test_tagless_audio_duration_and_date_hint(tmp_path, wav):
    assert read_hint(wav).duration == pytest.approx(.3, abs=.02)
    target, _ = Library(tmp_path / 'out').process(wav, Metadata('Song', 'Artist', year='2024-03-01'))
    assert read_hint(target).year == '2024'


def test_edited_release_identity_drops_stale_cover():
    from dataclasses import asdict
    candidate = Metadata('Song', 'Artist', 'Album', '2024', 'Rock', 'release-id')
    values = {key: asdict(candidate)[key] for key in ('title', 'artist', 'album', 'year', 'genre')}
    assert confirm_metadata(values, candidate).release_id == 'release-id'
    assert confirm_metadata(dict(values, genre='Pop'), candidate).release_id == 'release-id'
    for field in ('title', 'artist', 'album', 'year'):
        assert confirm_metadata(dict(values, **{field: '2025' if field == 'year' else 'Changed'}), candidate).release_id == ''
    for invalid in ('２０２４', '2024-03', 'abcd'):
        with pytest.raises(ValueError, match='네 자리'):
            confirm_metadata(dict(values, year=invalid), candidate)


def test_musicbrainz_first_retry_is_rate_limited():
    retry = Network().http.get_adapter('https://musicbrainz.org/').max_retries
    retry = retry.increment(method='GET', error=ConnectionError('offline'))
    assert retry.get_backoff_time() >= 1.1


def test_ffmpeg_does_not_follow_network_playlists(tmp_path, server):
    playlist = tmp_path / 'audio.m3u8'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n' +
                        server + '/audio.ts\n#EXT-X-ENDLIST\n', encoding='utf-8')
    library = Library(tmp_path / 'out')
    with pytest.raises(RuntimeError, match='not on whitelist'):
        library.process(playlist, Metadata('Song', 'Artist'))
    assert not list(library.output.glob('*.mp3'))




def test_wav_id3_hints_and_embedded_art(wav):
    from mutagen.wave import WAVE
    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1
    from music_manager.core import read_embedded_cover
    audio = WAVE(wav)
    audio.add_tags()
    audio.tags.add(TIT2(encoding=1, text='Tagged title'))
    audio.tags.add(TPE1(encoding=1, text='Tagged artist'))
    audio.tags.add(TALB(encoding=1, text='Tagged album'))
    audio.tags.add(TDRC(encoding=1, text='2023-04-05'))
    audio.tags.add(APIC(encoding=1, mime='image/jpeg', type=3, data=cover()))
    audio.save()
    hint = read_hint(wav)
    assert (hint.title, hint.artist, hint.album, hint.year) == ('Tagged title', 'Tagged artist', 'Tagged album', '2023')
    assert Image.open(io.BytesIO(read_embedded_cover(wav))).size == (32, 32)


def test_registered_artist_and_existing_tag_priority(tmp_path, wav):
    from music_manager.artists import validate_artists
    rules = validate_artists([{'name': 'Drake', 'aliases': ['드레이크']}])
    reversed_name = tmp_path / 'Wu-Tang Forever - Drake (128k).wav'
    shutil.copyfile(wav, reversed_name)
    hint = read_hint(reversed_name, rules)
    assert hint.artist == 'Drake' and hint.title == 'Wu-Tang Forever'
    target, _ = Library(tmp_path / 'out').process(wav, Metadata('Actual tag title', '드레이크'))
    hint = read_hint(target, rules)
    assert hint.artist == 'Drake' and hint.title == 'Actual tag title'


def test_existing_mp3_audio_packets_are_not_reencoded(tmp_path, wav):
    original = tmp_path / '128k.mp3'
    subprocess.run(['ffmpeg', '-v', 'error', '-i', str(wav), '-c:a', 'libmp3lame', '-b:a', '128k', str(original)], check=True)
    def packet_hash(path):
        return subprocess.run(['ffmpeg', '-v', 'error', '-i', str(path), '-map', '0:a:0', '-c:a', 'copy',
                               '-f', 'hash', '-hash', 'sha256', '-'], capture_output=True, text=True, check=True).stdout
    before = packet_hash(original)
    target, _ = Library(tmp_path / 'out').process(original, Metadata('New tags', 'Artist'), cover())
    assert packet_hash(target) == before
    assert ID3(target).version == (2, 3, 0)
    assert ID3(target).getall('APIC')


def test_cover_alternatives_are_limited_to_same_release_group(monkeypatch):
    network = Network()
    monkeypatch.setattr(network, 'json', lambda *a, **kw: {'recordings': [{
        'title': 'Song', 'artist-credit': [{'name': 'Artist'}], 'releases': [
            {'id': 'first', 'title': 'Album', 'release-group': {'id': 'same', 'primary-type': 'Album'}},
            {'id': 'second', 'title': 'Album', 'release-group': {'id': 'same', 'primary-type': 'Album'}},
            {'id': 'other', 'title': 'Album', 'release-group': {'id': 'other', 'primary-type': 'Album'}}]}]})
    candidates = network.search(Metadata('Song', 'Artist'))
    first = next(c for c in candidates if c.release_id == 'first')
    assert first.other_release_ids == ('second',)
    values = {key: getattr(first, key) for key in ('title', 'artist', 'album', 'year', 'genre')}
    assert confirm_metadata(values, first).other_release_ids == ('second',)
    assert confirm_metadata(values, first).release_group_id == 'same'
    assert confirm_metadata(dict(values, album='Changed'), first).other_release_ids == ()
    assert confirm_metadata(dict(values, album='Changed'), first).release_group_id == ''


def test_quality_is_cleaned_from_existing_tags_and_saved_output(tmp_path, wav):
    tagged, _ = Library(tmp_path / 'input').process(wav, Metadata('Song', 'Drake'))
    # Simulate real downloaded files carrying dirty title AND artist tags.
    from mutagen.id3 import TIT2, TPE1
    tags = ID3(tagged)
    tags.add(TIT2(encoding=1, text='Wu-Tang Forever 128k'))
    tags.add(TPE1(encoding=1, text='드레이크 [256kbps]'))
    tags.save(tagged)
    hint = read_hint(tagged, [{'name': 'Drake', 'aliases': ['드레이크']}])
    assert (hint.title, hint.artist) == ('Wu-Tang Forever', 'Drake')
    candidate = Metadata('Wu-Tang Forever', 'Drake', 'Album', '2013', release_id='release')
    values = dict(title='Wu-Tang Forever (256k)', artist='Drake 128k', album='Album', year='2013', genre='')
    confirmed = confirm_metadata(values, candidate)
    assert confirmed.release_id == 'release'
    result, _ = Library(tmp_path / 'out').process(tagged, confirmed, cover())
    assert result.name == 'Drake - Wu-Tang Forever.mp3'
    assert str(ID3(result)['TIT2']) == 'Wu-Tang Forever'
    assert str(ID3(result)['TPE1']) == 'Drake'
    assert ID3(result).getall('APIC')


def test_search_retries_reversed_filename_and_keeps_album_link(monkeypatch):
    queries = []
    def response(url, params):
        queries.append(params['query'])
        if params['query'] == 'recording:"Wu\\-Tang Forever" AND artist:"Drake"':
            return {'recordings': [{
                'title': 'Wu‐Tang Forever', 'artist-credit': [{'name': 'Drake'}],
                'score': 100, 'length': 217693, 'releases': [
                    {'id': 'album-release', 'title': 'Nothing Was the Same', 'date': '2013-09-24',
                     'release-group': {'id': 'group', 'primary-type': 'Album'}}]}]}
        return {'recordings': []}
    network = Network()
    monkeypatch.setattr(network, 'json', response)
    candidates = network.search(Metadata('Drake (128k)', 'Wu-Tang Forever 256k', duration=217.704))
    assert len(queries) == 2
    assert candidates[0].artist == 'Drake'
    assert candidates[0].album == 'Nothing Was the Same'
    assert candidates[0].release_id == 'album-release'
    assert candidates[0].duration == pytest.approx(217.693)


def test_title_only_search_does_not_return_unrelated_songs(monkeypatch):
    queries = []
    def response(url, params):
        queries.append(params['query'])
        if 'artist:' in params['query']:
            return {'recordings': []}
        return {'recordings': [
            {'title': 'Song', 'artist-credit': [{'name': 'Actual Artist'}]},
            {'title': 'Unrelated Song', 'artist-credit': [{'name': 'Someone'}]}]}
    network = Network()
    monkeypatch.setattr(network, 'json', response)
    candidates = network.search(Metadata('Song', 'Mistaken Artist'))
    assert len(queries) == 3
    assert [(c.title, c.artist) for c in candidates] == [('Song', 'Actual Artist')]


def test_duplicate_can_update_confirmed_tags_name_cover_and_all_hash_aliases(tmp_path, wav):
    library = Library(tmp_path / 'out')
    old, _ = library.process(wav, Metadata('Wrong title', 'Wrong artist'))
    old_bytes = old.read_bytes()
    new, duplicate = library.process(wav, Metadata('Wu-Tang Forever 256k', 'Drake (128k)'),
                                     cover(), update_duplicate=True)
    assert duplicate and library.updated_from == old
    assert not old.exists()
    assert new.name == 'Drake - Wu-Tang Forever.mp3'
    assert str(ID3(new)['TIT2']) == 'Wu-Tang Forever'
    assert ID3(new).getall('APIC')
    assert len(list(library.output.glob('*.mp3'))) == 1
    # Both the original PCM and the generated MP3 fingerprints must point to it.
    reimport = tmp_path / 'old-download.mp3'
    reimport.write_bytes(old_bytes)
    target, duplicate = Library(library.output).process(reimport, Metadata('Other', 'Other'))
    assert duplicate and target == new


@pytest.mark.parametrize('same_name', [False, True])
def test_duplicate_update_rollback_keeps_previous_file_and_index(tmp_path, wav, monkeypatch, same_name):
    import os
    library = Library(tmp_path / 'out')
    old, _ = library.process(wav, Metadata('Song', 'Artist'))
    before, index_before = old.read_bytes(), library.index_path.read_bytes()
    real_replace = os.replace
    def fail_index(source, target):
        if target == library.index_path:
            raise OSError('index disk error')
        return real_replace(source, target)
    monkeypatch.setattr(os, 'replace', fail_index)
    with pytest.raises(OSError, match='index disk error'):
        library.process(wav, Metadata('Song' if same_name else 'New', 'Artist', album='New album'),
                        update_duplicate=True)
    assert old.read_bytes() == before
    assert library.index_path.read_bytes() == index_before
    assert list(library.output.glob('*.mp3')) == [old]


def test_duplicate_update_preserves_unrelated_filename_collision(tmp_path, wav):
    library = Library(tmp_path / 'out')
    old, _ = library.process(wav, Metadata('Wrong', 'Artist'))
    unrelated = library.output / 'Drake - Song.mp3'
    unrelated.write_bytes(b'existing unrelated file')
    new, duplicate = library.process(wav, Metadata('Song', 'Drake'), update_duplicate=True)
    assert duplicate and new.name == 'Drake - Song (2).mp3'
    assert unrelated.read_bytes() == b'existing unrelated file'
    assert not old.exists()


def test_featured_artist_is_a_match_without_unrelated_title_searches(monkeypatch):
    queries = []
    title = 'Do Not Go Gentle Into That Good Night'
    def response(url, params):
        queries.append(params['query'])
        return {'recordings': [{'title': title, 'score': 100, 'length': 303000,
            'artist-credit': [{'name': 'JUSTHIS', 'joinphrase': ' feat. '},
                              {'name': 'KWAII', 'joinphrase': ', '}, {'name': 'Don Malik'}],
            'releases': [{'id': 'correct', 'title': title + ' II', 'date': '2022-01-01'}]}]}
    network = Network()
    monkeypatch.setattr(network, 'json', response)
    candidates = network.search(Metadata(title, 'JUSTHIS', duration=304.17))
    assert len(queries) == 1
    assert candidates[0].credited_artists == ('JUSTHIS', 'KWAII', 'Don Malik')
    assert candidates[0].artist == 'JUSTHIS feat. KWAII, Don Malik'
    assert candidates[0].release_id == 'correct'


def test_artist_name_as_song_title_is_not_a_fallback_match(monkeypatch):
    network = Network()
    monkeypatch.setattr(network, 'json', lambda *a, **k: {'recordings': [
        {'title': 'Justhis', 'artist-credit': [{'name': 'JUSTHIS'}], 'length': 201000},
        {'title': 'Justhis', 'artist-credit': [{'name': 'Quasimoly'}], 'length': 202000}]})
    assert network.search(Metadata('Do Not Go Gentle Into That Good Night', 'JUSTHIS', duration=304.17)) == []


def test_long_original_filename_is_independent_of_storage_name(wav):
    original = ('JUSTHIS - Do Not Go Gentle Into That Good Night '
                '[Do Not Go Gentle Into That Good Night II]ㅣLyrics가사.wav')
    hint = read_hint(wav, filename=original)
    assert hint.title == 'Do Not Go Gentle Into That Good Night'
    assert hint.artist == 'JUSTHIS'
    assert hint.album == 'Do Not Go Gentle Into That Good Night II'
    assert hint.duration > 0
