"""G2-4 container tier: run the probe against a limited server container.

``just vuln-container`` starts the fixture in a container with ``--memory``
and ``--pids-limit`` and runs the probe from a sibling container sharing the
network and PID namespaces (no published ports), so /proc observation (G2-3)
works unchanged.  It records memory.events (oom_kill), pids.events (max),
memory.peak, pids.peak and docker inspect's OOMKilled/ExitCount, compares
the container verdicts with a native run (the recipe does that diff), and
exits non-zero on any resource breach or verdict drift.  ``--expect-oom``
turns the run into a detector verification: a tiny ``--memory`` must make
the tool *detect* an OOM.

Containers are self-expiring (``timeout`` inside the command) and carry
labels; every failure path removes them (finally + label sweep), so no
container survives the recipe.

Build the image first: ``docker build -t bb-vuln -f
tools/security/container.Dockerfile .``
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_IMAGE = 'bb-vuln'
_VOLUME = 'bb-vuln-rt'
_LIFETIME_S = 600  # self-expiry: the server command dies after this


def parse_cgroup_counters(text: str) -> dict[str, int]:
    """Parse a cgroup v2 counters file: key/value pairs, one or many per line."""
    parts = text.split()
    if len(parts) == 1 and parts[0].isdigit():
        return {'value': int(parts[0])}
    values: dict[str, int] = {}
    for key, raw in zip(parts[::2], parts[1::2]):
        if raw.lstrip('-').isdigit():
            values[key] = int(raw)
    return values


def parse_verdicts(text: str) -> dict[str, str]:
    """check -> verdict from the probe's stdout table."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2] in ('PASS', 'FAIL', 'TIMEOUT', 'SKIP'):
            out[parts[0]] = parts[2]
    return out


def _docker(*args: str, check: bool = True) -> str:
    proc = subprocess.run(['docker', *args], capture_output=True, text=True,
                          timeout=900)
    if check and proc.returncode != 0:
        raise RuntimeError(f'docker {" ".join(args)}: {proc.stderr.strip()}')
    return proc.stdout


def _sweep(run_id: str | None = None) -> None:
    label = f'bb-vuln.run={run_id}' if run_id else 'bb-vuln=1'
    ids = _docker('ps', '-aq', '--filter', f'label={label}', check=False).split()
    for cid in ids:
        _docker('rm', '-f', cid, check=False)


def _collect(server: str) -> dict[str, object]:
    metrics: dict[str, object] = {}
    for name in ('memory.events', 'memory.peak', 'pids.events', 'pids.peak'):
        text = _docker('exec', server, 'cat', f'/sys/fs/cgroup/{name}',
                       check=False)
        if text.strip():
            metrics[name] = parse_cgroup_counters(text) or text.strip()
    inspect = _docker('inspect', '--format',
                      '{{.State.OOMKilled}} {{.State.ExitCode}}', server,
                      check=False).split()
    if len(inspect) == 2:
        metrics['docker.OOMKilled'] = inspect[0] == 'true'
        metrics['docker.ExitCode'] = int(inspect[1])
    return metrics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--memory', default='64m')
    parser.add_argument('--pids-limit', type=int, default=256)
    parser.add_argument('--expect-oom', action='store_true',
                        help='detector verification: a tiny --memory must be '
                             '*detected* as an OOM (exit 0 = detection works)')
    parser.add_argument('--verdicts-out', default=None,
                        help='write check=verdict lines here for the parity '
                             'diff the recipe performs')
    parser.add_argument('--out-dir', default='bench/results/security')
    args = parser.parse_args(argv)

    run_id = f'{int(time.time())}'
    server = f'bb-vuln-server-{run_id}'
    _sweep()  # leftovers of earlier failed runs never survive
    _docker('volume', 'create', _VOLUME)
    probe_out = ''
    metrics: dict[str, object] = {}
    probe_rc = 0
    try:
        _docker('run', '-d', '--name', server, '--label', 'bb-vuln=1',
                '--label', f'bb-vuln.run={run_id}', '--memory', args.memory,
                '--pids-limit', str(args.pids_limit), '-v', f'{_VOLUME}:/run/bb-vuln',
                '-e', 'XDG_RUNTIME_DIR=/run/bb-vuln', _IMAGE, 'sh', '-c',
                f'exec timeout {_LIFETIME_S} python tools/security/fixture_app.py')
        for _ in range(60):
            time.sleep(1)
            health = _docker('run', '--rm', '--network', f'container:{server}',
                             '--pid', f'container:{server}', '-v',
                             f'{_VOLUME}:/run/bb-vuln', '-e',
                             'XDG_RUNTIME_DIR=/run/bb-vuln', '--entrypoint',
                             'python', _IMAGE,
                             'tools/security/fixture_app.py', '--health',
                             check=False)
            if 'ok' in health or not args.expect_oom:
                break
        probe_script = (
            'import pathlib\n'
            'pid = next(p.name for p in pathlib.Path("/proc").iterdir()\n'
            '           if p.name.isdigit()\n'
            '           and b"fixture_app" in (p / "cmdline").read_bytes())\n'
            'import subprocess, sys\n'
            'sys.exit(subprocess.call([\n'
            '    "python", "tools/security/probe.py",\n'
            '    "--base-url", "http://127.0.0.1:8000",\n'
            '    "--h2-url", "https://127.0.0.1:8443",\n'
            '    "--server-pid", pid, "--observe-settle", "0.2"]))\n')
        proc = subprocess.run(
            ['docker', 'run', '--rm', '--network', f'container:{server}',
             '--pid', f'container:{server}', '-v', f'{_VOLUME}:/run/bb-vuln',
             '-e', 'XDG_RUNTIME_DIR=/run/bb-vuln', '--entrypoint', 'python',
             _IMAGE, '-c', probe_script],
            capture_output=True, text=True, timeout=600)
        probe_out, probe_rc = proc.stdout + proc.stderr, proc.returncode
        metrics = _collect(server)
    finally:
        _docker('rm', '-f', server, check=False)
        _sweep(run_id)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    (out_dir / f'container-{stamp}.txt').write_text(probe_out, encoding='utf-8')
    (out_dir / f'container-{stamp}.json').write_text(
        json.dumps(metrics, indent=2, sort_keys=True), encoding='utf-8')
    if args.verdicts_out:
        lines = [f'{k}={v}' for k, v in sorted(parse_verdicts(probe_out).items())]
        Path(args.verdicts_out).write_text('\n'.join(lines) + '\n', encoding='utf-8')

    oom_kill = metrics.get('memory.events', {}).get('oom_kill', 0) \
        if isinstance(metrics.get('memory.events'), dict) else 0
    pids_max = metrics.get('pids.events', {}).get('max', 0) \
        if isinstance(metrics.get('pids.events'), dict) else 0
    oomkilled = bool(metrics.get('docker.OOMKilled'))
    print(f'G2-4: oom_kill={oom_kill} pids_max={pids_max} '
          f'OOMKilled={oomkilled} memory.peak={metrics.get("memory.peak")} '
          f'pids.peak={metrics.get("pids.peak")} probe_rc={probe_rc}')
    if args.expect_oom:
        detected = oom_kill > 0 or oomkilled or pids_max > 0
        print(f'G2-4 OOM detection: {"verified" if detected else "NOT DETECTED"}')
        return 0 if detected else 1
    breached = oom_kill > 0 or pids_max > 0 or oomkilled or probe_rc != 0
    leftovers = _docker('ps', '-aq', '--filter', 'label=bb-vuln=1',
                        check=False).split()
    if leftovers:
        print(f'G2-4: leftover containers after cleanup: {leftovers}')
        breached = True
    print(f'G2-4: {"CLEAN" if not breached else "BREACH"} '
          f'(containers left: {len(leftovers)})')
    return 1 if breached else 0


if __name__ == '__main__':
    raise SystemExit(main())
