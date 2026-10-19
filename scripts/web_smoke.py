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
from music_manager.web import create_app


def main():
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        source = root / 'Studio - Sample.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=1', str(source)], check=True)
        class DemoNetwork:
            def search(self, hint):
                return [Metadata('Sample', 'Studio', 'Sessions', '2024', 'Electronic')]
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
                page.locator('#file-input').set_input_files([str(source), str(source)])
                page.locator('#stat-total').get_by_text('2', exact=False).wait_for()
                for index in range(2):
                    page.get_by_role('button', name='정보 확인 →').first.click(timeout=15000)
                    page.locator('.candidate').first.click()
                    page.locator('#metadata-form input[name=title]').fill('검증곡')
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
                # A poll must leave an unchanged audio element connected.
                page.locator('audio').first.evaluate('(element) => element.dataset.identity = "retained"')
                page.wait_for_timeout(2300)
                assert page.locator('audio').first.get_attribute('data-identity') == 'retained'
                page.screenshot(path='/tmp/id3-web-completed-desktop.png', full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.screenshot(path='/tmp/id3-web-mobile.png', full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'), 'mobile overflow'
                page.get_by_role('button', name='실패', exact=True).click()
                page.get_by_text('선택한 상태의 트랙이 없어요').wait_for()
                page.get_by_role('button', name='전체', exact=True).click()
                page.once('dialog', lambda dialog: dialog.accept())
                page.locator('.remove-job').first.click()
                page.wait_for_function('() => document.querySelectorAll("#tracks tr").length === 1')
                assert not errors, errors
                browser.close()
                print('Browser upload, metadata editing, conversion, download, duplicate handling, mobile layout and deletion passed')
        finally:
            app.extensions['jobs'].executor.shutdown(wait=True)
            server.close()
            thread.join(timeout=5)


if __name__ == '__main__':
    main()
