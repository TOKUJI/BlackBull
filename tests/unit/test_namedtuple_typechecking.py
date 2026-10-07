import subprocess
import sys

import pytest


@pytest.mark.parametrize('module,name,fields,arguments,bad_arguments', [
    ('blackbull.connection', '_FieldSpec',
     ('attr', 'scope_key', 'to_scope', 'from_scope'),
     "attr='path', scope_key='path', to_scope=str, from_scope=str",
     "attr='path', scope_key='path', to_scope=1, from_scope=str"),
    ('blackbull.client.http1', '_PreparedRequest', ('head', 'body', 'declared'),
     "head=b'head', body=b'body', declared=4", "head=b'head', body=1, declared=4"),
    ('blackbull.router', '_QuerySpec', ('name', 'type', 'coercer', 'required', 'default'),
     "name='limit', type=int, coercer=int, required=True, default=None",
     "name='limit', type=int, coercer=1, required=True, default=None"),
    ('blackbull.grpc.registry', 'GrpcMethod', ('handler', 'streaming', 'client_streaming'),
     'handler=str, streaming=False', 'handler=1, streaming=False'),
    ('blackbull.mqtt.messages', 'PropertyInfo', ('identifier', 'name', 'wire_type'),
     "identifier=PropertyId.CONTENT_TYPE, name='content_type', wire_type='utf8'",
     "identifier='invalid', name='content_type', wire_type='utf8'"),
    ('blackbull.mqtt.extension', 'Subscription', ('topic', 'callback'),
     "topic='t', callback=str", "topic='t', callback=1"),
])
def test_namedtuple_contract_under_package_typechecking(module, name, fields, arguments, bad_arguments):
    script = f'''
from beartype.claw import beartype_package
from beartype.roar import BeartypeCallHintParamViolation
beartype_package('blackbull')
from {module} import {name} as Record
from blackbull.mqtt.messages import PropertyId
record = Record({arguments})
assert Record.__module__ == {module!r}
assert Record._fields == {fields!r}
assert isinstance(record, tuple)
assert record[0] == tuple(record)[0]
assert record._replace(**{{Record._fields[0]: record[0]}}) == record
assert Record(*record) == record
if {name!r} == 'GrpcMethod':
    assert record.client_streaming is False
    assert Record._field_defaults == {{'client_streaming': False}}
try:
    Record({bad_arguments})
except BeartypeCallHintParamViolation:
    pass
else:
    raise AssertionError('package type checking did not reject the invalid field')
'''
    completed = subprocess.run([sys.executable, '-c', script], capture_output=True,
                               text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
