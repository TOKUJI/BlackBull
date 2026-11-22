"""The ``http.response.start`` headers a response emits survive pickle
with their fields and framing facts."""
import copy
import pickle

import pytest

from blackbull.native import NativeResponse


@pytest.mark.parametrize('fields', [
    [(b'content-length', b'3'), (b'date', b'x')],
    [(b'transfer-encoding', b'chunked'), (b'connection', b'close')],
    [(b'x-a', b'1')],
], ids=['content-length', 'connection-specific', 'plain'])
@pytest.mark.parametrize('clone', [
    lambda h: pickle.loads(pickle.dumps(h)), copy.copy, copy.deepcopy,
], ids=['pickle', 'copy', 'deepcopy'])
def test_a_cloned_section_equals_the_original(fields, clone):
    head = NativeResponse(status=200, header=fields, body=b'abc').to_asgi()[0]['headers']
    twin = clone(head)
    assert type(twin) is type(head) and twin == head
    twin.add(b'content-length', b'4')
    assert twin.content_length[-1] == (b'content-length', b'4')
