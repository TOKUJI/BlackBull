import subprocess
import sys


def test_integration_conftest_preserves_later_typechecking(child_env):
    script = '''
import tests.integration.conftest
from beartype.claw import beartype_package
from beartype.roar import BeartypeCallHintParamViolation
beartype_package('blackbull')
from blackbull import Response
try:
    Response(b'ok', status='invalid')
except BeartypeCallHintParamViolation:
    pass
else:
    raise AssertionError('integration conftest bypassed the typechecking hook')
'''
    result = subprocess.run([sys.executable, '-c', script], env=child_env(),
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
