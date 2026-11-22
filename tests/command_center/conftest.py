"""Disposable archives and roots for Command Center tests (synthetic fixture only)."""

import shutil

import pytest

from vader_intelligence.command_center.config import CenterConfig

from ._synthetic import build_archive, two_game_fixture


@pytest.fixture
def dev_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    return root


@pytest.fixture
def two_game_archive(tmp_path):
    fixture = two_game_fixture(tmp_path / "two.json")
    return build_archive(tmp_path / "archive.sqlite3", fixture)


@pytest.fixture
def cfg(dev_root, two_game_archive):
    return CenterConfig(root=dev_root, archive=two_game_archive, labels={"archive": "fixture"})


@pytest.fixture
def free_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def copy_archive(src, dst):
    shutil.copy(src, dst)
    return dst
