import asyncio
import base64
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest

from browser_use.browser.watchdogs.downloads_watchdog import _DOWNLOAD_CHUNK_SIZE, DownloadsWatchdog


def make_watchdog(tmp_path: Path, payload: bytes, *, encoded: bool = True):
	position = 0

	async def call_function(*, params, session_id):
		function = params['functionDeclaration']
		if 'fetch(' in function:
			return {'result': {'value': {'size': len(payload), 'from_cache': True}}}
		if 'return this.blob;' in function:
			return {'result': {'objectId': 'blob-object'}}
		return {'result': {'type': 'undefined'}}

	async def read(*, params, session_id):
		nonlocal position
		chunk = payload[position : position + params['size']]
		position += len(chunk)
		return {
			'data': base64.b64encode(chunk).decode() if encoded else chunk.decode(),
			'base64Encoded': encoded,
			'eof': position == len(payload),
		}

	runtime = SimpleNamespace(
		evaluate=AsyncMock(return_value={'result': {'objectId': 'download-state'}}),
		callFunctionOn=AsyncMock(side_effect=call_function),
		releaseObjectGroup=AsyncMock(return_value={}),
	)
	io = SimpleNamespace(
		resolveBlob=AsyncMock(return_value={'uuid': 'test-blob'}),
		read=AsyncMock(side_effect=read),
		close=AsyncMock(return_value={}),
	)
	send = SimpleNamespace(Runtime=runtime, IO=io)
	session = SimpleNamespace(cdp_client=SimpleNamespace(send=send), session_id='target-session')
	browser = SimpleNamespace(
		get_or_create_cdp_session=AsyncMock(return_value=session),
		logger=logging.getLogger('download-test'),
		browser_profile=SimpleNamespace(downloads_path=tmp_path),
	)
	watchdog = DownloadsWatchdog.model_construct(browser_session=browser, event_bus=Mock())
	return watchdog, send


@pytest.mark.asyncio
@pytest.mark.parametrize('encoded', [False, True])
async def test_stream_download_preserves_bytes_and_bounds_reads(tmp_path, encoded):
	payload = (bytes(range(256)) if encoded else b'file,data\n') * 65537
	watchdog, send = make_watchdog(tmp_path, payload, encoded=encoded)
	destination = tmp_path / 'report.bin'
	metadata = await watchdog._stream_download_from_url('https://example.com/file', 'target', destination)
	assert destination.read_bytes() == payload
	assert metadata.size == len(payload)
	assert metadata.from_cache
	assert send.IO.read.await_count > 1
	assert all(c.kwargs['params']['size'] == _DOWNLOAD_CHUNK_SIZE for c in send.IO.read.await_args_list)
	assert all(c.kwargs['session_id'] == 'target-session' for c in send.IO.read.await_args_list)
	assert list(tmp_path.iterdir()) == [destination]
	send.IO.close.assert_awaited_once_with(params={'handle': 'blob:test-blob'}, session_id='target-session')
	send.Runtime.releaseObjectGroup.assert_awaited_once()
	assert 'abort()' in send.Runtime.callFunctionOn.await_args.kwargs['params']['functionDeclaration']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['truncated', 'oversized', 'empty_chunk', 'invalid_base64', 'disconnect', 'timeout'])
async def test_failed_stream_preserves_destination_and_removes_partial(tmp_path, failure):
	watchdog, send = make_watchdog(tmp_path, b'expected')
	destination = tmp_path / 'existing.bin'
	destination.write_bytes(b'original')
	if failure == 'disconnect':
		send.IO.read.side_effect = ConnectionError('connection closed')
	elif failure == 'timeout':
		send.IO.read.side_effect = TimeoutError('read timeout')
	else:
		send.IO.read.side_effect = None
		send.IO.read.return_value = {
			'truncated': {'data': 'short', 'eof': True},
			'oversized': {'data': 'longer than expected', 'eof': True},
			'empty_chunk': {'data': '', 'eof': False},
			'invalid_base64': {'data': '***', 'base64Encoded': True, 'eof': True},
		}[failure]
	with pytest.raises((ValueError, ConnectionError, TimeoutError)):
		await watchdog._stream_download_from_url('https://example.com/file', 'target', destination)
	assert destination.read_bytes() == b'original'
	assert list(tmp_path.iterdir()) == [destination]
	send.IO.close.assert_awaited_once()
	send.Runtime.releaseObjectGroup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['fetch', 'read'])
async def test_cancellation_aborts_fetch_and_releases_resources(tmp_path, phase):
	watchdog, send = make_watchdog(tmp_path, b'download')
	entered = asyncio.Event()

	async def wait_forever(**kwargs):
		entered.set()
		await asyncio.Future()

	if phase == 'read':
		send.IO.read.side_effect = wait_forever
	else:
		original = send.Runtime.callFunctionOn.side_effect

		async def wait_for_fetch(**kwargs):
			if 'fetch(' in kwargs['params']['functionDeclaration']:
				return await wait_forever(**kwargs)
			return await original(**kwargs)

		send.Runtime.callFunctionOn.side_effect = wait_for_fetch
	task = asyncio.create_task(watchdog._stream_download_from_url('https://example.com/file', 'target', tmp_path / 'file'))
	await asyncio.wait_for(entered.wait(), timeout=2)
	task.cancel()
	with pytest.raises(asyncio.CancelledError):
		await task
	assert not list(tmp_path.iterdir())
	assert send.IO.close.await_count == (1 if phase == 'read' else 0)
	assert 'abort()' in send.Runtime.callFunctionOn.await_args.kwargs['params']['functionDeclaration']
	send.Runtime.releaseObjectGroup.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_fetch_exception_does_not_publish_file(tmp_path):
	watchdog, send = make_watchdog(tmp_path, b'download')
	send.Runtime.callFunctionOn.side_effect = None
	send.Runtime.callFunctionOn.return_value = {'exceptionDetails': {'text': 'HTTP error'}}
	with pytest.raises(RuntimeError, match='Browser download failed'):
		await watchdog._stream_download_from_url('https://example.com/file', 'target', tmp_path / 'file')
	assert not list(tmp_path.iterdir())
	send.IO.read.assert_not_awaited()
	send.Runtime.releaseObjectGroup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['close', 'abort', 'release'])
@pytest.mark.parametrize('existing', [False, True])
async def test_cancellation_during_cleanup_does_not_publish_file(tmp_path, phase, existing):
	watchdog, send = make_watchdog(tmp_path, b'complete')
	destination = tmp_path / 'report.bin'
	if existing:
		destination.write_bytes(b'original')
	entered = asyncio.Event()
	finish = asyncio.Event()

	async def pause_cleanup(**kwargs):
		entered.set()
		await finish.wait()
		return {}

	if phase == 'close':
		send.IO.close.side_effect = pause_cleanup
	elif phase == 'release':
		send.Runtime.releaseObjectGroup.side_effect = pause_cleanup
	else:
		original = send.Runtime.callFunctionOn.side_effect

		async def pause_abort(**kwargs):
			if 'abort()' in kwargs['params']['functionDeclaration']:
				return await pause_cleanup(**kwargs)
			return await original(**kwargs)

		send.Runtime.callFunctionOn.side_effect = pause_abort
	task = asyncio.create_task(watchdog._stream_download_from_url('https://example.com/file', 'target', destination))
	try:
		await asyncio.wait_for(entered.wait(), timeout=2)
		# Publication must wait for cleanup even on the success path.
		assert destination.read_bytes() == b'original' if existing else not destination.exists()
		task.cancel()
		await asyncio.sleep(0)
		assert not task.done()
		task.cancel()
		await asyncio.sleep(0)
		assert not task.done()
	finally:
		finish.set()
		with pytest.raises(asyncio.CancelledError):
			await asyncio.wait_for(task, timeout=2)
	assert destination.read_bytes() == b'original' if existing else not destination.exists()
	assert not list(tmp_path.glob('.browser-use-download-*.part'))
	send.IO.close.assert_awaited_once()
	assert 'abort()' in send.Runtime.callFunctionOn.await_args.kwargs['params']['functionDeclaration']
	send.Runtime.releaseObjectGroup.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_failure_does_not_lose_completed_file_or_skip_release(tmp_path):
	watchdog, send = make_watchdog(tmp_path, b'complete')
	send.IO.close.side_effect = ConnectionError('closed')
	await watchdog._stream_download_from_url('https://example.com/file', 'target', tmp_path / 'file')
	assert (tmp_path / 'file').read_bytes() == b'complete'
	send.Runtime.releaseObjectGroup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('pdf', [False, True])
async def test_download_entrypoints_keep_file_metadata_and_duplicate_cache(tmp_path, pdf):
	payload = b'%PDF-1.7\nfile' if pdf else b'col,value\na,1\n'
	watchdog, send = make_watchdog(tmp_path, payload)
	url = 'https://example.com/report.pdf' if pdf else 'https://example.com/report.csv'
	if pdf:
		send.Runtime.evaluate.side_effect = [
			{'result': {'value': {'url': url}}},
			{'result': {'objectId': 'download-state'}},
			{'result': {'value': {'url': url}}},
		]
		result = await watchdog.trigger_pdf_download('target')
		cached = await watchdog.trigger_pdf_download('target')
	else:
		result = await watchdog.download_file_from_url(url, 'target', content_type='text/csv', suggested_filename='report.csv')
		cached = await watchdog.download_file_from_url(url, 'target', content_type='text/csv', suggested_filename='report.csv')
	assert result == cached
	assert result is not None
	assert Path(result).read_bytes() == payload
	dispatch = cast(Mock, watchdog.event_bus.dispatch)
	dispatch.assert_called_once()
	event = dispatch.call_args.args[0]
	assert event.file_size == len(payload)
	assert event.path == result
	assert event.file_name == ('report.pdf' if pdf else 'report.csv')
	send.IO.resolveBlob.assert_awaited_once()
