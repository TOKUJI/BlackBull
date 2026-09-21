"""Small primitives shared by resource-owning cleanup paths."""


def combine_cleanup_errors(*errors):
    """Keep one error's identity; group distinct concurrent failures."""
    unique = []
    seen = set()
    for error in errors:
        if error is not None and id(error) not in seen:
            unique.append(error)
            seen.add(id(error))
    if len(unique) < 2:
        return unique[0] if unique else None
    return BaseExceptionGroup('cleanup failed', unique)
