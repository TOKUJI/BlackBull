"""Unit tests for the native response message (native-ization, Sprint 92).

Each test protects the unified NativeResponse contract at the component
boundary: presence semantics (``is not None``), DX header/body properties,
and the ``to_asgi()`` boundary conversion.  No sockets, no actor loop.
"""
import pytest

from blackbull.native import NativeResponse


# ---------------------------------------------------------------------------
# Construction & presence semantics
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_defaults(self):
        r = NativeResponse()
        assert r.status == 200
        assert r.header is None
        assert r.body is None
        assert r.more_body is False
        assert r.trailers is None
        assert r.expects_trailers is False

    def test_full_response(self):
        r = NativeResponse(status=201,
                           header=[(b'content-type', b'text/plain')],
                           body=b'Hello')
        assert r.status == 201
        assert r.header is not None
        assert r.body == b'Hello'

    def test_presence_is_not_none(self):
        # presence is decided by `is not None`, never by truthiness: an
        # empty body is a real body (204-style) and must not be confused
        # with "absent".
        r = NativeResponse(body=b'')
        assert r.body is not None
        assert r.is_empty
        assert NativeResponse().body is None


class TestHeaderView:
    def test_get(self):
        r = NativeResponse(header=[(b'content-type', b'text/plain'),
                                   (b'x-a', b'1')])
        hv = r.header
        assert hv is not None
        assert hv.get(b'content-type') == b'text/plain'
        assert hv.get(b'missing', b'def') == b'def'

    def test_getlist(self):
        r = NativeResponse(header=[(b'x-a', b'1'), (b'x-b', b'2'), (b'x-a', b'3')])
        hv = r.header
        assert hv is not None
        assert hv.getlist(b'x-a') == [(b'x-a', b'1'), (b'x-a', b'3')]

    def test_contains(self):
        r = NativeResponse(header=[(b'content-type', b'text/plain')])
        hv = r.header
        assert hv is not None
        assert b'content-type' in hv
        assert b'x-missing' not in hv

    def test_len_and_iter(self):
        r = NativeResponse(header=[(b'a', b'1'), (b'b', b'2')])
        hv = r.header
        assert hv is not None
        assert len(hv) == 2
        assert list(hv) == [(b'a', b'1'), (b'b', b'2')]

    def test_append_is_zero_copy(self):
        # the view wraps the underlying list; a mutation must be visible
        # to anything reading the response afterwards (e.g. to_asgi).
        r = NativeResponse(header=[(b'a', b'1')])
        hv = r.header
        assert hv is not None
        hv.append(b'content-encoding', b'gzip')
        assert (b'content-encoding', b'gzip') in r.to_asgi()[0]['headers']

    def test_append_single_tuple_is_one_pair(self):
        # A bare (name, value) 2-tuple in the one-arg form must be treated as
        # one pair, not extended element-wise (extend() would walk the two
        # bytes as separate malformed entries — the blackbull-session footgun).
        r = NativeResponse(header=[])
        hv = r.header
        assert hv is not None
        hv.append((b'set-cookie', b'a=b'))
        assert list(hv) == [(b'set-cookie', b'a=b')]
        assert hv.get(b'set-cookie') == b'a=b'

    def test_append_list_of_pairs_still_extends(self):
        r = NativeResponse(header=[])
        hv = r.header
        assert hv is not None
        hv.append([(b'a', b'1'), (b'b', b'2')])
        assert list(hv) == [(b'a', b'1'), (b'b', b'2')]

    def test_header_absent_returns_none(self):
        assert NativeResponse().header is None
        assert NativeResponse(body=b'x').header is None


class TestBodyDX:
    def test_content_length(self):
        assert NativeResponse(body=b'Hello').content_length == 5
        assert NativeResponse().content_length == 0

    def test_is_empty(self):
        assert NativeResponse().is_empty
        assert NativeResponse(body=b'').is_empty
        assert not NativeResponse(body=b'x').is_empty

    def test_content_type(self):
        r = NativeResponse(header=[(b'content-type', b'application/json')])
        assert r.content_type == b'application/json'
        assert NativeResponse().content_type == b''


# ---------------------------------------------------------------------------
# to_asgi() boundary conversion
# ---------------------------------------------------------------------------

class TestToASGI:
    def test_complete_response(self):
        r = NativeResponse(status=200,
                           header=[(b'content-type', b'text/plain')],
                           body=b'Hi')
        events = r.to_asgi()
        assert events == [
            {'type': 'http.response.start',
             'status': 200, 'headers': [(b'content-type', b'text/plain')]},
            {'type': 'http.response.body', 'body': b'Hi', 'more_body': False},
        ]

    def test_header_only(self):
        r = NativeResponse(status=204, header=[(b'x-a', b'1')])
        assert r.to_asgi() == [
            {'type': 'http.response.start', 'status': 204,
             'headers': [(b'x-a', b'1')]},
        ]

    def test_body_only(self):
        r = NativeResponse(body=b'chunk', more_body=True)
        assert r.to_asgi() == [
            {'type': 'http.response.body', 'body': b'chunk', 'more_body': True},
        ]

    def test_trailers(self):
        r = NativeResponse(trailers=[(b'x-trailer', b'v')])
        assert r.to_asgi() == [
            {'type': 'http.response.trailers', 'headers': [(b'x-trailer', b'v')]},
        ]

    def test_expects_trailers_flag_on_start(self):
        # the ASGI start `trailers: True` flag is preserved losslessly
        # through the native form and back at the boundary.
        r = NativeResponse(status=200,
                           header=[(b'content-type', b'text/plain')],
                           expects_trailers=True)
        events = r.to_asgi()
        assert events[0]['trailers'] is True
        assert NativeResponse(status=200, header=[]).to_asgi()[0].get('trailers') is None


class TestSlots:
    def test_no_dict_per_instance(self):
        # __slots__ — no per-instance __dict__; guards accidental attr drift.
        r = NativeResponse()
        assert not hasattr(r, '__dict__')

    def test_unknown_attr_rejected(self):
        r = NativeResponse()
        with pytest.raises(AttributeError):
            r.typo_field = 1
