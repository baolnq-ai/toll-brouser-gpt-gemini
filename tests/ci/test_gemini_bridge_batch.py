from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


SERVER_FILE = Path(__file__).resolve().parents[2] / 'examples' / 'apps' / 'gemini-use' / 'server.py'


@pytest.fixture(scope='module')
def server_module():
	spec = importlib.util.spec_from_file_location('gemini_use_server', SERVER_FILE)
	assert spec is not None
	assert spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = module
	spec.loader.exec_module(module)
	return module


def test_extract_prompts(server_module):
	prompts = server_module._extract_prompts(['one', 'two', ' three '])
	assert prompts == ['one', 'two', 'three']

	with pytest.raises(server_module.AutomationError) as error:
		server_module._extract_prompts([])

	assert error.value.code == 'PROMPT_REQUIRED'


@pytest.mark.asyncio
async def test_open_web_accepts_multi_ports(monkeypatch, server_module):
	async def fake_open_web(self, *, port, url, new_tab, force_reconnect):
		return {
			'provider': self.cfg.provider,
			'port': port,
			'cdp_url': self.cdp_url_for_port(port),
			'navigated_url': url or self.cfg.default_open_url,
			'provider_tab_ready': True,
		}

	async def fake_probe_port(self, port):
		return server_module.PortStatus(
			port=port,
			active=True,
			cdp_url=self.cdp_url_for_port(port),
			managed_by=[self.cfg.provider],
			browser='Chromium',
			web_socket_debugger_url=f'ws://127.0.0.1:{port}/devtools/browser/mock',
		)

	monkeypatch.setattr(server_module.GeminiBridgeService, 'open_web', fake_open_web)
	monkeypatch.setattr(server_module.GeminiBridgeService, 'probe_port', fake_probe_port)

	async with AsyncClient(transport=ASGITransport(app=server_module.app), base_url='http://test') as client:
		response = await client.post(
			'/v1/web/open',
			json={
				'ports': [9222, 9223],
			},
		)

	assert response.status_code == 200
	payload = response.json()
	assert payload['success'] is True
	assert len(payload['results']) == 2


@pytest.mark.asyncio
async def test_chat_batch_rate_limit_failover(monkeypatch, server_module):
	call_log: list[int] = []

	async def fake_ask(self, *, request_id, prompt, cdp_port, mode, timeout_s):
		call_log.append(cdp_port)
		if cdp_port == 9222:
			raise server_module.AutomationError(
				'GEMINI_RATE_LIMIT',
				'quota exceeded',
				status_code=429,
			)
		return server_module.ChatResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			mode_requested=mode,
			mode_applied=None,
			used_port=cdp_port,
			answer=f'answer:{prompt}',
			results=None,
			error_code=None,
			error_message=None,
			details=None,
			elapsed_ms=10,
		)

	monkeypatch.setattr(server_module.GeminiBridgeService, 'ask', fake_ask)

	async with AsyncClient(transport=ASGITransport(app=server_module.app), base_url='http://test') as client:
		response = await client.post(
			'/v1/chat/gemini',
			json={
				'prompt': ['p1', 'p2'],
				'timeout_s': 60,
			},
		)

	assert response.status_code == 200
	payload = response.json()
	assert payload['success'] is True
	assert len(payload['results']) == 2
	assert all(item['success'] is True for item in payload['results'])
	assert 9222 in call_log
	assert 9223 in call_log


@pytest.mark.asyncio
async def test_chat_batch_distributes_across_ports(monkeypatch, server_module):
	# Reset scheduler state to avoid cross-test cooldown/reservation side effects.
	server_module.GEMINI_SCHEDULER._cooldown_until.clear()
	server_module.GEMINI_SCHEDULER._reserved_ports.clear()
	server_module.GEMINI_SCHEDULER._wait_queue.clear()
	call_log: list[int] = []

	async def fake_ask(self, *, request_id, prompt, cdp_port, mode, timeout_s):
		call_log.append(cdp_port)
		await asyncio.sleep(0.05)
		return server_module.ChatResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			mode_requested=mode,
			mode_applied=None,
			used_port=cdp_port,
			answer=f'answer:{prompt}',
			results=None,
			error_code=None,
			error_message=None,
			details=None,
			elapsed_ms=10,
		)

	monkeypatch.setattr(server_module.GeminiBridgeService, 'ask', fake_ask)

	async with AsyncClient(transport=ASGITransport(app=server_module.app), base_url='http://test') as client:
		response = await client.post(
			'/v1/chat/gemini',
			json={
				'prompt': ['p1', 'p2', 'p3'],
				'timeout_s': 60,
			},
		)

	assert response.status_code == 200
	payload = response.json()
	assert payload['success'] is True
	assert len(payload['results']) == 3
	used_ports = {item['port'] for item in payload['results'] if item['success']}
	assert len(used_ports) >= 2


@pytest.mark.asyncio
async def test_image_binary_response(monkeypatch, server_module):
	png_bytes = b'\x89PNG\r\n\x1a\nmock'
	image_path = server_module._ensure_image_storage_dir() / 'test_binary_response.png'
	image_path.write_bytes(png_bytes)

	async def fake_create_image(self, *, request_id, prompt, cdp_port, timeout_s, max_images):
		image = server_module.GeneratedImage(
			file_name=image_path.name,
			content_type='image/png',
			byte_size=len(png_bytes),
			local_path=str(image_path),
			download_url=f'/v1/image/download/{image_path.name}',
			source_url='https://example.test/image.png',
			width=100,
			height=100,
		)
		return server_module.ImageResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			used_port=cdp_port,
			images=[image],
			results=None,
			error_code=None,
			error_message=None,
			details=None,
			elapsed_ms=10,
		)

	monkeypatch.setattr(server_module.GeminiBridgeService, 'create_image', fake_create_image)

	try:
		async with AsyncClient(transport=ASGITransport(app=server_module.app), base_url='http://test') as client:
			response = await client.post(
				'/v1/image/gemini',
				json={
					'prompt': ['cat'],
					'response_format': 'binary',
				},
			)

		assert response.status_code == 200
		assert response.headers['content-type'].startswith('image/png')
		assert response.content == png_bytes
	finally:
		if image_path.exists():
			image_path.unlink()


@pytest.mark.asyncio
async def test_image_json_response_uses_download_url(monkeypatch, server_module):
	png_bytes = b'\x89PNG\r\n\x1a\njson'
	image_path = server_module._ensure_image_storage_dir() / 'test_json_response.png'
	image_path.write_bytes(png_bytes)

	async def fake_create_image(self, *, request_id, prompt, cdp_port, timeout_s, max_images):
		image = server_module.GeneratedImage(
			file_name=image_path.name,
			content_type='image/png',
			byte_size=len(png_bytes),
			local_path=str(image_path),
			download_url=f'/v1/image/download/{image_path.name}',
			source_url='https://example.test/json-image.png',
			width=100,
			height=100,
		)
		return server_module.ImageResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			used_port=cdp_port,
			images=[image],
			results=None,
			error_code=None,
			error_message=None,
			details=None,
			elapsed_ms=10,
		)

	monkeypatch.setattr(server_module.GeminiBridgeService, 'create_image', fake_create_image)

	try:
		async with AsyncClient(transport=ASGITransport(app=server_module.app), base_url='http://test') as client:
			response = await client.post(
				'/v1/image/gemini',
				json={
					'prompt': ['cat'],
					'response_format': 'json',
				},
			)

		assert response.status_code == 200
		payload = response.json()
		image_payload = payload['images'][0]
		assert image_payload['download_url'] == f'/v1/image/download/{image_path.name}'
		assert image_payload['local_path'] == str(image_path)
		assert 'base64_data' not in image_payload
	finally:
		if image_path.exists():
			image_path.unlink()


@pytest.mark.asyncio
async def test_image_download_and_clear_endpoints(server_module):
	storage_dir = server_module._ensure_image_storage_dir()
	target = storage_dir / 'gemini_download_clear_test.png'
	target.write_bytes(b'png-data')

	async with AsyncClient(transport=ASGITransport(app=server_module.app), base_url='http://test') as client:
		download_response = await client.get(f'/v1/image/download/{target.name}')
		assert download_response.status_code == 200
		assert download_response.content == b'png-data'

		clear_response = await client.post('/v1/image/clear', json={'provider': 'gemini'})
		assert clear_response.status_code == 200
		payload = clear_response.json()
		assert payload['success'] is True
		assert payload['cleared_files'] >= 1

	assert not target.exists()


@pytest.mark.asyncio
async def test_wait_for_answer_returns_when_stream_flag_stale(monkeypatch, server_module):
	service = server_module.GeminiBridgeService(
		server_module.ServiceConfig.from_env(
			provider='gpt',
			display_name='ChatGPT',
			default_hosts='chatgpt.com,chat.openai.com',
			default_open_url='https://chatgpt.com/',
			supports_mode=False,
		)
	)

	service.cfg.poll_interval_s = 0.01
	service.cfg.stable_polls = 1
	monkeypatch.setattr(server_module, 'CHAT_STALE_CONTENT_RETURN_S', 0.03)
	monkeypatch.setattr(server_module, 'CHAT_STALE_MIN_WORDS', 1)
	monkeypatch.setattr(server_module, 'CHAT_STALE_MIN_CHARS', 4)

	async def fake_snapshot(_session):
		return {
			'errorTexts': [],
			'responseCount': 1,
			'lastResponseText': 'final answer',
			'responseTextsTail': ['final answer'],
			'isStreaming': True,
			'sendButtonFound': True,
			'sendDisabled': False,
		}

	monkeypatch.setattr(service, '_snapshot', fake_snapshot)

	answer = await service._wait_for_answer(
		session=object(),
		baseline_count=0,
		baseline_last='',
		timeout_s=1.0,
	)

	assert answer == 'final answer'
