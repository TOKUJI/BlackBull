"""Fixtures for the third-party servers the differential cells point at."""
import pytest

from ._reference_peers import NGHTTPD, NGINX


@pytest.fixture(scope='module')
def nginx_peer(request):
    yield from NGINX.serve(request)


@pytest.fixture(scope='module')
def nghttpd_peer(request):
    yield from NGHTTPD.serve(request)
