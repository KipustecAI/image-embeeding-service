"""Unit tests for scripts/check_zip_range.py (read-only Range check, frames desde ZIP).

No network: the same ``FakeStore`` (httpx.MockTransport honouring Range) as
tests/test_zip_frame_reader.py. Also asserts the script only sends GETs plus
the one presign POST (it must never write).
"""

import importlib.util
import sys
import zipfile
from pathlib import Path

from tests.test_zip_frame_reader import (
    CROPPED,
    EVIDENCE_ZIP,
    PRIVATE_URL,
    PUBLIC_URL,
    ROOT,
    STORAGE,
    FakeStore,
    build_zip,
)

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_zip_range.py"
_spec = importlib.util.spec_from_file_location("check_zip_range", _PATH)
check = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_zip_range", check)  # dataclasses look the module up
_spec.loader.exec_module(check)


def _methods(store: FakeStore) -> set[tuple[str, str]]:
    return {(r.method, r.url.path) for r in store.requests}


async def test_public_zip_passes_with_two_range_gets():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        report = await check.run_check(
            client, PUBLIC_URL, zip_entry="cropped/20261021-001529_457445.jpg"
        )
    assert report.ok, check.format_report(report)
    assert report.mode == "public"
    assert report.entry_bytes == len(CROPPED)
    assert [s.status for s in report.steps if s.status is not None] == [206, 206]
    assert all(r.method == "GET" and r.headers.get("range") for r in store.requests)
    assert len(store.requests) == 2
    assert "RESULT:  PASS" in check.format_report(report)


async def test_private_zip_is_presigned_and_signature_not_printed():
    store = FakeStore({PRIVATE_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        report = await check.run_check(
            client,
            PRIVATE_URL,
            image_name="20261021-001529_457445.jpg",
            storage_service_url=STORAGE,
        )
    assert report.ok, check.format_report(report)
    assert report.mode == "presigned"
    assert report.entry_bytes == len(ROOT)
    # Only the presign POST plus GETs: nothing is written.
    assert {m for m, _ in _methods(store)} == {"POST", "GET"}
    assert [p for m, p in _methods(store) if m == "POST"] == ["/internal/presigned-url"]
    assert "Signature" not in check.format_report(report)


async def test_store_ignoring_range_fails_the_check():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP}, ignore_range=True)
    async with store.client() as client:
        report = await check.run_check(client, PUBLIC_URL)
    assert not report.ok
    assert report.steps[0].status == 200
    assert "RESULT:  FAIL" in check.format_report(report)


async def test_missing_entry_and_missing_object_fail():
    store = FakeStore({PUBLIC_URL: EVIDENCE_ZIP})
    async with store.client() as client:
        missing_entry = await check.run_check(client, PUBLIC_URL, zip_entry="nope.jpg")
        missing_obj = await check.run_check(
            client, "https://storage.lookia.mx/lucam-assets/public/none.zip"
        )
    assert not missing_entry.ok and missing_entry.steps[-1].name == "select entry"
    assert not missing_obj.ok and missing_obj.steps[0].status == 404


async def test_central_directory_before_tail_uses_one_more_range():
    big = [(f"f{i:05d}_{'x' * 200}.jpg", b"j", zipfile.ZIP_STORED) for i in range(400)]
    data = build_zip(big)
    store = FakeStore({PUBLIC_URL: data})
    async with store.client() as client:
        report = await check.run_check(client, PUBLIC_URL)
    assert report.ok, check.format_report(report)
    assert [s.name for s in report.steps][:2] == ["tail range GET", "central-directory range GET"]


def test_pure_helpers():
    assert check.parse_content_range("bytes 10-19/100") == (10, 19, 100)
    assert check.parse_content_range(None) is None
    assert check.parse_content_range("items 1-2/3") is None
    assert check.redact_url("https://h/lucam-assets/a.zip?X-Amz-Signature=s") == (
        "https://h/lucam-assets/a.zip"
    )
    assert check.main(["ftp://x"]) == 2
