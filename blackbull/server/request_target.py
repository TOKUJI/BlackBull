"""Path-plus-query decomposition shared by the HTTP wire parsers."""
from urllib.parse import unquote


def split_path_query(target: bytes) -> tuple[str, bytes, bytes]:
    # Split before decoding: encoded delimiters are path data.  Leading //
    # belongs to the path, not an authority as in a generic URI reference.
    without_fragment, _, _ = target.partition(b'#')
    raw_path, _, query = without_fragment.partition(b'?')
    path = raw_path.decode('utf-8')
    if b'%' in raw_path:
        path = unquote(path, encoding='utf-8', errors='replace')
    return path, raw_path, query
