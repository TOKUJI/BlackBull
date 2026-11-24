"""Path-plus-query decomposition shared by the HTTP wire parsers."""
from functools import lru_cache
from urllib.parse import unquote

from ..protocol.http1 import MessageError

# RFC 9112 §2.1 / RFC 3986 — a request-target may carry only visible ASCII.
TARGET_ALLOWED_OCTETS = bytes(range(0x21, 0x7F))


# Clients repeat their targets; bounded because the bytes are the peer's.
@lru_cache(maxsize=128)
def split_path_query(target: bytes) -> tuple[str, bytes, bytes]:
    """Split an origin-form *target* of visible ASCII (MAL-NON-ASCII-URL);
    ``MessageError`` for anything else."""
    if target[:1] != b'/' or target.translate(None, TARGET_ALLOWED_OCTETS):
        raise MessageError(f'invalid request-target {target!r}')
    # Split before decoding: encoded delimiters are path data.  Leading //
    # belongs to the path, not an authority as in a generic URI reference.
    without_fragment, _, _ = target.partition(b'#')
    raw_path, _, query = without_fragment.partition(b'?')
    path = raw_path.decode('utf-8')
    if b'%' in raw_path:
        path = unquote(path, encoding='utf-8', errors='replace')
    return path, raw_path, query
