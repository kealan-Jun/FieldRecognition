"""Keep isolated temporary paths short enough for Linux Unix-domain sockets."""

import hashlib
import pytest


@pytest.fixture
def tmp_path(request, tmp_path_factory):
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:10]
    return tmp_path_factory.mktemp(name)
