from __future__ import annotations

from pathlib import Path

import pytest

from harness.storage.atomic import (
    SAFE_ID,
    append_jsonl,
    assert_safe_filename,
    atomic_write_json,
    atomic_write_text,
    new_id,
    now_ts,
    read_json,
    read_jsonl,
)


def test_ids_are_safe_and_unique(tmp_path: Path) -> None:
    ids = {new_id("mem") for _ in range(100)}
    assert len(ids) == 100
    assert all(SAFE_ID.match(item) for item in ids)


def test_timestamps_are_utc_iso(tmp_path: Path) -> None:
    ts = now_ts()
    assert ts.endswith("Z")
    assert "T" in ts


def test_atomic_write_uses_utf8_and_lf(tmp_path: Path) -> None:
    path = tmp_path / "record.json"
    atomic_write_text(path, '{"text":"héllo"}\n', retries=2, retry_s=0)
    raw = path.read_bytes()
    assert b"\r\n" not in raw
    assert "héllo".encode("utf-8") in raw
    assert read_json(path)["text"] == "héllo"


def test_append_jsonl_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "log.jsonl"
    append_jsonl(path, {"a": 1}, retries=2, retry_s=0)
    append_jsonl(path, {"a": 2}, retries=2, retry_s=0)
    assert [row["a"] for row in read_jsonl(path)] == [1, 2]


def test_unsafe_filenames_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        atomic_write_json(tmp_path / "con.json", {}, retries=1, retry_s=0)
    with pytest.raises(ValueError):
        atomic_write_json(tmp_path / "domain:one.json", {}, retries=1, retry_s=0)
    assert_safe_filename("domain_one.json")
