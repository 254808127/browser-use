"""Exercise Blob stream handles against real Chromium, without mocking CDP."""

import base64
from pathlib import Path

import pytest

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.browser.watchdogs.downloads_watchdog import _DOWNLOAD_CHUNK_SIZE, DownloadsWatchdog


@pytest.mark.asyncio
async def test_stream_download_with_real_browser(tmp_path: Path):
	# Binary data spanning multiple IO.read calls, including a short final chunk.
	payload = bytes(range(256)) * (_DOWNLOAD_CHUNK_SIZE // 256 * 2 + 1)
	# A data URL keeps the test independent of network access while exercising the real fetch/Blob/IO path.
	url = 'data:application/octet-stream;base64,' + base64.b64encode(payload).decode('ascii')
	session = BrowserSession(
		browser_profile=BrowserProfile(
			headless=True,
			user_data_dir=None,
			enable_default_extensions=False,
			auto_download_pdfs=False,
			downloads_path=tmp_path,
		)
	)
	try:
		await session.start()
		target_id = session.agent_focus_target_id
		assert target_id is not None
		watchdog = DownloadsWatchdog.model_construct(browser_session=session, event_bus=session.event_bus)
		destination = tmp_path / 'payload.bin'
		metadata = await watchdog._stream_download_from_url(url, target_id, destination)
		assert metadata.size == len(payload)
		assert destination.read_bytes() == payload
		assert not list(tmp_path.glob('.browser-use-download-*.part'))
	finally:
		await session.kill()
		await session.event_bus.stop(clear=True, timeout=5)
