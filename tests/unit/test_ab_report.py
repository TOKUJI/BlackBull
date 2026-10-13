"""bench/ab/report.py turns an ABBA raw.tsv into a verdict a reader can act on."""
from __future__ import annotations

import json
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _raw(tmp_path: Path, phases: dict[str, dict[str, list[float]]]) -> Path:
    """phases: phase -> arm -> values, laid out as ``rounds`` ABBA rounds."""
    lines = ['phase\tround\tarm\trps\tproof']
    for phase, arms in phases.items():
        rounds = len(arms['base']) // 2
        for r in range(rounds):
            for arm in ('base', 'treat'):
                for v in arms[arm][2 * r:2 * r + 2]:
                    lines.append(f'{phase}\t{r + 1}\t{arm}\t{v}\tproof')
    path = tmp_path / 'raw.tsv'
    path.write_text('\n'.join(lines) + '\n')
    return path


def _summary(raw: Path, *args: str) -> dict:
    out = subprocess.run([sys.executable, str(ROOT / 'bench/ab/report.py'), str(raw),
                          '--json', *args], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _noisy(mean: float, n: int, cv: float, seed: int) -> list[float]:
    rng = random.Random(seed)
    return [mean * (1 + rng.gauss(0, cv)) for _ in range(n)]


def test_a_clear_regression_is_called_slower(tmp_path):
    raw = _raw(tmp_path, {
        'null': {'base': _noisy(100, 16, .003, 1), 'treat': _noisy(100, 16, .003, 2)},
        'real': {'base': _noisy(100, 16, .003, 3), 'treat': _noisy(97, 16, .003, 4)},
    })
    s = _summary(raw)

    lo, hi = s['phases']['real']['ci95']
    assert -3.5 < s['phases']['real']['delta_pct'] < -2.5
    assert hi < 0
    assert s['verdict'] == 'slower'


def test_no_change_on_a_quiet_box_is_called_equivalent(tmp_path):
    raw = _raw(tmp_path, {
        'null': {'base': _noisy(100, 24, .002, 5), 'treat': _noisy(100, 24, .002, 6)},
        'real': {'base': _noisy(100, 24, .002, 7), 'treat': _noisy(100, 24, .002, 8)},
    })

    assert _summary(raw, '--equiv', '1')['verdict'] == 'equivalent'


def test_a_null_floor_wider_than_the_bound_says_the_box_cannot_resolve_it(tmp_path):
    raw = _raw(tmp_path, {
        'null': {'base': _noisy(100, 8, .05, 9), 'treat': _noisy(100, 8, .05, 10)},
        'real': {'base': _noisy(100, 8, .05, 11), 'treat': _noisy(100, 8, .05, 12)},
    })

    assert _summary(raw, '--equiv', '0.5')['verdict'] == 'unresolved'


def test_a_bimodal_sample_is_compared_within_its_modes(tmp_path):
    rng = random.Random(13)

    def arm(mean: float) -> list[float]:
        return [mean * (0.85 if rng.random() < 0.5 else 1) * (1 + rng.gauss(0, .002))
                for _ in range(24)]

    raw = _raw(tmp_path, {'real': {'base': arm(100), 'treat': arm(98)}})
    s = _summary(raw)

    assert s['bimodal'] is True
    assert -2.6 < s['phases']['real']['delta_pct'] < -1.4


def test_failed_runs_are_left_out_rather_than_breaking_the_report(tmp_path):
    raw = _raw(tmp_path, {'real': {'base': [100.0, float('nan'), 100.2, 99.8],
                                   'treat': [97.0, 97.1, float('nan'), 96.9]}})
    s = _summary(raw)

    assert s['phases']['real']['n'] == {'base': 3, 'treat': 3}


def test_the_markdown_report_names_the_verdict(tmp_path):
    raw = _raw(tmp_path, {'real': {'base': _noisy(100, 8, .003, 14),
                                   'treat': _noisy(97, 8, .003, 15)}})
    out = subprocess.run([sys.executable, str(ROOT / 'bench/ab/report.py'), str(raw)],
                         capture_output=True, text=True, check=True).stdout

    assert 'slower' in out and '95 % CI' in out


def test_a_large_clear_effect_is_not_mistaken_for_two_modes(tmp_path):
    for effect, verdict in ((1.10, 'faster'), (0.90, 'slower')):
        raw = _raw(tmp_path, {
            'null': {'base': _noisy(100, 16, .004, 16), 'treat': _noisy(100, 16, .004, 17)},
            'real': {'base': _noisy(100, 16, .004, 18), 'treat': _noisy(100 * effect, 16, .004, 19)},
        })
        s = _summary(raw)

        assert s['bimodal'] is False
        assert s['verdict'] == verdict
        assert abs(s['phases']['real']['delta_pct'] - (effect - 1) * 100) < 1
