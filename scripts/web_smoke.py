"""Real Chromium + HTTP + FFmpeg workflow, using deterministic metadata."""
import io
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from mutagen.id3 import ID3
from PIL import Image
from playwright.sync_api import sync_playwright
from waitress import create_server

from music_manager.core import Metadata
from music_manager.release import VERSION
from music_manager.web import create_app


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / 'Sample - 스튜디오 (128k).wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', str(source)], check=True)
        cover_image = io.BytesIO()
        Image.new('RGB', (40, 40), 'navy').save(cover_image, 'JPEG')
        manual_image = root / 'manual-cover.png'
        Image.new('RGB', (80, 80), 'red').save(manual_image)
        latest_image = root / 'updated-cover.png'
        Image.new('RGB', (80, 80), 'green').save(latest_image)
        class DemoNetwork:
            def search(self, hint):
                return [Metadata(hint.title, hint.artist, 'Sessions', '2024', 'Electronic',
                                 release_id='first', other_release_ids=('second',))]
            def cover(self, release_id):
                return cover_image.getvalue() if release_id == 'second' else None
        app = create_app({'DATA_ROOT': root / 'data', 'WEB_PASSWORD': '',
                          'SECRET_KEY': 'browser-test', 'TESTING': True}, DemoNetwork)
        server = create_server(app, host='127.0.0.1', port=0, threads=4)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            with sync_playwright() as p:
                browser_path = os.environ.get('CHROMIUM_PATH') or shutil.which('chromium')
                browser = p.chromium.launch(executable_path=browser_path, args=['--no-sandbox'])
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, accept_downloads=True)
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.effective_port}', wait_until='networkidle')
                assert page.locator('#header-version').inner_text() == 'v'+VERSION
                page.locator('#release-notes summary').click()
                page.get_by_text('긴 원본 파일명을 보존하고 Lyrics·가사 표기와 [앨범명]을 구분해 검색합니다.', exact=True).first.wait_for()
                assert page.locator('#release-notes').get_attribute('open') is not None
                page.locator('#release-notes summary').click()
                page.locator('#manage-artists').click()
                page.locator('#artist-name').fill('Studio')
                page.locator('#artist-aliases').fill('스튜디오')
                page.locator('#artist-save').click()
                page.locator('.artist-row strong').get_by_text('Studio', exact=True).wait_for()
                page.get_by_role('button', name='Studio 수정', exact=True).click()
                page.locator('#artist-aliases').fill('스튜디오\nStudio Official')
                page.locator('#artist-save').click()
                page.get_by_text('스튜디오 · Studio Official', exact=True).wait_for()
                page.locator('#artist-name').fill('Rihanna')
                page.locator('#artist-aliases').fill('리한나')
                page.locator('#artist-save').click()
                page.get_by_role('button', name='Rihanna 삭제').wait_for()
                page.once('dialog', lambda dialog: dialog.accept())
                page.get_by_role('button', name='Rihanna 삭제').click()
                page.wait_for_function('() => document.querySelectorAll(".artist-row").length === 1')
                with page.expect_download() as export_event:
                    page.locator('#artist-export').click()
                exported = root / 'artists.json'
                export_event.value.save_as(exported)
                assert 'Studio Official' in exported.read_text()
                page.get_by_role('button', name='Studio 수정', exact=True).click()
                page.locator('#artist-aliases').fill('스튜디오\nTemporary')
                page.locator('#artist-save').click()
                page.get_by_text('스튜디오 · Temporary', exact=True).wait_for()
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('#artist-import').set_input_files(str(exported))
                page.get_by_text('스튜디오 · Studio Official', exact=True).wait_for()
                page.screenshot(path='/tmp/id3-artists-manager.png', full_page=True)
                page.reload(wait_until='networkidle')
                assert page.locator('#artist-count').inner_text() == '1'
                page.locator('#file-input').set_input_files([str(source), str(source)])
                page.locator('#stat-total').get_by_text('2', exact=False).wait_for()
                for index in range(2):
                    page.get_by_role('button', name='정보 확인 →').first.click(timeout=15000)
                    assert 'v'+VERSION in page.locator('#dialog-version').inner_text()
                    assert page.locator('#metadata-form input[name=artist]').input_value() == 'Studio'
                    assert page.locator('#metadata-form input[name=title]').input_value() == 'Sample'
                    assert '128k' not in page.locator('#source-description').inner_text()
                    page.locator('.candidate').first.click()
                    page.locator('#cover-preview').wait_for(state='visible')
                    page.locator('#metadata-form input[name=title]').fill('검증곡')
                    if index == 0:
                        page.locator('#research-fields').click()
                        page.locator('.candidate strong').get_by_text('Studio — 검증곡', exact=True).wait_for()
                        page.locator('.candidate').first.click()
                        page.locator('#cover-preview').wait_for(state='visible')
                        page.locator('#cover-file').set_input_files(str(manual_image))
                        page.get_by_text('직접 올린 이미지', exact=True).wait_for()
                    else:
                        page.locator('#metadata-form input[name=title]').fill('검증곡 256k')
                        page.locator('#metadata-form input[name=artist]').fill('Studio (128k)')
                        page.locator('#cover-file').set_input_files(str(latest_image))
                        page.get_by_text('직접 올린 이미지', exact=True).wait_for()
                    page.get_by_role('button', name='확인하고 MP3 만들기 →').click()
                    page.wait_for_function('(n) => document.querySelectorAll("a.download").length >= n', arg=index + 1, timeout=20000)
                page.get_by_text('중복 저장 방지됨').wait_for()
                with page.expect_download() as event:
                    page.locator('a.download').first.click()
                download = event.value
                target = root / download.suggested_filename
                download.save_as(target)
                tags = ID3(target)
                assert tags.version == (2, 3, 0)
                assert str(tags['TIT2']) == '검증곡'
                assert str(tags['TPE1']) == 'Studio'
                picture = Image.open(io.BytesIO(tags.getall('APIC')[0].data))
                assert picture.getpixel((0, 0))[1] > 100
                assert picture.getpixel((0, 0))[0] < 50
                assert '128k' not in download.suggested_filename and '256k' not in download.suggested_filename
                # A completed item can be corrected without uploading again.
                page.get_by_role('button', name='정보 수정', exact=True).first.click()
                page.get_by_role('button', name='정보 확인 →').first.click(timeout=15000)
                assert page.locator('#metadata-form input[name=title]').input_value() == '검증곡'
                assert '원본 길이' in page.locator('#source-description').inner_text()
                page.locator('#metadata-form input[name=title]').fill('수정곡 (256kbps)')
                page.locator('#cover-file').set_input_files(str(latest_image))
                page.get_by_text('직접 올린 이미지', exact=True).wait_for()
                page.get_by_role('button', name='확인하고 MP3 만들기 →').click()
                page.wait_for_function('() => [...document.querySelectorAll("#tracks strong")].every(el => el.textContent === "수정곡")', timeout=20000)
                # A poll must leave an unchanged audio element connected.
                page.locator('audio').first.evaluate('(element) => element.dataset.identity = "retained"')
                page.wait_for_timeout(2300)
                assert page.locator('audio').first.get_attribute('data-identity') == 'retained'
                page.screenshot(path='/tmp/id3-web-completed-desktop.png', full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                assert page.locator('#header-version').is_visible()
                assert page.locator('#release-notes summary').is_visible()
                page.screenshot(path='/tmp/id3-web-mobile.png', full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile overflow'
                page.locator('#manage-artists').click()
                assert page.locator('#artists-dialog').is_visible()
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'artist manager mobile overflow'
                page.locator('#close-artists').click()
                page.get_by_role('button', name='실패', exact=True).click()
                page.get_by_text('선택한 상태의 트랙이 없어요').wait_for()
                page.get_by_role('button', name='전체', exact=True).click()
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('.remove-job').first.click()
                page.wait_for_function('() => document.querySelectorAll("#tracks tr").length === 1')
                long_source = root / ('JUSTHIS - Do Not Go Gentle Into That Good Night '
                                      '[Do Not Go Gentle Into That Good Night II]ㅣLyrics가사.wav')
                shutil.copyfile(source, long_source)
                page.locator('#file-input').set_input_files(str(long_source))
                page.get_by_role('button', name='정보 확인 →').first.click(timeout=15000)
                assert page.locator('#metadata-form input[name=title]').input_value() == 'Do Not Go Gentle Into That Good Night'
                assert page.locator('#metadata-form input[name=artist]').input_value() == 'JUSTHIS'
                assert page.locator('#metadata-form input[name=album]').input_value() == 'Do Not Go Gentle Into That Good Night II'
                assert 'v'+VERSION in page.locator('#dialog-version').inner_text()
                page.locator('#close-dialog').click()
                # An open old page must report a newer server without changing
                # its own displayed version to pretend the new UI is loaded.
                def newer_version(route):
                    response = route.fetch()
                    payload = response.json()
                    payload['version'] = '99.0.0'
                    route.fulfill(response=response, json=payload)
                page.route('**/api/jobs', newer_version)
                page.locator('#update-notice').wait_for(state='visible', timeout=10000)
                assert '99.0.0' in page.locator('#update-message').inner_text()
                assert page.locator('#header-version').inner_text() == 'v'+VERSION
                page.unroute('**/api/jobs', newer_version)
                assert not errors, errors
                browser.close()
                print('Browser version/history and update notice, full-length filenames, artists, research, covers, downloads, duplicate editing and mobile layout passed')
        finally:
            app.extensions['jobs'].executor.shutdown(wait=True)
            server.close()
            thread.join(timeout=5)


if __name__ == '__main__':
    main()
