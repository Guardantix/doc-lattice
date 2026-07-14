"""Tests for load-cache location and persistence."""

import json
from pathlib import Path

import pytest

from doc_lattice import __version__
from doc_lattice.cache import CacheFile, Entry, NodePayload, SectionRecordModel, StatRecord
from doc_lattice.cache.store import StoreSnapshot, cache_home, cache_path, load, save_if_changed
from doc_lattice.constants import CACHE_FILE_NAME, CACHE_VERSION
from doc_lattice.model import NodeMeta


def _sample_cache_file() -> CacheFile:
    return CacheFile(
        version=CACHE_VERSION,
        tool_version=__version__,
        roots=["/abs/root"],
        entries={
            "docs/a.md": Entry(
                file_sha256="a" * 64,
                stats={"/abs/root": StatRecord(size=10, mtime_ns=123)},
                node=NodePayload(
                    meta=NodeMeta.model_validate({"id": "a"}),
                    body="# A\n",
                    total_lines=1,
                    sections=[SectionRecordModel(anchor="a-top", start=1, end=1)],
                ),
            )
        },
    )


def _write_cache(path: Path, cache_file: CacheFile) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cache_file.model_dump_json(), encoding="utf-8")


def test_cache_home_uses_absolute_xdg():
    home = cache_home({"XDG_CACHE_HOME": "/custom/cache", "HOME": "/home/u"})
    assert home == Path("/custom/cache")


def test_cache_home_ignores_relative_xdg():
    home = cache_home({"XDG_CACHE_HOME": "relative/cache", "HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_home_falls_back_to_home_dot_cache_when_xdg_unset():
    home = cache_home({"HOME": "/home/u"})
    assert home == Path("/home/u/.cache")


def test_cache_home_falls_back_to_home_cache_dir_when_home_and_xdg_unset():
    home = cache_home({})
    assert home == Path.home() / ".cache"


def test_cache_path_composes_slot_and_file_name():
    path = cache_path("my-docs", {"XDG_CACHE_HOME": "/c", "HOME": "/home/u"})
    assert path == Path("/c") / "doc-lattice" / "my-docs" / CACHE_FILE_NAME


def test_load_missing_file_is_empty(tmp_path: Path):
    assert load(tmp_path / CACHE_FILE_NAME) == StoreSnapshot(cache=None, baseline=None)


def test_load_valid_file_returns_cache_and_baseline(tmp_path: Path):
    original = _sample_cache_file()
    path = tmp_path / CACHE_FILE_NAME
    _write_cache(path, original)

    snapshot = load(path)

    assert snapshot.cache == original
    assert snapshot.baseline == original.model_dump(mode="json")


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{ not json",
        '{"version": 1}',
    ],
)
def test_load_corrupt_file_is_empty(tmp_path: Path, text: str):
    path = tmp_path / CACHE_FILE_NAME
    path.write_text(text, encoding="utf-8")
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_load_invalid_utf8_is_empty(tmp_path: Path):
    path = tmp_path / CACHE_FILE_NAME
    path.write_bytes(b"\xff\xfe not valid utf-8\n")
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_load_wrong_version_is_empty(tmp_path: Path):
    bad = _sample_cache_file().model_copy(update={"version": 999})
    path = tmp_path / CACHE_FILE_NAME
    _write_cache(path, bad)
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_load_wrong_tool_version_is_empty(tmp_path: Path):
    bad = _sample_cache_file().model_copy(update={"tool_version": "0.0.0-other"})
    path = tmp_path / CACHE_FILE_NAME
    _write_cache(path, bad)
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_load_invalid_meta_is_empty(tmp_path: Path):
    path = tmp_path / CACHE_FILE_NAME
    payload = _sample_cache_file().model_dump(mode="json")
    payload["entries"]["docs/a.md"]["node"]["meta"]["id"] = "bad#id"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert load(path) == StoreSnapshot(cache=None, baseline=None)


def test_save_unchanged_baseline_skips_write_and_directory_creation(tmp_path: Path):
    final = _sample_cache_file()
    path = tmp_path / "missing" / CACHE_FILE_NAME

    save_if_changed(path, final, final.model_dump(mode="json"))

    assert not path.parent.exists()


def test_save_without_baseline_writes_revalidatable_cache(tmp_path: Path):
    final = _sample_cache_file()
    path = tmp_path / "new" / CACHE_FILE_NAME

    save_if_changed(path, final, None)

    written = CacheFile.model_validate_json(path.read_text(encoding="utf-8"))
    assert written == final


def test_save_differing_baseline_writes(tmp_path: Path):
    baseline = _sample_cache_file()
    final = baseline.model_copy(update={"roots": ["/different/root"]})
    path = tmp_path / "changed" / CACHE_FILE_NAME

    save_if_changed(path, final, baseline.model_dump(mode="json"))

    written = CacheFile.model_validate_json(path.read_text(encoding="utf-8"))
    assert written == final


def test_save_delegates_atomic_persistence_with_cache_prefix(tmp_path: Path, monkeypatch):
    import doc_lattice.cache.store as store_module  # noqa: PLC0415

    final = _sample_cache_file()
    path = tmp_path / "new" / CACHE_FILE_NAME
    captured: list[tuple[Path, bytes, str]] = []

    def _capture(target: Path, data: bytes, *, prefix: str) -> None:
        captured.append((target, data, prefix))

    monkeypatch.setattr(store_module, "atomic_replace_bytes", _capture)

    save_if_changed(path, final, None)

    assert len(captured) == 1
    target, data, prefix = captured[0]
    assert target == path
    assert CacheFile.model_validate_json(data) == final
    assert prefix == f"{CACHE_FILE_NAME}."


def test_save_persistence_failure_emits_one_stderr_line_and_is_swallowed(
    tmp_path: Path, capsys, monkeypatch
):
    import doc_lattice.cache.store as store_module  # noqa: PLC0415

    path = tmp_path / "failed" / CACHE_FILE_NAME

    def _boom(*args, **kwargs):  # noqa: ARG001
        raise OSError("disk full")

    monkeypatch.setattr(store_module, "atomic_replace_bytes", _boom)
    result = save_if_changed(path, _sample_cache_file(), None)

    captured = capsys.readouterr()
    assert result is None
    assert captured.err.count("\n") == 1
    assert "cache" in captured.err.lower()


def test_save_persistence_failure_flattens_cleanup_notes_into_warning(
    tmp_path: Path, capsys, monkeypatch
):
    import doc_lattice.cache.store as store_module  # noqa: PLC0415

    path = tmp_path / "failed" / CACHE_FILE_NAME
    error = OSError("disk full")
    error.add_note("exact helper-owned orphan remediation")

    def _boom(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(store_module, "atomic_replace_bytes", _boom)

    result = save_if_changed(path, _sample_cache_file(), None)

    captured = capsys.readouterr()
    assert result is None
    assert captured.err == (
        f"doc-lattice: could not write load cache at {path}: "
        "disk full; exact helper-owned orphan remediation\n"
    )
