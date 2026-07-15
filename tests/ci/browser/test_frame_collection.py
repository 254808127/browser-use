from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.dom.service import DomService


@pytest.mark.parametrize(
	('kwargs', 'expected_metadata_calls'),
	[
		({}, 1),
		({'include_metadata': False}, 0),
	],
)
async def test_get_all_frames_can_skip_optional_metadata(
	monkeypatch: pytest.MonkeyPatch,
	kwargs: dict[str, bool],
	expected_metadata_calls: int,
) -> None:
	metadata_calls: list[tuple[dict[str, dict], dict[str, str]]] = []

	async def get_no_targets(self: BrowserSession, **kwargs: Any) -> list[dict[str, Any]]:
		del self, kwargs
		return []

	async def record_metadata(
		self: BrowserSession,
		all_frames: dict[str, dict],
		target_sessions: dict[str, str],
	) -> None:
		del self
		metadata_calls.append((all_frames, target_sessions))

	monkeypatch.setattr(BrowserSession, '_cdp_get_all_pages', get_no_targets)
	monkeypatch.setattr(BrowserSession, '_populate_frame_metadata', record_metadata)
	session = BrowserSession(browser_profile=BrowserProfile(cross_origin_iframes=True))

	assert await session.get_all_frames(**kwargs) == ({}, {})
	assert len(metadata_calls) == expected_metadata_calls


async def test_dom_oopif_target_map_uses_active_targets_without_cdp_fanout() -> None:
	iframe_target = SimpleNamespace(
		target_id='frame-id',
		target_type='iframe',
		url='https://iframe.example',
		title='Iframe',
	)
	page_target = SimpleNamespace(
		target_id='page-id',
		target_type='page',
		url='https://page.example',
		title='Page',
	)
	browser_session = SimpleNamespace(
		logger=logging.getLogger('test_dom_oopif_target_map'),
		session_manager=SimpleNamespace(
			get_all_targets=lambda: {
				iframe_target.target_id: iframe_target,
				page_target.target_id: page_target,
			}
		),
	)
	service = DomService(browser_session=browser_session)  # type: ignore[arg-type]

	assert service._get_oopif_target_map() == {
		'frame-id': {
			'id': 'frame-id',
			'url': 'https://iframe.example',
			'title': 'Iframe',
			'frameTargetId': 'frame-id',
			'isCrossOrigin': True,
		}
	}


async def test_cdp_client_for_frame_uses_active_oopif_target_without_frame_scan(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	requested_targets: list[tuple[str | None, bool]] = []
	expected_session = object()

	async def get_session(
		self: BrowserSession,
		target_id: str | None = None,
		focus: bool = True,
	) -> object:
		del self
		requested_targets.append((target_id, focus))
		return expected_session

	async def fail_frame_scan(self: BrowserSession, **kwargs: Any) -> tuple[dict, dict]:
		del self, kwargs
		raise AssertionError('active OOPIF lookup must not scan every target')

	monkeypatch.setattr(BrowserSession, 'get_or_create_cdp_session', get_session)
	monkeypatch.setattr(BrowserSession, 'get_all_frames', fail_frame_scan)
	session = BrowserSession(browser_profile=BrowserProfile(cross_origin_iframes=True))
	session.session_manager = SimpleNamespace(
		get_target=lambda target_id: SimpleNamespace(target_id=target_id, target_type='iframe')
	)

	assert await session.cdp_client_for_frame('frame-id') is expected_session
	assert requested_targets == [('frame-id', False)]
