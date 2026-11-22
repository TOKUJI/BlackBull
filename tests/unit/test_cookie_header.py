"""``cookie_header`` refuses what would rewrite or break the Set-Cookie line.

RFC 6265 §4.1.1: cookie-name is an RFC 9110 token, cookie-value is
cookie-octets (optionally in double quotes), and path-value is any CHAR
except CTLs and ';'.
"""
import pytest

from blackbull import cookie_header


def test_a_valid_cookie_is_emitted_unchanged():
    assert cookie_header('session', 'abc123', path='/app') == (
        b'set-cookie', b'session=abc123; Path=/app; HttpOnly; SameSite=Lax')


@pytest.mark.parametrize('value', ['', 'a.b-c_d~!#$%&\'()*+/:<=>?@[]^`{|}', '"quoted"'])
def test_every_cookie_octet_and_a_quoted_value_are_accepted(value):
    assert cookie_header('n', value)[1].startswith(f'n={value};'.encode())


@pytest.mark.parametrize('value', [
    'b; Domain=evil.example', 'a\r\nset-cookie: x=y', 'two words', 'a,b',
    'back\\slash', 'quo"te', '"unterminated', 'café', 'tab\there',
])
def test_a_value_outside_cookie_octets_is_refused(value):
    with pytest.raises(ValueError, match='value'):
        cookie_header('n', value)


@pytest.mark.parametrize('name', ['', 'a b', 'a;b', 'a=b', 'a,b', '(n)', 'café', 'a\nb'])
def test_a_name_outside_the_token_grammar_is_refused(name):
    with pytest.raises(ValueError, match='name'):
        cookie_header(name, 'v')


@pytest.mark.parametrize('path', ['/a;Domain=evil.example', '/a\r\nx: y', '/a\x00'])
def test_a_path_with_a_separator_or_control_is_refused(path):
    with pytest.raises(ValueError, match='path'):
        cookie_header('n', 'v', path=path)
