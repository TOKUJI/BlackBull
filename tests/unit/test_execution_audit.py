import json
import os
from pathlib import Path
import subprocess
import sys
import sysconfig

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.version_info < (3, 14), reason='audit lane uses CPython 3.14')


def audit(tmp_path, source, command, extra=None):
    package = tmp_path / 'audit_subject'
    package.mkdir()
    (package / '__init__.py').write_text('from .core import Used as Alias\n')
    (package / 'core.py').write_text(source)
    for name, text in (extra or {}).items():
        (package / name).write_text(text)
    output = tmp_path / 'audit'
    result = subprocess.run(
        [sys.executable, '-m', 'tools.execution_audit', 'collect', '--package', str(package),
         '--output', str(output), '--', *(command if isinstance(command, list) else [sys.executable, '-c', command])],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return package, output


def check(package, output, allowance):
    allowlist = output / 'allow.json'
    allowlist.write_text(json.dumps(allowance))
    result = subprocess.run(
        [sys.executable, '-m', 'tools.execution_audit', 'check', '--package', str(package),
         '--output', str(output), '--allowlist', str(allowlist)],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert (output / 'report.json').exists(), result.stdout + result.stderr
    return result, json.loads((output / 'report.json').read_text())


def test_runtime_aliases_registry_calls_and_binding_reads(tmp_path):
    package, output = audit(tmp_path, '''
READ = 7
UNREAD = 9
class Used:
    def called(self): return READ
    def forgotten(self): return 0
class Unused: pass
def invoked(): return READ
def forgotten(): return UNREAD
CALLBACKS = [invoked]
''', '''
import audit_subject as exported
import audit_subject.core as core
assert exported.Alias().called() == 7
assert core.CALLBACKS[0]() == 7
''')
    result, report = check(package, output, {})
    assert result.returncode == 1
    names = {(x['kind'], x['name']) for x in report['candidates'].values()}
    assert names == {('function', 'Used.forgotten'), ('class', 'Unused'),
                     ('function', 'forgotten'), ('binding', 'UNREAD')}
    allowed = {key: 'Retained extension point deliberately not invoked by this fixture.'
               for key in report['candidates']}
    result, report = check(package, output, allowed)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not report['unexpected']


def test_from_import_getattr_and_direct_dictionary_limit(tmp_path):
    package, output = audit(tmp_path, '''
class Used: pass
IMPORTED = 1
DYNAMIC = 2
DIRECT_DICT = 3
''', '''
import audit_subject.core as core
from audit_subject.core import IMPORTED as alias
assert alias == 1
assert getattr(core, 'DYNAMIC') == 2
assert vars(core)['DIRECT_DICT'] == 3
''')
    result, report = check(package, output, {})
    assert {(x['kind'], x['name']) for x in report['candidates'].values()} == {
        ('binding', 'DIRECT_DICT')}


@pytest.mark.parametrize('reason', ['', '   ', None, 42])
def test_allowances_need_a_reason(tmp_path, reason):
    package, output = audit(tmp_path, 'class Used: pass\nUNREAD = 1\n', 'import audit_subject')
    _, report = check(package, output, {})
    result, _ = check(package, output, {next(iter(report['candidates'])): reason})
    assert result.returncode != 0


def test_missing_observations_and_changed_source_fail_closed(tmp_path):
    package, output = audit(tmp_path, 'class Used: pass\n', 'import audit_subject')
    (package / 'core.py').write_text('class Used: pass\nNEW = 1\n')
    result, _ = check(package, output, {})
    assert result.returncode != 0
    (package / 'core.py').write_text('class Used: pass\n')
    for record in output.glob('observed-*.json'):
        record.unlink()
    result, _ = check(package, output, {})
    assert result.returncode != 0


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='fork-only subprocess case')
def test_forked_body_entry_is_collected_before_low_level_exit(tmp_path):
    package, output = audit(tmp_path, '''
class Used: pass
READ_IN_CHILD = 3
def one_line(): return READ_IN_CHILD
''', '''
import os
import audit_subject.core as core
pid = os.fork()
if pid == 0:
    assert core.one_line() == 3
    os._exit(0)
assert os.waitpid(pid, 0)[1] == 0
''')
    result, report = check(package, output, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not report['candidates']


@pytest.mark.parametrize('method', ['fork', 'spawn', 'forkserver'])
def test_multiprocessing_child_only_work_is_collected(tmp_path, method):
    package, output = audit(tmp_path, '''
class Used: pass
READ_IN_CHILD = 3
def child_only():
    assert READ_IN_CHILD == 3
''', f'''
import multiprocessing
import audit_subject.core as core
p = multiprocessing.get_context({method!r}).Process(target=core.child_only)
p.start()
p.join(10)
assert p.exitcode == 0
''')
    result, report = check(package, output, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert report['processes'] >= 2


def test_lazy_module_attributes_keep_normal_import_behavior(tmp_path):
    package, output = audit(tmp_path, 'class Used: pass\n', '''
import audit_subject as lazy
import audit_subject.core as core
from audit_subject import *
assert Alias is core.Used
import warnings
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter('always')
    assert lazy.Alias is core.Used
assert caught[0].filename == '<string>'
assert getattr(lazy, 'missing', None) is None
assert getattr(lazy, '_module_getattr', None) is None
assert getattr(lazy, 'module_getattr', None) is None
try:
    lazy.missing
except AttributeError as exc:
    assert str(exc) == 'missing'
else:
    raise AssertionError('missing attribute was accepted')
''', extra={'__init__.py': '''
__all__ = ['Alias']
def __getattr__(name):
    if name == 'Alias':
        import warnings
        warnings.warn('Alias', DeprecationWarning, stacklevel=2)
        from .core import Used
        return Used
    raise AttributeError(name)
'''})
    result, report = check(package, output, {})
    assert result.returncode == 0, result.stdout + result.stderr


def test_required_suites_cannot_be_substituted_with_a_small_command(tmp_path):
    package, output = audit(tmp_path, 'class Used: pass\n', 'import audit_subject')
    allowance = output / 'allow.json'
    allowance.write_text('{}')
    result = subprocess.run(
        [sys.executable, '-m', 'tools.execution_audit', 'check', '--package', str(package),
         '--output', str(output), '--allowlist', str(allowance), '--require-suites'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0


def test_new_unexercised_definition_fails_an_existing_allowlist(tmp_path):
    first = tmp_path / 'first'
    first.mkdir()
    package, output = audit(first, 'class Used: pass\nOLD = 1\n', 'import audit_subject')
    _, report = check(package, output, {})
    allowance = {key: 'Known optional extension not called in this fixture.' for key in report['candidates']}
    second = tmp_path / 'second'
    second.mkdir()
    package, output = audit(second, 'class Used: pass\nOLD = 1\ndef added(): return 2\n', 'import audit_subject')
    result, report = check(package, output, allowance)
    assert result.returncode == 1
    assert report['unexpected'] == ['function:audit_subject/core.py:added']


def test_shadowed_or_rebound_globals_do_not_exercise_the_original_binding(tmp_path):
    package, output = audit(tmp_path, '''
VALUE = 3
class Used:
    VALUE = 3
    alias = VALUE
def read(): return VALUE
''', '''
import types
import audit_subject.core as core
foreign = vars(core).copy()
assert types.FunctionType(core.read.__code__, foreign)() == 3
assert core.Used.alias == 3
''')
    _, report = check(package, output, {})
    assert 'binding:audit_subject/core.py:VALUE' in report['candidates']


def test_initialization_failure_is_a_failing_run(tmp_path):
    bootstrap = f'''
import sys, runpy
sys.path.insert(0, {sysconfig.get_path('purelib')!r})
sys.monitoring.use_tool_id(sys.monitoring.DEBUGGER_ID, 'occupied')
runpy.run_path({str(ROOT / 'tools/security/covproc/sitecustomize.py')!r})
'''
    with pytest.raises(AssertionError, match='Execution audit initialization failed'):
        audit(tmp_path, 'class Used: pass\n', [sys.executable, '-S', '-c', bootstrap])
    assert list((tmp_path / 'audit').glob('initialization-error-*'))


def test_reexec_keeps_work_before_and_after_the_exec(tmp_path):
    package, output = audit(tmp_path, '''
class Used: pass
BEFORE = 1
AFTER = 2
def before_exec(): assert BEFORE == 1
def after_exec(): assert AFTER == 2
''', '''
import os, sys
import audit_subject.core as core
core.before_exec()
os.execv(sys.executable, [sys.executable, '-c', 'import audit_subject.core as core; core.after_exec()'])
''')
    result, report = check(package, output, {})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not report['candidates']


def test_new_definitions_inside_an_unexecuted_branch_are_candidates(tmp_path):
    package, output = audit(tmp_path, '''
class Used: pass
if False:
    def added(): return 1
    class Hidden: pass
    NEVER_READ = 2
''', 'import audit_subject')
    result, report = check(package, output, {})
    assert result.returncode == 1
    assert {(v['kind'], v['name']) for v in report['candidates'].values()} == {
        ('function', 'added'), ('class', 'Hidden'), ('binding', 'NEVER_READ')}


def test_optimized_python_cannot_disable_the_gate(tmp_path):
    package, output = audit(tmp_path, 'class Used: pass\nUNREAD = 1\n', 'import audit_subject')
    allow = output / 'allow.json'
    allow.write_text('{}')
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('COVERAGE_', 'BB_EXECUTION_AUDIT_'))}
    result = subprocess.run(
        [sys.executable, '-O', '-m', 'tools.execution_audit', 'check', '--package', str(package),
         '--output', str(output), '--allowlist', str(allow)],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'optimized' in result.stderr


@pytest.mark.parametrize('methods', [False, True])
def test_redefined_classes_do_not_exercise_the_previous_definition(tmp_path, methods):
    first = 'def marker(self): return 1' if methods else 'pass'
    second = 'def marker(self): return 2' if methods else 'pass'
    package, output = audit(tmp_path, f'''
class Used: pass
class Repeated:
    {first}
class Repeated:
    {second}
''', 'import audit_subject.core as core\ninstance = core.Repeated()\n' +
        ('assert instance.marker() == 2\n' if methods else ''))
    result, report = check(package, output, {})
    assert 'class:audit_subject/core.py:Repeated' in report['candidates']
    assert 'class:audit_subject/core.py:Repeated#2' not in report['candidates']
    if methods:
        assert 'function:audit_subject/core.py:Repeated.marker' in report['candidates']
        assert 'function:audit_subject/core.py:Repeated.marker#2' not in report['candidates']


def test_failed_run_saves_a_diagnostic_report(tmp_path):
    package = tmp_path / 'audit_subject'
    package.mkdir()
    (package / '__init__.py').write_text('UNREAD = 1\n')
    allowance = tmp_path / 'allow.json'
    allowance.write_text('{}')
    output = tmp_path / 'audit'
    result = subprocess.run(
        [sys.executable, '-m', 'tools.execution_audit', 'run', '--package', str(package),
         '--output', str(output), '--allowlist', str(allowance), '--', sys.executable,
         '-c', 'import audit_subject; raise SystemExit(1)'],
        cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    report = json.loads((output / 'report.json').read_text())
    assert report['error']
