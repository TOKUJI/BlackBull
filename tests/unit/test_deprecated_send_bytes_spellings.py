"""``SendBytes`` and ``H2CSendBytes`` warn wherever they are reached.

Each deprecated spelling still yields the class it always did, and warns at
the caller's own line naming the replacement *a caller of that module* should
use.  In ``blackbull.fault_injection`` that is ``H1CSendRawBytes``: a bare
``SendRawBytes`` there is the HTTP/2 *server* step, which an HTTP/1.1 client
scenario rejects as an unknown step type without raising.

Not naming a deprecated spelling stays silent — importing a package, ``import
*`` and the replacements themselves — so none of them is listed in the
``__all__`` of a module that resolves it lazily.
"""
import re
import subprocess
import sys
import warnings

import pytest

import blackbull.client
import blackbull.client.http1
import blackbull.fault_injection
from blackbull.fault_injection import scenario_h1, scenario_h2_client

REMOVAL_FLOOR = '2027-08-19'


def _from_client_import():
    from blackbull.client import SendBytes
    return SendBytes


def _from_http1_import():
    from blackbull.client.http1 import SendBytes
    return SendBytes


def _from_fault_injection_import():
    from blackbull.fault_injection import SendBytes
    return SendBytes


def _from_fault_injection_import_h2c():
    from blackbull.fault_injection import H2CSendBytes
    return H2CSendBytes


#: (access, the replacement its warning must name)
SEND_BYTES_ACCESSES = [
    pytest.param(lambda: blackbull.client.SendBytes, 'SendRawBytes',
                 id='client-attribute'),
    pytest.param(_from_client_import, 'SendRawBytes', id='client-from-import'),
    pytest.param(lambda: blackbull.client.http1.SendBytes, 'SendRawBytes',
                 id='client.http1-attribute'),
    pytest.param(_from_http1_import, 'SendRawBytes',
                 id='client.http1-from-import'),
    pytest.param(lambda: blackbull.fault_injection.SendBytes, 'H1CSendRawBytes',
                 id='fault_injection-attribute'),
    pytest.param(_from_fault_injection_import, 'H1CSendRawBytes',
                 id='fault_injection-from-import'),
]

H2C_SEND_BYTES_ACCESSES = [
    pytest.param(lambda: blackbull.fault_injection.H2CSendBytes,
                 id='fault_injection-attribute'),
    pytest.param(_from_fault_injection_import_h2c,
                 id='fault_injection-from-import'),
]


def _quietly(access):
    """The value an access yields, with any warning it emits recorded away."""
    with warnings.catch_warnings(record=True):
        warnings.simplefilter('always')
        return access()


def _bare_send_raw_bytes(message: str) -> bool:
    return re.search(r'\bSendRawBytes\b', message) is not None


# ---------------------------------------------------------------------------
# Reaching a deprecated spelling warns, naming the right replacement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('access, replacement', SEND_BYTES_ACCESSES)
def test_send_bytes_warns_naming_its_replacement(access, replacement):
    with pytest.warns(DeprecationWarning) as record:
        access()
    messages = [str(w.message) for w in record
                if issubclass(w.category, DeprecationWarning)]
    assert any(REMOVAL_FLOOR in m and re.search(rf'\b{replacement}\b', m)
               for m in messages), messages
    if replacement == 'H1CSendRawBytes':
        assert not any(_bare_send_raw_bytes(m) for m in messages), (
            'a bare SendRawBytes in blackbull.fault_injection is the HTTP/2 '
            f'server step: {messages}')


@pytest.mark.parametrize('access', H2C_SEND_BYTES_ACCESSES)
def test_h2c_send_bytes_warns_naming_its_replacement(access):
    with pytest.warns(DeprecationWarning) as record:
        access()
    messages = [str(w.message) for w in record
                if issubclass(w.category, DeprecationWarning)]
    assert any(REMOVAL_FLOOR in m and 'H2CSendRawBytes' in m
               for m in messages), messages
    assert not any(_bare_send_raw_bytes(m) for m in messages), messages


def _from_scenario_h1_import():
    from blackbull.fault_injection.scenario_h1 import SendBytes
    return SendBytes


def _from_scenario_h2_client_import():
    from blackbull.fault_injection.scenario_h2_client import SendBytes
    return SendBytes


#: The deprecated spelling each vocabulary module resolves for itself.
MODULE_SHIM_ACCESSES = [
    pytest.param(lambda: scenario_h1.SendBytes, id='scenario_h1-attribute'),
    pytest.param(_from_scenario_h1_import, id='scenario_h1-from-import'),
    pytest.param(lambda: scenario_h2_client.SendBytes,
                 id='scenario_h2_client-attribute'),
    pytest.param(_from_scenario_h2_client_import,
                 id='scenario_h2_client-from-import'),
]


@pytest.mark.parametrize(
    'access', [p.values[0] for p in SEND_BYTES_ACCESSES]
    + [p.values[0] for p in H2C_SEND_BYTES_ACCESSES]
    + [p.values[0] for p in MODULE_SHIM_ACCESSES],
    ids=[f'SendBytes-{p.id}' for p in SEND_BYTES_ACCESSES]
    + [f'H2CSendBytes-{p.id}' for p in H2C_SEND_BYTES_ACCESSES]
    + [f'SendBytes-{p.id}' for p in MODULE_SHIM_ACCESSES])
def test_the_warning_is_attributed_to_the_callers_line(access):
    """Why every deprecation shim's ``__getattr__`` carries no annotation.

    Each shim warns with ``stacklevel=2``, which names the caller's line only
    while ``__getattr__`` is the frame directly below the caller.  ``just
    typecheck`` runs beartype's import hook over ``blackbull``, and the hook
    wraps every module-level function that has an annotation; inside that
    wrapper, ``stacklevel=2`` names the wrapper instead.  An unannotated
    function is left unwrapped, so the warning reaches the caller under both
    configurations — and this test fails under ``just typecheck`` the moment a
    shim gains an annotation.
    """
    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter('always')
        access()
    assert any(issubclass(w.category, DeprecationWarning) and w.filename == __file__
               for w in record), [(w.filename, str(w.message)) for w in record]


# ---------------------------------------------------------------------------
# Every deprecated spelling still yields the class it always did
# ---------------------------------------------------------------------------

def test_send_bytes_yields_the_http1_client_step_everywhere():
    h1_step = scenario_h1.SendRawBytes
    assert _quietly(lambda: blackbull.client.SendBytes) is h1_step
    assert _quietly(lambda: blackbull.client.http1.SendBytes) is h1_step
    assert _quietly(lambda: blackbull.fault_injection.SendBytes) is h1_step
    assert (_quietly(lambda: blackbull.fault_injection.SendBytes)
            is not blackbull.fault_injection.SendRawBytes)
    assert _quietly(lambda: scenario_h1.SendBytes) is scenario_h1.SendRawBytes
    assert (_quietly(lambda: scenario_h2_client.SendBytes)
            is scenario_h2_client.SendRawBytes)


def test_h2c_send_bytes_yields_the_http2_client_step():
    h2c = _quietly(lambda: blackbull.fault_injection.H2CSendBytes)
    assert h2c is scenario_h2_client.SendRawBytes
    assert h2c is not blackbull.fault_injection.SendRawBytes


# ---------------------------------------------------------------------------
# Not naming a deprecated spelling stays silent
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('module', [
    pytest.param(blackbull.client, id='blackbull.client'),
    pytest.param(blackbull.fault_injection, id='blackbull.fault_injection'),
    pytest.param(scenario_h1, id='scenario_h1'),
])
def test_send_bytes_is_not_exported(module):
    assert 'SendBytes' not in module.__all__


def test_h2c_send_bytes_is_not_exported():
    assert 'H2CSendBytes' not in blackbull.fault_injection.__all__


@pytest.mark.parametrize('module_name', [
    'blackbull.client',
    'blackbull.client.http1',
    'blackbull.fault_injection',
    'blackbull.fault_injection.scenario_h1',
    'blackbull.fault_injection.scenario_h2_client',
])
def test_star_import_is_silent(module_name):
    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        exec(f'from {module_name} import *', {})


def test_importing_the_packages_is_silent():
    modules = ('blackbull.client, blackbull.fault_injection, '
               'blackbull.fault_injection.scenario_h1, '
               'blackbull.fault_injection.scenario_h2_client, '
               'blackbull.client.http1')
    completed = subprocess.run(
        [sys.executable, '-W', 'error::DeprecationWarning', '-c',
         f'import {modules}'],
        capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_the_replacements_import_silently():
    with warnings.catch_warnings():
        warnings.simplefilter('error', DeprecationWarning)
        from blackbull.fault_injection import (  # noqa: F401
            H1CSendRawBytes, H2CSendRawBytes, SendRawBytes,
        )


def test_the_deprecated_scenario_shim_keeps_its_export():
    """``blackbull.client.scenario`` warns on import, so listing the name there
    cannot make ``import *`` warn code that never asked for it."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        import blackbull.client.scenario as shim
    assert 'SendBytes' in shim.__all__
    assert shim.SendBytes is scenario_h1.SendRawBytes
