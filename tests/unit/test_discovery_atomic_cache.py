from concurrent.futures import ThreadPoolExecutor

import pytest

from fabric_kg_builder.contracts.base import canonical_json
from fabric_kg_builder.domain import discovery


class CacheRecord(discovery._Hashed):
    value: int


def test_cache_becomes_visible_only_after_sync(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    record = discovery._seal(CacheRecord, value=1)
    expected = (canonical_json(record) + "\n").encode()
    original_link = discovery.os.link

    def publish(staging, final):
        assert not final.exists()
        assert staging.read_bytes() == expected
        return original_link(staging, final)

    monkeypatch.setattr(discovery.os, "link", publish)
    discovery._write(path, record)
    assert path.read_bytes() == expected
    assert not list(tmp_path.glob("*.pending"))


def test_interrupted_cache_write_leaves_no_poisoned_final_entry(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    record = discovery._seal(CacheRecord, value=1)

    def interrupted(_fd):
        assert not path.exists()
        raise OSError("simulated interrupted write")

    with monkeypatch.context() as patch:
        patch.setattr(discovery.os, "fsync", interrupted)
        with pytest.raises(OSError, match="interrupted"):
            discovery._write(path, record)
    assert not path.exists()
    assert not list(tmp_path.glob("*.pending"))
    discovery._write(path, record)
    assert path.exists()


def test_concurrent_identical_cache_writes_preserve_create_only_semantics(tmp_path):
    path = tmp_path / "cache.json"
    record = discovery._seal(CacheRecord, value=1)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: discovery._write(path, record), range(16)))
    original = path.read_bytes()
    assert original == (canonical_json(record) + "\n").encode()
    assert not list(tmp_path.glob("*.pending"))
    with pytest.raises(ValueError, match="create-only"):
        discovery._write(path, discovery._seal(CacheRecord, value=2))
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.pending"))
