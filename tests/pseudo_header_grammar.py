"""Pseudo-header grammar cases — one definition for both suites.

``tests/unit/test_parser.py`` grades the rules at the ``Connection`` boundary
and ``tests/conformance/http2/test_rfc9113_gaps.py`` at the wire.  Both need
the same values inside and outside the grammars, and two spellings of one
table would let one suite be updated alone and disagree about what the rule
covers.
"""

#: RFC 9110 §5.6.2 — a method is ``1*tchar``, so every separator, HTAB, SP and
#: the empty value are outside it.
ILLEGAL_METHODS = [
    b'M\tT', b'M T', b'M(T', b'M)T', b'M,T', b'M/T', b'M:T', b'M;T',
    b'M<T', b'M=T', b'M>T', b'M?T', b'M@T', b'M[T', b'M\\T', b'M]T',
    b'M{T', b'M}T', b'M"T', b'',
]

#: RFC 3986 §3.1 — a scheme is ``ALPHA *( ALPHA / DIGIT / "+" / "-" / "." )``,
#: so a leading digit and every separator are outside it.  ``a_b`` is the
#: telling one: an underscore is a §5.6.2 tchar, so only the scheme grammar
#: refuses it.
ILLEGAL_SCHEMES = [
    b'1http', b'ht,tp', b'ht tp', b'a_b', b'a:b', b'a/b', b'a;b',
    b'a?b', b'a@b', b'a[b', b'a\\b', b'a]b', b'a{b', b'a}b', b'a"b',
    b'a(b', b'a)b', b'a<b', b'a>b', b'a=b', b'a\tb', b'',
]

#: Inside both grammars — the controls that keep a narrowed rule from passing
#: by refusing everything.  ``M-SEARCH`` and ``a+b-c.d`` take the extension
#: points; ``1``, ``gEt`` and ``HTTP`` pin that neither grammar reads
#: case or a leading digit as special where it is not.
LEGAL_METHODS = [b'GET', b'HEAD', b'M-SEARCH', b'X.Y', b'1', b'gEt']
LEGAL_SCHEMES = [b'https', b'http', b'h', b'a+b-c.d', b'HTTP']
