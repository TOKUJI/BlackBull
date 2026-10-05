import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('exists', [False, True])
def test_version_registration_preserves_existing_version(tmp_path, exists):
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
        '    assert value == {"name":"0.81.0", "$type":"VersionBundleElement", "released":False}\n'
        '    print(json.dumps({"id":"133-1", "name":"0.81.0", "released":False}))\n'
        'else:\n'
        '    print(json.dumps([{"id":"133-1", "name":"0.81.0", "released":True}] if os.environ["EXISTS"] == "1" else []))\n'
    )
    curl.chmod(0o755)
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, 'PATH': f'{tmp_path}:{os.environ["PATH"]}',
           'YOUTRACK_URL': 'https://example.test', 'YOUTRACK_TOKEN': 'test-token',
           'CALLS': str(calls), 'EXISTS': str(int(exists))}
    result = subprocess.run(['bash', str(root / 'scripts/youtrack.sh'), 'version', '0.81.0'],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['released'] is exists
    requests = [json.loads(line) for line in calls.read_text().splitlines()]
    assert sum('POST' in request for request in requests) == (0 if exists else 1)
