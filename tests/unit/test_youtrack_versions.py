import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('operation,exists', [
    ('version', False), ('version', True),
    ('version-release', False), ('version-release', True),
])
def test_version_registration_preserves_existing_version(tmp_path, operation, exists):
    calls = tmp_path / 'calls.jsonl'
    curl = tmp_path / 'curl'
    curl.write_text(
        f'#!{sys.executable}\n'
        'import json, os, sys\n'
        'args = sys.argv[1:]\n'
        'with open(os.environ["CALLS"], "a") as f:\n'
        '    f.write(json.dumps(args) + "\\n")\n'
        'if any("/api/admin/projects" == a.split("https://example.test")[-1] for a in args):\n'
        '    print(json.dumps([{"id":"0-1", "shortName":"BLA"}]))\n'
        'elif any("/customFields" in a for a in args):\n'
        '    print(json.dumps([{"field":{"name":"Fix versions"}, "bundle":{"id":"71-1"}}]))\n'
        'elif "POST" in args:\n'
        '    value = json.load(sys.stdin)\n'
        '    if os.environ["OPERATION"] == "version-release":\n'
        '        assert any("/values/133-1?" in a for a in args)\n'
        '        assert value == {"released":True, "releaseDate":1791158400000}\n'
        '    else:\n'
        '        assert value == {"name":"0.81.0", "$type":"VersionBundleElement", "released":False}\n'
        '    print(json.dumps({"id":"133-1", "name":"0.81.0", "released":value["released"]}))\n'
        'else:\n'
        '    print(json.dumps([{"id":"133-1", "name":"0.81.0", "released":True}] if os.environ["EXISTS"] == "1" else []))\n'
    )
    curl.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'YOUTRACK_URL': 'https://example.test', 'YOUTRACK_TOKEN': 'test-token',
           'CALLS': str(calls), 'EXISTS': str(int(exists)), 'OPERATION': operation}
    args = ['bash', str(root / 'scripts/youtrack.sh'), operation, '0.81.0']
    if operation == 'version-release':
        args.append('2026-10-05')
    result = subprocess.run(args,
                            env=env, capture_output=True, text=True, timeout=10)
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    if operation == 'version-release' and not exists:
        assert result.returncode != 0
        assert not any('POST' in request for request in requests)
        return
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['released'] is exists
    expected_posts = 1 if operation == 'version-release' else int(not exists)
    assert sum('POST' in request for request in requests) == expected_posts


@pytest.mark.parametrize('date', ['2026-02-31', '2026-10-5', 'invalid'])
def test_invalid_release_date_never_contacts_tracker(tmp_path, date):
    curl = tmp_path / 'curl'
    called = tmp_path / 'called'
    curl.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 1\n')
    curl.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'YOUTRACK_URL': 'https://example.test', 'YOUTRACK_TOKEN': 'test-token'}
    result = subprocess.run(
        ['bash', str(root / 'scripts/youtrack.sh'), 'version-release', '0.81.0', date],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert not called.exists()
