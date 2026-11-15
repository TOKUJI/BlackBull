"""Fixtures for the third-party servers the differential cells point at."""
import pytest

from ._reference_peers import NGHTTPD, NGINX, PEERS


def pytest_collection_modifyitems(items):
    """Run the image preparers first: a peer waits for its preparer's build,
    so one queued after a peer on the same worker would wait out the budget."""
    preparers = {peer.preparer for peer in PEERS}
    items.sort(key=lambda item: item.name not in preparers)


@pytest.fixture(scope='module')
def nginx_peer(request):
    yield from NGINX.serve(request)


@pytest.fixture(scope='module')
def nghttpd_peer(request):
    yield from NGHTTPD.serve(request)
