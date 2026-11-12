"""Collect execution evidence and check explicitly reviewed, unexercised symbols."""
from __future__ import annotations

import argparse
import ast
import atexit
import dis
import hashlib
import importlib.machinery
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from functools import partial

ROOT = Path(__file__).resolve().parents[1]
SUITES = ('unit', 'architecture', 'properties', 'integration', 'conformance')


def inventory(package: Path) -> dict:
    files, symbols = {}, {}
    for path in sorted(package.rglob('*.py')):
        relative = path.relative_to(package.parent).as_posix()
        source = path.read_bytes()
        files[relative] = hashlib.sha256(source).hexdigest()
        module = relative[:-3].replace('/', '.').removesuffix('.__init__')
        counters = {}

        class Visitor(ast.NodeVisitor):
            prefix = ''
            scoped = False

            def add(self, kind, name, node, **fields):
                key = f'{kind}:{relative}:{name}'
                counters[key] = counters.get(key, 0) + 1
                if counters[key] > 1:
                    key += f'#{counters[key]}'
                symbols[key] = dict(kind=kind, file=relative, name=name,
                                    line=node.lineno, module=module, **fields)

            def definition(self, node, kind):
                name = self.prefix + node.name
                decorators = [d.lineno for d in node.decorator_list]
                body = {n.lineno for statement in node.body for n in ast.walk(statement)
                        if hasattr(n, 'lineno')} - {node.lineno}
                self.add(kind, name, node, first_line=min([node.lineno, *decorators]), end_line=node.end_lineno,
                         body=sorted(body))
                prefix, scoped = self.prefix, self.scoped
                self.prefix = name + ('.<locals>.' if kind == 'function' else '.')
                self.scoped = True
                for statement in node.body:
                    self.visit(statement)
                self.prefix, self.scoped = prefix, scoped

            def visit_FunctionDef(self, node):
                self.definition(node, 'function')

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_ClassDef(self, node):
                self.definition(node, 'class')

            def visit_Assign(self, node):
                if not self.scoped:
                    for target in node.targets:
                        for name in ast.walk(target):
                            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                                if name.id != '_':
                                    self.add('binding', name.id, node)
                self.generic_visit(node)

            def visit_AnnAssign(self, node):
                if not self.scoped and node.value is not None and isinstance(node.target, ast.Name):
                    self.add('binding', node.target.id, node)
                self.generic_visit(node)

        Visitor().visit(ast.parse(source))
    return dict(files=files, symbols=symbols)


class Recorder:
    def __init__(self, package: Path, output: Path):
        self.package, self.output = package, output
        self.generation = uuid.uuid4().hex
        self.manifest = json.loads((output / 'inventory.json').read_text())
        self.entered, self.reads, self.classes, self.modules = set(), set(), set(), set()
        self.tests = set()
        self.untraced = set()
        self.instructions = {}
        self.names = {}
        for symbol in self.manifest['symbols'].values():
            if symbol['kind'] == 'binding' or '.' not in symbol['name']:
                self.names.setdefault(symbol['file'], set()).add(symbol['name'])
        self.monitor = sys.monitoring
        self.tool = self.monitor.DEBUGGER_ID

    def relative(self, filename):
        path = Path(filename)
        return (path.relative_to(self.package.parent).as_posix()
                if path.is_absolute() and path.is_relative_to(self.package) else None)

    def reference(self, file, name, value):
        self.reads.add(f'{file}:{name}')
        if isinstance(value, type):
            module = type.__getattribute__(value, '__module__')
            qualified = type.__getattribute__(value, '__qualname__')
            first_line = type.__getattribute__(value, '__dict__').get('__firstlineno__')
            self.classes.add(f'{module}:{qualified}:{first_line}')

    def entered_code(self, code, offset):
        relative = self.relative(code.co_filename)
        if relative in self.manifest['files']:
            self.modules.add(relative)
            if code.co_flags & inspect.CO_NEWLOCALS and code.co_name != '__annotate__':
                self.entered.add(f'{relative}:{code.co_qualname}:{code.co_firstlineno}')
            names = self.names.get(relative, ())
            instructions = {i.offset: (i.opname, i.argval) for i in dis.get_instructions(code)
                            if i.opname in ('LOAD_GLOBAL', 'LOAD_NAME') and i.argval in names}
            if instructions:
                self.instructions[code] = (relative, instructions)
                self.monitor.set_local_events(self.tool, code, self.monitor.events.INSTRUCTION)
        return self.monitor.DISABLE

    def instruction(self, code, offset):
        file, instructions = self.instructions[code]
        instruction = instructions.get(offset)
        if instruction is None:
            return self.monitor.DISABLE
        opcode, name = instruction
        frame = sys._getframe(1)
        try:
            if opcode == 'LOAD_NAME' and frame.f_locals is not frame.f_globals and name in frame.f_locals:
                return self.monitor.DISABLE
            module = sys.modules.get(frame.f_globals.get('__name__'))
            if (module is not None and vars(module) is frame.f_globals and name in frame.f_globals
                    and frame.f_globals.get('__file__') == code.co_filename):
                self.reference(file, name, frame.f_globals[name])
                return self.monitor.DISABLE
        finally:
            frame = None
        return None

    def save(self):
        path = self.output / f'observed-{os.getpid()}-{self.generation}.json'
        data = dict(token=self.manifest['token'], entered=sorted(self.entered),
                    reads=sorted(self.reads), classes=sorted(self.classes), modules=sorted(self.modules),
                    tests=sorted(self.tests), untraced=sorted(self.untraced))
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(data))
        temporary.replace(path)

    def start(self):
        recorder = self

        def module_getattr(module):
            namespace = object.__getattribute__(module, '__dict__')
            return namespace.get('__getattr__', partial(types.ModuleType.__getattribute__, module))

        class Module(types.ModuleType):
            def __getattribute__(self, name):
                namespace = object.__getattribute__(self, '__dict__')
                if name == '__getattr__' and name not in namespace:
                    raise AttributeError(name)
                value = namespace[name] if name in namespace else object.__getattribute__(self, name)
                if file := recorder.relative(namespace.get('__file__', '')):
                    recorder.reference(file, name, value)
                return value

            __getattr__ = property(module_getattr)

        class Loader:
            def __init__(self, original):
                self.original = original

            def __getattr__(self, name):
                return getattr(self.original, name)

            def create_module(self, spec):
                return self.original.create_module(spec)

            def exec_module(self, module):
                module.__class__ = Module
                self.original.exec_module(module)

        class Finder:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == recorder.package.name or fullname.startswith(recorder.package.name + '.'):
                    spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                    if spec and spec.origin and recorder.relative(spec.origin) and spec.loader:
                        spec.loader = Loader(spec.loader)
                        return spec
                return None

        self.monitor.use_tool_id(self.tool, 'blackbull-execution-audit')
        self.monitor.register_callback(self.tool, self.monitor.events.PY_START, self.entered_code)
        self.monitor.register_callback(self.tool, self.monitor.events.INSTRUCTION, self.instruction)
        sys.meta_path.insert(0, Finder())
        self.monitor.set_events(self.tool, self.monitor.events.PY_START)
        original_exit = os._exit

        def exit_process(status):
            try:
                self.save()
            finally:
                original_exit(status)

        os._exit = exit_process
        for name in ('execv', 'execve'):
            original = getattr(os, name)

            def exec_process(*args, function=original):
                self.save()
                return function(*args)

            setattr(os, name, exec_process)
        atexit.register(self.save)


_recorder = None


def coverage_init(registry, options):
    global _recorder
    if not os.environ.get('BB_EXECUTION_AUDIT_DIR') or _recorder is not None:
        return
    if sys.flags.optimize:
        raise ValueError('Execution audit does not support optimized Python')
    package = Path(os.environ['BB_EXECUTION_AUDIT_PACKAGE'])
    output = Path(os.environ['BB_EXECUTION_AUDIT_DIR'])
    _recorder = Recorder(package, output)
    _recorder.start()


def pytest_runtest_logreport(report):
    if _recorder is not None and report.when == 'call' and report.passed:
        parts = report.nodeid.split('/')
        if len(parts) > 1 and parts[0] == 'tests' and parts[1] in SUITES:
            _recorder.tests.add(report.nodeid)


def pytest_collection_modifyitems(config, items):
    if _recorder is None:
        return
    import pytest
    for item in items:
        if marker := item.get_closest_marker('no_audit'):
            reason = marker.kwargs.get('reason')
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError('no_audit needs an explicit instrumentation reason')
            file, *names = item.nodeid.split('::')
            identity = '.'.join([file[:-3].replace('/', '.'), *names[:-1]]) + '::' + names[-1]
            _recorder.untraced.add(identity)
            item.add_marker(pytest.mark.skip(reason=f'Verified in the untraced audit phase: {reason}'))


def pytest_sessionfinish(session, exitstatus):
    if _recorder is not None:
        _recorder.save()


def collect(package: Path, output: Path, command: list[str], untraced=False) -> int:
    if sys.version_info < (3, 14) or sys.implementation.name != 'cpython':
        raise ValueError('The execution audit lane requires CPython 3.14 or later.')
    output.mkdir(parents=True)
    manifest = dict(token=uuid.uuid4().hex, **inventory(package))
    (output / 'inventory.json').write_text(json.dumps(manifest, indent=2))
    if untraced:
        clean = {k: v for k, v in os.environ.items()
                 if not k.startswith(('COVERAGE_', 'BB_EXECUTION_AUDIT_'))}
        control = [sys.executable, '-m', 'pytest', *(f'tests/{s}' for s in SUITES),
                   '--run-integration', '-m', 'no_audit', '--timeout=120', '-q',
                   '--junitxml', str(output / 'untraced.xml')]
        result = subprocess.run(control, env=clean, timeout=300)
        if result.returncode:
            return result.returncode
    config = output / 'coverage.ini'
    config.write_text(f'''[run]
source = {package}
data_file = {output / '.coverage'}
parallel = True
concurrency = multiprocessing
patch = subprocess, fork, _exit, execv
plugins = tools.execution_audit
''')
    env = {**os.environ, 'COVERAGE_PROCESS_START': str(config), 'COVERAGE_CORE': 'ctrace',
           'BB_EXECUTION_AUDIT_DIR': str(output), 'BB_EXECUTION_AUDIT_PACKAGE': str(package)}
    env.pop('COVERAGE_PROCESS_CONFIG', None)
    env['PYTHONPATH'] = os.pathsep.join([str(ROOT / 'tools/security/covproc'), str(ROOT),
                                      str(package.parent), env.get('PYTHONPATH', '')])
    result = subprocess.run(command, env=env, timeout=1200)
    (output / 'command.json').write_text(json.dumps(dict(command=command, exit_code=result.returncode)))
    return result.returncode


def report(package: Path, output: Path, allowlist: Path, require_suites=False) -> dict:
    from coverage import Coverage
    manifest = json.loads((output / 'inventory.json').read_text())
    assert inventory(package) == {k: manifest[k] for k in ('files', 'symbols')}, 'Source changed after collection'
    command = json.loads((output / 'command.json').read_text())
    if require_suites:
        assert all(f'tests/{name}' in command['command'] for name in SUITES), 'Missing test suite'
        assert '--run-integration' in command['command'], 'Integration tests were not enabled'
    records = [json.loads(p.read_text()) for p in output.glob('observed-*.json')]
    assert not list(output.glob('initialization-error-*')), 'A child audit initialization failed'
    assert records and all(r['token'] == manifest['token'] for r in records), 'Missing or stale observations'
    observed = {name: set().union(*(r.get(name, ()) for r in records))
                for name in ('entered', 'reads', 'classes', 'modules', 'untraced', 'tests')}
    assert observed['modules'], 'No target modules were executed'
    suites = Counter(test.split('/')[1] for test in observed['tests'])
    if require_suites:
        assert all(suites[name] for name in SUITES), 'A required suite executed no passing tests'
        control = ET.parse(output / 'untraced.xml')
        passed = {f"{case.attrib['classname']}::{case.attrib['name']}" for case in control.iter('testcase')
                  if not any(case.find(tag) is not None for tag in ('failure', 'error', 'skipped'))}
        assert observed['untraced'] <= passed, 'An untraced test was not verified'
    coverage = Coverage(data_file=str(output / '.coverage'), config_file=False)
    coverage.combine(data_paths=[str(output)], strict=True, keep=True)
    coverage.save()
    data = coverage.get_data()
    candidates = {}
    all_keys = set(manifest['symbols'])
    for relative in manifest['files']:
        key = f'module:{relative}'
        all_keys.add(key)
        if not data.lines(str(package.parent / relative)):
            candidates[key] = dict(kind='module', file=relative, name=relative, line=1)
    for key, symbol in manifest['symbols'].items():
        lines = set(data.lines(str(package.parent / symbol['file'])) or ())
        kind, name, file = symbol['kind'], symbol['name'], symbol['file']
        if kind == 'binding':
            missing = f'{file}:{name}' not in observed['reads']
        elif kind == 'function':
            missing = (f"{file}:{name}:{symbol['first_line']}" not in observed['entered']
                       and not set(symbol['body']) & lines)
        else:
            missing = (f"{symbol['module']}:{name}:{symbol['first_line']}" not in observed['classes'] and not any(
                s.startswith(f'{file}:{name}.') and
                symbol['first_line'] <= int(s.rsplit(':', 1)[1]) <= symbol['end_line']
                for s in observed['entered']))
        if missing:
            candidates[key] = symbol
    allowed = json.loads(allowlist.read_text())
    assert isinstance(allowed, dict) and all(isinstance(v, str) and v.strip() for v in allowed.values()), 'Every allowance needs a reason'
    assert not allowed.keys() - all_keys, 'Allowlist refers to removed or renamed definitions'
    return dict(error='Test command failed' if command['exit_code'] else None,
                candidates=candidates, unexpected=sorted(candidates.keys() - allowed.keys()),
                observed={k: sorted(v) for k, v in observed.items()},
                processes=len(records), suites=dict(suites),
                limitations=['direct_module_dict_reads', 'dynamic_namespaces', 'abrupt_process_death'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['collect', 'check', 'run'])
    parser.add_argument('--package', type=Path, default=ROOT / 'blackbull')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--allowlist', type=Path, default=ROOT / 'tools/execution_audit_allowlist.json')
    parser.add_argument('--require-suites', action='store_true')
    args, command = parser.parse_known_args()
    if sys.flags.optimize:
        parser.error('Execution audit does not support optimized Python')
    command = command[1:] if command[:1] == ['--'] else command
    package = args.package.resolve()
    if args.output is None and args.action == 'check':
        parser.error('check requires --output from a completed collection')
    output = (args.output or ROOT / 'build/execution-audit' /
              f'{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}-{uuid.uuid4().hex[:8]}').resolve()
    if args.action in ('collect', 'run'):
        default = [sys.executable, '-m', 'pytest', *(f'tests/{s}' for s in SUITES),
                   '--run-integration', '--timeout=120', '-q', '-n', 'auto', '-p', 'tools.execution_audit']
        code = collect(package, output, command or default, untraced=not command)
        if args.action == 'collect':
            return code
    try:
        result = report(package, output, args.allowlist, args.require_suites or args.action == 'run')
    except (AssertionError, OSError, ValueError) as exc:
        result = dict(error=str(exc), candidates={}, unexpected=[])
    (output / 'report.json').write_text(json.dumps(result, indent=2))
    print(output)
    print(json.dumps({k: v for k, v in result.items() if k not in ('observed', 'candidates')}, indent=2))
    return int(bool(result.get('error') or result['unexpected']))


if __name__ == '__main__':
    raise SystemExit(main())
