"""Validate the built image without publishing a port or using external APIs."""
import os
import subprocess
import time
import uuid
from music_manager.release import VERSION

CODE = '''
import os, json, subprocess, tempfile, urllib.request
from pathlib import Path
from mutagen.id3 import ID3
from music_manager.core import Library, Metadata
assert os.getuid() == 10001
with urllib.request.urlopen('http://127.0.0.1:8000/healthz') as response:
    health=json.load(response)
    assert health['ffmpeg'] and health['version'] == os.environ['ID3_EXPECTED_VERSION']
with urllib.request.urlopen('http://127.0.0.1:8000/') as response:
    html=response.read().decode()
    assert '음악에 정보를 입히다' in html
    assert '현재 버전 v'+os.environ['ID3_EXPECTED_VERSION'] in html and '{{' not in html
with tempfile.TemporaryDirectory() as folder:
    root=Path(folder); audio=root/'sample.wav'
    subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=0.2',str(audio)],check=True)
    library=Library(root/'out')
    target, duplicate=library.process(audio,Metadata('컨테이너 검증 256k','ID3 Sit (128k)'))
    assert target.is_file() and not duplicate
    assert ID3(target).version == (2,3,0)
    assert str(ID3(target)['TIT2']) == '컨테이너 검증'
    updated, duplicate=library.process(audio,Metadata('수정곡','ID3 Sit'),update_duplicate=True)
    assert duplicate and not target.exists() and updated.name == 'ID3 Sit - 수정곡.mp3'
    assert len(list((root/'out').glob('*.mp3'))) == 1
print('Non-root v'+health['version']+' container, website, FFmpeg, quality cleanup and duplicate retagging passed')
'''


def main():
    docker = ['docker']
    if os.environ.get('ID3_DOCKER_CONFIG'):
        docker += ['--config', os.environ['ID3_DOCKER_CONFIG']]
    name = 'id3-sit-check-' + uuid.uuid4().hex[:10]
    def run(args, **kwargs):
        return subprocess.run(docker + args, **kwargs)
    try:
        run(['run', '--detach', '--name', name, '--network', 'none', '-e',
             'ID3_EXPECTED_VERSION='+VERSION, 'id3-sit-web:latest'], check=True, capture_output=True)
        for attempt in range(40):
            running = run(['inspect', '--format', '{{.State.Running}}', name], capture_output=True, text=True, check=True)
            if running.stdout.strip() != 'true':
                logs = run(['logs', name], capture_output=True, text=True)
                raise RuntimeError(logs.stdout + logs.stderr)
            ready = run(['exec', name, 'python', '-c', "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(.25)
        else:
            raise RuntimeError('Container did not become ready')
        run(['exec', name, 'python', '-c', CODE], check=True)
    finally:
        run(['rm', '--force', name], capture_output=True)


if __name__ == '__main__':
    main()
