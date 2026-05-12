#!/usr/bin/env python3
"""FastAPI Gemini bridge built on browser-use BrowserSession + CDP.

Design goals:
- Connect to an already-running Chrome instance via CDP.
- Reuse an already-open Gemini tab (user handles login manually).
- Receive one chat prompt per request, wait for stream completion, return final answer.
- Production-oriented behavior: request lock, clear error taxonomy, retries via reconnect,
  and explicit handling for quota/rate-limit style failures.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections import deque
import json
import logging
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from browser_use import BrowserProfile, BrowserSession
from browser_use.browser.events import SwitchTabEvent

ChatProvider = Literal['gemini', 'gpt']
ChatMode = Literal['fast', 'reasoning', 'pro']


IMAGE_STORAGE_DIR = Path(
	os.getenv('CHAT_BRIDGE_IMAGE_DIR', os.path.join(os.getcwd(), 'generated-images'))
).resolve()


def _to_bool(value: str | None, default: bool = False) -> bool:
	if value is None:
		return default
	return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _auto_launch_chrome_enabled() -> bool:
	raw = os.getenv('CHAT_BRIDGE_AUTO_LAUNCH_CHROME', os.getenv('AUTO_LAUNCH_CHROME'))
	return _to_bool(raw, default=True)


def _to_float(value: str | None, default: float) -> float:
	if value is None:
		return default
	try:
		return float(value)
	except Exception:
		return default


def _to_int(value: str | None, default: int) -> int:
	if value is None:
		return default
	try:
		return int(value)
	except Exception:
		return default


def _parse_port_from_cdp_url(cdp_url: str, default: int = 9222) -> int:
	parsed = urlparse(cdp_url)
	if parsed.port is None:
		return default
	if parsed.port < 1 or parsed.port > 65535:
		return default
	return parsed.port


def _build_cdp_url_for_port(base_cdp_url: str, port: int) -> str:
	parsed = urlparse(base_cdp_url)
	scheme = parsed.scheme or 'http'
	hostname = parsed.hostname or '127.0.0.1'
	path = parsed.path or ''
	netloc = f'{hostname}:{port}'
	return urlunparse((scheme, netloc, path, '', '', ''))


def _parse_discovery_ports(raw: str | None) -> tuple[int, ...]:
	if not raw:
		return ()
	ports: list[int] = []
	for part in raw.split(','):
		candidate = _to_int(part.strip() or None, -1)
		if 1 <= candidate <= 65535:
			ports.append(candidate)
	return tuple(sorted(set(ports)))


def _ensure_image_storage_dir() -> Path:
	IMAGE_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
	return IMAGE_STORAGE_DIR


def _safe_image_file_name(file_name: str) -> str:
	safe_name = os.path.basename(str(file_name or '').strip())
	if not safe_name or safe_name in {'.', '..'} or safe_name != file_name:
		raise AutomationError(
			'IMAGE_FILE_INVALID',
			'Invalid image file name.',
			status_code=400,
			details={'file_name': file_name},
		)
	return safe_name


def _resolve_image_file_path(file_name: str) -> Path:
	safe_name = _safe_image_file_name(file_name)
	return _ensure_image_storage_dir() / safe_name


def _build_download_url(file_name: str) -> str:
	return f'/v1/image/download/{file_name}'


def _probe_cdp_sync(cdp_url: str) -> tuple[bool, dict[str, Any]]:
	version_url = f"{cdp_url.rstrip('/')}/json/version"
	request = Request(version_url, headers={'User-Agent': 'Mozilla/5.0'})
	try:
		with urlopen(request, timeout=2.0) as response:
			payload = json.loads(response.read().decode('utf-8'))
			if isinstance(payload, dict):
				return True, payload
			return True, {}
	except Exception:
		return False, {}


def _is_local_cdp_host(host: str | None) -> bool:
	if host is None:
		return True
	normalized = host.strip().lower()
	return normalized in {'127.0.0.1', 'localhost', '::1'}


def _find_chrome_executable() -> str | None:
	env_candidates = [
		os.getenv('CHAT_BRIDGE_CHROME_BIN'),
		os.getenv('GEMINI_CHROME_BIN'),
		os.getenv('CHROME_BIN'),
	]
	for candidate in env_candidates:
		if candidate and os.path.isfile(candidate):
			return candidate

	if os.name == 'nt':
		windows_candidates = [
			r'C:\Program Files\Google\Chrome\Application\chrome.exe',
			r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
			r'C:\Program Files\Chromium\Application\chrome.exe',
			r'C:\Program Files (x86)\Chromium\Application\chrome.exe',
			r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
			r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
		]
		for candidate in windows_candidates:
			if os.path.isfile(candidate):
				return candidate

	for binary_name in ('google-chrome', 'chrome', 'chromium', 'chromium-browser', 'msedge'):
		resolved = shutil.which(binary_name)
		if resolved:
			return resolved

	return None


def _chrome_profile_dir_for_port(port: int) -> str:
	base_dir = (
		os.getenv('CHAT_BRIDGE_CHROME_PROFILE_DIR')
		or os.getenv('GEMINI_CHROME_PROFILE_DIR')
		or os.path.join(tempfile.gettempdir(), 'browser-use-chat-bridge')
	)
	return os.path.join(base_dir, f'cdp-{port}')


def _launch_local_chrome_cdp_sync(cdp_url: str, port: int) -> dict[str, Any]:
	parsed = urlparse(cdp_url)
	host = parsed.hostname or '127.0.0.1'
	if not _is_local_cdp_host(host):
		return {'attempted': False, 'reason': 'non_local_cdp_host', 'host': host}

	chrome_bin = _find_chrome_executable()
	if not chrome_bin:
		return {'attempted': False, 'reason': 'chrome_binary_not_found'}

	profile_dir = _chrome_profile_dir_for_port(port)
	os.makedirs(profile_dir, exist_ok=True)

	launch_args = [
		chrome_bin,
		f'--remote-debugging-port={port}',
		f'--user-data-dir={profile_dir}',
		'--no-first-run',
		'--no-default-browser-check',
		'--disable-renderer-backgrounding',
		'--disable-background-timer-throttling',
		'--disable-backgrounding-occluded-windows',
		'--disable-features=CalculateNativeWinOcclusion',
	]

	if os.name == 'nt':
		launch_args.append('--new-window')

	try:
		process = subprocess.Popen(launch_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
	except Exception as e:
		return {
			'attempted': True,
			'reason': 'chrome_launch_failed',
			'error': str(e),
			'chrome_bin': chrome_bin,
			'profile_dir': profile_dir,
		}

	for _ in range(24):
		is_ready, _ = _probe_cdp_sync(cdp_url)
		if is_ready:
			return {
				'attempted': True,
				'launched': True,
				'pid': process.pid,
				'chrome_bin': chrome_bin,
				'profile_dir': profile_dir,
			}
		if process.poll() is not None:
			return {
				'attempted': True,
				'reason': 'chrome_exited_early',
				'returncode': process.returncode,
				'chrome_bin': chrome_bin,
				'profile_dir': profile_dir,
			}
		time.sleep(0.25)

	return {
		'attempted': True,
		'reason': 'cdp_not_ready_after_launch',
		'chrome_bin': chrome_bin,
		'profile_dir': profile_dir,
	}


def _activate_chrome_window_sync() -> dict[str, Any]:
	if os.name != 'nt':
		return {'attempted': False, 'reason': 'unsupported_os'}

	powershell_candidates = [
		shutil.which('powershell'),
		shutil.which('pwsh'),
	]
	powershell = next((candidate for candidate in powershell_candidates if candidate), None)
	if not powershell:
		return {'attempted': False, 'reason': 'powershell_not_found'}

	script = (
		"$ws=New-Object -ComObject WScript.Shell;"
		"$ok=$ws.AppActivate('Google Chrome');"
		"if(-not $ok){$ok=$ws.AppActivate('Chrome');};"
		"if($ok){'ok'}else{'not-found'}"
	)
	try:
		result = subprocess.run(
			[powershell, '-NoProfile', '-NonInteractive', '-Command', script],
			capture_output=True,
			text=True,
			timeout=3,
		)
	except Exception as e:
		return {'attempted': True, 'reason': 'activation_failed', 'error': str(e)}

	output = (result.stdout or '').strip().lower()
	if result.returncode == 0 and output == 'ok':
		return {'attempted': True, 'activated': True}
	return {
		'attempted': True,
		'activated': False,
		'returncode': result.returncode,
		'stdout': (result.stdout or '').strip(),
		'stderr': (result.stderr or '').strip(),
	}


@dataclass
class ServiceConfig:
	provider: ChatProvider
	display_name: str
	cdp_url: str
	default_open_url: str
	tab_hosts: tuple[str, ...]
	default_timeout_s: float
	poll_interval_s: float
	stable_polls: int
	cdp_connect_retries: int
	cdp_connect_retry_delay_s: float
	mode_required: bool
	supports_mode: bool
	max_prompt_len: int

	@classmethod
	def from_env(
		cls,
		*,
		provider: ChatProvider,
		display_name: str,
		default_hosts: str,
		default_open_url: str,
		supports_mode: bool,
	) -> 'ServiceConfig':
		prefix = provider.upper()
		hosts_raw = os.getenv(f'{prefix}_TAB_HOSTS', default_hosts)
		hosts = tuple(x.strip().lower() for x in hosts_raw.split(',') if x.strip())
		if not hosts:
			hosts = tuple(x.strip().lower() for x in default_hosts.split(',') if x.strip())

		return cls(
			provider=provider,
			display_name=display_name,
			cdp_url=os.getenv('CHAT_BRIDGE_CDP_URL', os.getenv('GEMINI_CDP_URL', 'http://127.0.0.1:9222')).strip(),
			default_open_url=os.getenv(f'{prefix}_OPEN_URL', default_open_url).strip(),
			tab_hosts=hosts,
			default_timeout_s=_to_float(os.getenv(f'{prefix}_DEFAULT_TIMEOUT_S', os.getenv('GEMINI_DEFAULT_TIMEOUT_S')), 600.0),
			poll_interval_s=_to_float(os.getenv(f'{prefix}_POLL_INTERVAL_S', os.getenv('GEMINI_POLL_INTERVAL_S')), 1.0),
			stable_polls=max(2, _to_int(os.getenv(f'{prefix}_STABLE_POLLS', os.getenv('GEMINI_STABLE_POLLS')), 3)),
			cdp_connect_retries=max(1, _to_int(os.getenv(f'{prefix}_CDP_CONNECT_RETRIES', os.getenv('GEMINI_CDP_CONNECT_RETRIES')), 3)),
			cdp_connect_retry_delay_s=max(0.25, _to_float(os.getenv(f'{prefix}_CDP_CONNECT_RETRY_DELAY_S', os.getenv('GEMINI_CDP_CONNECT_RETRY_DELAY_S')), 1.0)),
			mode_required=_to_bool(os.getenv(f'{prefix}_MODE_REQUIRED'), default=False),
			supports_mode=supports_mode,
			max_prompt_len=max(256, _to_int(os.getenv(f'{prefix}_MAX_PROMPT_LEN', os.getenv('GEMINI_MAX_PROMPT_LEN')), 16000)),
		)


class AutomationError(Exception):
	def __init__(self, code: str, message: str, *, status_code: int, details: dict[str, Any] | None = None):
		super().__init__(message)
		self.code = code
		self.message = message
		self.status_code = status_code
		self.details = details or {}


class ChatRequest(BaseModel):
	prompt: list[str] = Field(...)
	mode: ChatMode | None = None
	timeout_s: float = Field(default=600.0, ge=10.0, le=1200.0)


class ChatItemResult(BaseModel):
	prompt: str
	success: bool
	port: int | None = None
	answer: str | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


class ChatResponse(BaseModel):
	success: bool
	request_id: str
	provider: ChatProvider
	mode_requested: ChatMode | None = None
	mode_applied: bool | None = None
	used_port: int | None = None
	answer: str | None = None
	results: list[ChatItemResult] | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


class ImageRequest(BaseModel):
	prompt: list[str] = Field(...)
	timeout_s: float = Field(default=600.0, ge=20.0, le=1200.0)
	max_images: int = Field(default=4, ge=1, le=4)
	response_format: Literal['json', 'binary'] = 'json'


class GeneratedImage(BaseModel):
	file_name: str
	content_type: str
	byte_size: int
	local_path: str | None = None
	download_url: str | None = None
	source_url: str | None = None
	width: int | None = None
	height: int | None = None


class ImageItemResult(BaseModel):
	prompt: str
	success: bool
	port: int | None = None
	images: list[GeneratedImage] | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


class ImageResponse(BaseModel):
	success: bool
	request_id: str
	provider: ChatProvider
	used_port: int | None = None
	images: list[GeneratedImage] | None = None
	results: list[ImageItemResult] | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


class ClearImagesRequest(BaseModel):
	provider: ChatProvider | None = None


class ClearImagesResponse(BaseModel):
	success: bool
	provider: ChatProvider | None = None
	cleared_files: int
	remaining_files: int
	folder: str


PortNumber = Annotated[int, Field(ge=1, le=65535)]


class OpenWebRequest(BaseModel):
	ports: list[PortNumber] | None = None
	force_reconnect: bool = False


class OpenWebResult(BaseModel):
	success: bool
	port: int
	cdp_url: str | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None


class OpenWebResponse(BaseModel):
	success: bool
	port: int | None = None
	cdp_url: str | None = None
	results: list[OpenWebResult] | None = None
	active_ports: list[int] | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


class PingPortsRequest(BaseModel):
	ports: list[PortNumber] | None = None


class PortStatus(BaseModel):
	port: int
	active: bool
	cdp_url: str
	managed_by: list[ChatProvider] = Field(default_factory=list)
	browser: str | None = None
	web_socket_debugger_url: str | None = None


class PingPortsResponse(BaseModel):
	success: bool
	ports: list[PortStatus]


class ClosePortRequest(BaseModel):
	port: int = Field(ge=1, le=65535)
	provider: ChatProvider | None = None
	shutdown_browser: bool = False


class ClosePortResponse(BaseModel):
	success: bool
	port: int
	closed_by: list[ChatProvider]
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None


MODE_TARGETS: dict[str, tuple[str, ...]] = {
	'fast': ('flash', '2.5 flash', 'flash-lite', 'flash lite', '2.0 flash'),
	'reasoning': ('reasoning', 'thinking', 'deep think', 'reason'),
	'pro': ('pro', '2.5 pro', 'gemini pro'),
}

RATE_LIMIT_KEYWORDS = (
	'rate limit',
	'too many requests',
	'quota',
	'limit reached',
	'usage limit',
	'usage cap',
	'capacity',
	'try again later',
	'temporarily unavailable',
	'you have reached',
	'please wait',
	'image limit',
	'image generation limit',
	'image creation limit',
	'free plan limit',
	'upgrade to plus',
	'daily limit',
	'hourly limit',
	'request limit',
	'exceeded your current quota',
	'resource has been exhausted',
)

ERROR_KEYWORDS = (
	'something went wrong',
	'error',
	'failed',
	'unavailable',
	'network',
)


IMAGE_TOOL_KEYWORDS = ('create image', 'generate image', 'image')
STREAM_SETTLE_GRACE_SECONDS = 12.0
# Chat-only fallback: if answer text has not changed for N seconds, return it even if UI still shows loading.
CHAT_STALE_CONTENT_RETURN_S = max(1.0, _to_float(os.getenv('CHAT_BRIDGE_CHAT_STALE_CONTENT_RETURN_S'), 2.0))
CHAT_STALE_MIN_WORDS = max(1, _to_int(os.getenv('CHAT_BRIDGE_CHAT_STALE_MIN_WORDS'), 2))
CHAT_STALE_MIN_CHARS = max(4, _to_int(os.getenv('CHAT_BRIDGE_CHAT_STALE_MIN_CHARS'), 10))
FORCE_RESTORE_WINDOW = _to_bool(os.getenv('CHAT_BRIDGE_FORCE_RESTORE_WINDOW'), default=True)
FORCE_PAGE_ACTIVE_STATE = _to_bool(os.getenv('CHAT_BRIDGE_FORCE_PAGE_ACTIVE_STATE'), default=True)
FORCE_FOREGROUND_WINDOW = _to_bool(os.getenv('CHAT_BRIDGE_FORCE_FOREGROUND_WINDOW'), default=True)
FOREGROUND_RETRY_INTERVAL_S = max(0.5, _to_float(os.getenv('CHAT_BRIDGE_FOREGROUND_RETRY_INTERVAL_S'), 2.0))


SNAPSHOT_JS = r"""
(function() {
	const visible = (el) => {
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	};

	const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
	const norm = (s) => (s || '').toLowerCase();

	const composerSelectors = [
		'#prompt-textarea',
		'textarea[data-testid*="prompt" i]',
		'textarea[aria-label*="message" i]',
		'textarea[placeholder*="message" i]',
		'textarea[placeholder*="ask" i]',
		'textarea',
		'div[contenteditable="true"][role="textbox"]',
		'div[contenteditable="true"][aria-label*="message" i]',
		'div[contenteditable="true"][aria-label*="ask" i]',
		'div[contenteditable="true"][data-testid*="input" i]',
		'div[contenteditable="true"]'
	];

	let composer = null;
	for (const sel of composerSelectors) {
		const list = Array.from(document.querySelectorAll(sel)).filter(visible);
		if (list.length > 0) {
			composer = list[list.length - 1];
			break;
		}
	}

	const sendButtonSelectors = [
		'button[data-testid*="send" i]',
		'button[aria-label="Send prompt" i]',
		'button[aria-label*="send" i]',
		'button[type="submit"]'
	];

	let sendButton = null;
	for (const sel of sendButtonSelectors) {
		const list = Array.from(document.querySelectorAll(sel)).filter(visible);
		if (list.length > 0) {
			sendButton = list[list.length - 1];
			break;
		}
	}

	const allButtons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
	const stopButtonSelectors = [
		'button[data-testid*="stop" i]',
		'button[aria-label*="stop" i]'
	];
	let stopButton = null;
	for (const sel of stopButtonSelectors) {
		const list = Array.from(document.querySelectorAll(sel)).filter(visible);
		if (list.length > 0) {
			stopButton = list[list.length - 1];
			break;
		}
	}
	if (!stopButton) {
		stopButton = allButtons.find((btn) => {
			const t = norm(textOf(btn) + ' ' + (btn.getAttribute('aria-label') || ''));
			return t.includes('stop generating') || t.includes('stop response') || t === 'stop';
		});
	}

	const responseSelectors = [
		'[data-message-author-role="assistant"]',
		'[data-message-author-role="assistant"] .markdown',
		'[data-message-author-role="assistant"] [class*="markdown" i]',
		'article[data-testid*="conversation-turn" i]',
		'[data-testid*="assistant" i]',
		'[data-testid*="conversation-turn" i] [class*="markdown" i]',
		'[data-testid*="response" i]',
		'[data-test-id*="response" i]',
		'main structured-content-container',
		'main .model-response-text',
		'message-content',
		'main article',
		'main [role="article"]',
		'main div.markdown',
		'main div[data-node-type*="model" i]'
	];

	const responseTexts = [];
	const seen = new Set();
	for (const sel of responseSelectors) {
		for (const el of document.querySelectorAll(sel)) {
			if (!visible(el)) continue;
			const t = textOf(el);
			if (!t || t.length < 2) continue;
			if (seen.has(t)) continue;
			seen.add(t);
			responseTexts.push(t);
		}
	}

	const errorSelectors = [
		'[role="alert"]',
		'[aria-live="assertive"]',
		'[data-testid*="error" i]',
		'[class*="error" i]',
		'[class*="toast" i]'
	];

	const errorTexts = [];
	for (const sel of errorSelectors) {
		for (const el of document.querySelectorAll(sel)) {
			if (!visible(el)) continue;
			const t = textOf(el);
			if (!t || t.length < 3) continue;
			errorTexts.push(t);
		}
	}

	let activeModeText = '';
	const modeHints = ['pro', 'flash', 'reasoning', 'thinking'];
	for (const btn of allButtons) {
		const t = textOf(btn);
		if (!t || t.length > 80) continue;
		const n = norm(t);
		if (modeHints.some((m) => n.includes(m))) {
			activeModeText = t;
			break;
		}
	}

	const lastResponseText = responseTexts.length > 0 ? responseTexts[responseTexts.length - 1] : '';
	const responseTextsTail = responseTexts.slice(-4);

	return {
		url: location.href,
		title: document.title || '',
		composerFound: !!composer,
		composerText: composer ? textOf(composer).slice(0, 2000) : '',
		sendButtonFound: !!sendButton,
		sendDisabled: !!(sendButton && (sendButton.disabled || sendButton.getAttribute('aria-disabled') === 'true')),
		isStreaming: !!stopButton,
		activeModeText,
		responseCount: responseTexts.length,
		lastResponseText: lastResponseText.slice(0, 30000),
		responseTextsTail: responseTextsTail.map((t) => t.slice(0, 30000)),
		errorTexts: errorTexts.slice(0, 8)
	};
})();
"""


def build_send_prompt_js(prompt: str) -> str:
	payload = json.dumps(prompt)
	return f"""
(function() {{
	const PROMPT = {payload};
	const visible = (el) => {{
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	}};
	const textValue = (el) => ('value' in el ? (el.value || '') : (el.textContent || '')).trim();

	const composerSelectors = [
		'#prompt-textarea',
		'textarea[data-testid*="prompt" i]',
		'textarea[aria-label*="message" i]',
		'textarea[placeholder*="message" i]',
		'textarea[placeholder*="ask" i]',
		'textarea',
		'div[contenteditable="true"][role="textbox"]',
		'div[contenteditable="true"][aria-label*="message" i]',
		'div[contenteditable="true"][aria-label*="ask" i]',
		'div[contenteditable="true"][data-testid*="input" i]',
		'div[contenteditable="true"]'
	];

	let composer = null;
	for (const sel of composerSelectors) {{
		const list = Array.from(document.querySelectorAll(sel)).filter(visible);
		if (list.length > 0) {{
			composer = list[list.length - 1];
			break;
		}}
	}}
	if (!composer) {{
		return {{ ok: false, error: 'composer-not-found' }};
	}}

	composer.focus();
	if ('value' in composer) {{
		composer.value = '';
		composer.dispatchEvent(new Event('input', {{ bubbles: true }}));
		composer.value = PROMPT;
		composer.dispatchEvent(new Event('input', {{ bubbles: true }}));
		composer.dispatchEvent(new Event('change', {{ bubbles: true }}));
	}} else if (composer.getAttribute('contenteditable') === 'true') {{
		composer.textContent = '';
		composer.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'deleteContentBackward' }}));
		document.execCommand('insertText', false, PROMPT);
		if (!composer.textContent || composer.textContent.trim() !== PROMPT.trim()) {{
			composer.textContent = PROMPT;
		}}
		composer.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: PROMPT }}));
	}} else {{
		return {{ ok: false, error: 'composer-unsupported' }};
	}}

	composer.dispatchEvent(new Event('change', {{ bubbles: true }}));

	return {{ ok: true, method: 'set-prompt', composerText: textValue(composer) }};
}})();
"""


def build_mode_switch_js(targets: tuple[str, ...]) -> str:
	targets_payload = json.dumps(list(targets))
	return f"""
(function() {{
	const TARGETS = {targets_payload}.map((x) => (x || '').toLowerCase());
	const visible = (el) => {{
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	}};
	const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\\s+/g, ' ').trim();
	const norm = (s) => (s || '').toLowerCase();
	const hasTarget = (s) => TARGETS.some((t) => norm(s).includes(t));

	const allButtons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
	let picker = allButtons.find((btn) => {{
		const t = norm(textOf(btn) + ' ' + (btn.getAttribute('aria-label') || ''));
		return t.includes('model') || t.includes('gemini') || t.includes('flash') || t.includes('pro') || t.includes('reasoning');
	}});

	if (!picker) {{
		return {{ ok: false, error: 'mode-picker-not-found' }};
	}}

	if (hasTarget(textOf(picker) + ' ' + (picker.getAttribute('aria-label') || ''))) {{
		return {{ ok: true, already: true, selected: textOf(picker) }};
	}}

	picker.click();

	const roots = Array.from(document.querySelectorAll('[role="listbox"], [role="menu"], [aria-modal="true"], body'));
	let selected = null;

	for (const root of roots) {{
		const options = Array.from(root.querySelectorAll('[role="option"], [role="menuitemradio"], [role="menuitem"], button, [role="button"]'));
		for (const option of options) {{
			if (!visible(option)) continue;
			const txt = textOf(option);
			if (!txt) continue;
			if (hasTarget(txt)) {{
				selected = option;
				break;
			}}
		}}
		if (selected) break;
	}}

	if (!selected) {{
		return {{ ok: false, error: 'mode-option-not-found' }};
	}}

	const selectedText = textOf(selected);
	selected.click();
	return {{ ok: true, already: false, selected: selectedText }};
}})();
"""


CLICK_SEND_BUTTON_JS = r"""
(function() {
	const visible = (el) => {
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	};

	const sendButton = Array.from(document.querySelectorAll('button[data-testid*="send" i], button[aria-label="Send prompt" i], button[aria-label*="send" i], button[type="submit"]'))
		.filter(visible)
		.find((btn) => !(btn.disabled || btn.getAttribute('aria-disabled') === 'true'));
	if (sendButton) {
		sendButton.focus();
		sendButton.click();
		return { ok: true, method: 'button' };
	}

	const composer = Array.from(document.querySelectorAll('textarea, div[contenteditable="true"], input'))
		.filter(visible)
		.pop();
	if (!composer) {
		return { ok: false, error: 'composer-not-found' };
	}

	composer.focus();
	composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
	composer.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
	composer.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
	return { ok: true, method: 'enter' };
})();
"""


CLICK_NEW_CHAT_JS = r"""
(function() {
	const visible = (el) => {
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	};

	const controls = Array.from(document.querySelectorAll('a, button, [role="button"]')).filter(visible);
	const newChat = controls.find((el) => ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase().includes('new chat'));
	if (!newChat) {
		return { ok: false, error: 'new-chat-not-found' };
	}

	newChat.click();
	return { ok: true };
})();
"""


DISMISS_BLOCKING_UI_JS = r"""
(function() {
	const visible = (el) => {
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	};
	const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim().toLowerCase();

	const dialogs = Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], .modal, [class*="dialog" i]')).filter(visible);
	if (!dialogs.length) {
		return { ok: true, closed: false, reason: 'no-dialog' };
	}

	const closeKeywords = ['close', 'dismiss', 'cancel', 'done', 'back'];
	let closed = false;
	for (const dialog of dialogs) {
		const controls = Array.from(dialog.querySelectorAll('button, [role="button"], [aria-label], .close'));
		for (const control of controls) {
			if (!visible(control)) continue;
			const joined = `${textOf(control)} ${(control.getAttribute('aria-label') || '').toLowerCase()}`;
			if (!closeKeywords.some((keyword) => joined.includes(keyword))) continue;
			control.click();
			closed = true;
			break;
		}
	}

	if (!closed) {
		document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true }));
		document.dispatchEvent(new KeyboardEvent('keyup', { key: 'Escape', code: 'Escape', bubbles: true, cancelable: true }));
	}

	return { ok: true, closed: true, method: closed ? 'button' : 'escape' };
})();
"""


CLICK_CREATE_IMAGE_TOOL_JS = r"""
(function() {
	const visible = (el) => {
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	};
	const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim().toLowerCase();
	const controls = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter(visible);
	const button = controls.find((el) => {
		const joined = `${textOf(el)} ${(el.getAttribute('aria-label') || '').toLowerCase()}`;
		return joined.includes('create image') || joined.includes('generate image');
	});
	if (!button) {
		return { ok: false, error: 'create-image-tool-not-found' };
	}
	button.click();
	return { ok: true, label: (button.innerText || button.textContent || '').trim() };
})();
"""


IMAGE_SNAPSHOT_JS = r"""
(function() {
	const visible = (el) => {
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	};
	const textOf = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
	const norm = (s) => (s || '').toLowerCase();

	const allButtons = Array.from(document.querySelectorAll('button, [role="button"]')).filter(visible);
	const stopButtonSelectors = [
		'button[data-testid*="stop" i]',
		'button[aria-label*="stop" i]'
	];
	let stopButton = null;
	for (const sel of stopButtonSelectors) {
		const list = Array.from(document.querySelectorAll(sel)).filter(visible);
		if (list.length > 0) {
			stopButton = list[list.length - 1];
			break;
		}
	}
	if (!stopButton) {
		stopButton = allButtons.find((btn) => {
			const t = norm(textOf(btn) + ' ' + (btn.getAttribute('aria-label') || ''));
			return t.includes('stop generating') || t.includes('stop response') || t === 'stop';
		});
	}

	const imageCandidates = [];
	const seen = new Set();
	const pushCandidate = (candidate) => {
		const key = `${candidate.sourceUrl || ''}|${candidate.width || 0}|${candidate.height || 0}`;
		if (seen.has(key)) return;
		seen.add(key);
		imageCandidates.push(candidate);
	};

	Array.from(document.querySelectorAll('button.image-button')).forEach((button, buttonIndex) => {
		const img = button.querySelector('img');
		const src = img ? (img.currentSrc || img.src || '') : '';
		const alt = img ? (img.alt || '') : '';
		pushCandidate({
			kind: 'generated-image-button',
			buttonIndex,
			sourceUrl: src,
			width: img ? (img.naturalWidth || img.width || 0) : 0,
			height: img ? (img.naturalHeight || img.height || 0) : 0,
			alt,
		});
	});

	for (const img of document.querySelectorAll('img')) {
		if (!visible(img)) continue;
		const src = img.currentSrc || img.src || '';
		if (!src) continue;
		const width = img.naturalWidth || img.width || 0;
		const height = img.naturalHeight || img.height || 0;
		if (width < 128 || height < 128) continue;
		pushCandidate({
			kind: 'img',
			sourceUrl: src,
			width,
			height,
			alt: img.alt || '',
		});
	}

	for (const anchor of document.querySelectorAll('a[href]')) {
		if (!visible(anchor)) continue;
		const href = anchor.href || '';
		if (!href) continue;
		const joined = norm(textOf(anchor) + ' ' + (anchor.getAttribute('aria-label') || '') + ' ' + href);
		if (!joined.match(/download|png|jpg|jpeg|webp|image/)) continue;
		pushCandidate({ kind: 'link', sourceUrl: href, width: null, height: null, alt: textOf(anchor) });
	}

	const errorTexts = [];
	for (const sel of ['[role="alert"]', '[aria-live="assertive"]', '[data-testid*="error" i]', '[class*="error" i]', '[class*="toast" i]']) {
		for (const el of document.querySelectorAll(sel)) {
			if (!visible(el)) continue;
			const t = textOf(el);
			if (!t || t.length < 3) continue;
			errorTexts.push(t);
		}
	}

	return {
		url: location.href,
		isStreaming: !!stopButton,
		imageCount: imageCandidates.length,
		imageCandidates: imageCandidates.slice(0, 8),
		errorTexts: errorTexts.slice(0, 8),
	};
})();
"""



def build_extract_image_js(candidate: dict[str, Any]) -> str:
	payload = json.dumps(candidate)
	return f"""
(async function() {{
	const requested = {payload};
	const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
	const visible = (el) => {{
		if (!el) return false;
		const s = window.getComputedStyle(el);
		if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return false;
		const r = el.getBoundingClientRect();
		return r.width > 1 && r.height > 1;
	}};
	const candidates = [];
	const seen = new Set();
	const pushCandidate = (candidate) => {{
		const key = `${{candidate.sourceUrl || ''}}|${{candidate.width || 0}}|${{candidate.height || 0}}`;
		if (seen.has(key)) return;
		seen.add(key);
		candidates.push(candidate);
	}};

	Array.from(document.querySelectorAll('button.image-button')).forEach((button, buttonIndex) => {{
		const img = button.querySelector('img');
		const src = img ? (img.currentSrc || img.src || '') : '';
		const alt = img ? (img.alt || '') : '';
		pushCandidate({{ kind: 'generated-image-button', buttonIndex, sourceUrl: src, width: img ? (img.naturalWidth || img.width || 0) : 0, height: img ? (img.naturalHeight || img.height || 0) : 0, alt }});
	}});

	for (const img of document.querySelectorAll('img')) {{
		if (!visible(img)) continue;
		const src = img.currentSrc || img.src || '';
		if (!src) continue;
		const width = img.naturalWidth || img.width || 0;
		const height = img.naturalHeight || img.height || 0;
		if (width < 128 || height < 128) continue;
		pushCandidate({{ kind: 'img', sourceUrl: src, width, height, alt: img.alt || '' }});
	}}

	for (const anchor of document.querySelectorAll('a[href]')) {{
		if (!visible(anchor)) continue;
		const href = anchor.href || '';
		if (!href) continue;
		const joined = `${{(anchor.innerText || anchor.textContent || '').toLowerCase()}} ${{(anchor.getAttribute('aria-label') || '').toLowerCase()}} ${{href.toLowerCase()}}`;
		if (!joined.match(/download|png|jpg|jpeg|webp|image/)) continue;
		pushCandidate({{ kind: 'link', sourceUrl: href, width: null, height: null, alt: (anchor.innerText || anchor.textContent || '').trim() }});
	}}

	const candidate = candidates.find((item) => {{
		if (requested.kind === 'generated-image-button' && item.kind === 'generated-image-button') {{
			return item.buttonIndex === requested.buttonIndex;
		}}
		return item.sourceUrl === requested.sourceUrl && (item.width || 0) === (requested.width || 0) && (item.height || 0) === (requested.height || 0);
	}});
	if (!candidate) {{
		return {{ ok: false, error: 'image-candidate-not-found', candidateCount: candidates.length, requested }};
	}}

	const toDataUrl = (blob) => new Promise((resolve, reject) => {{
		const reader = new FileReader();
		reader.onload = () => resolve(reader.result);
		reader.onerror = () => reject(reader.error || new Error('file-reader-failed'));
		reader.readAsDataURL(blob);
	}});
	const dismissPreview = () => {{
		const closeKeywords = ['close', 'dismiss', 'cancel', 'done', 'back'];
		const controls = Array.from(document.querySelectorAll('button,[role="button"],[aria-label],.close')).filter(visible);
		for (const control of controls) {{
			const joined = `${{(control.innerText || control.textContent || '').toLowerCase()}} ${{(control.getAttribute('aria-label') || '').toLowerCase()}}`;
			if (!closeKeywords.some((keyword) => joined.includes(keyword))) continue;
			control.click();
			return 'button';
		}}
		document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', code: 'Escape', bubbles: true, cancelable: true }}));
		document.dispatchEvent(new KeyboardEvent('keyup', {{ key: 'Escape', code: 'Escape', bubbles: true, cancelable: true }}));
		return 'escape';
	}};

	const sourceUrl = candidate.sourceUrl || '';
	if (candidate.kind === 'generated-image-button') {{
		const buttons = Array.from(document.querySelectorAll('button.image-button'));
		const button = buttons[candidate.buttonIndex] || buttons[buttons.length - 1];
		if (!button) {{
			return {{ ok: false, error: 'image-button-not-found', buttonIndex: candidate.buttonIndex }};
		}}
		button.click();
		for (let attempt = 0; attempt < 20; attempt += 1) {{
			await sleep(250);
			const previewImages = Array.from(document.querySelectorAll('div.image-viewport img, .image-viewport img, img')).filter((img) => (img.currentSrc || img.src || '').startsWith('blob:') && img.complete && (img.naturalWidth || 0) >= 128 && (img.naturalHeight || 0) >= 128);
			const preview = previewImages[previewImages.length - 1];
			if (!preview) continue;
			try {{
				const canvas = document.createElement('canvas');
				canvas.width = preview.naturalWidth;
				canvas.height = preview.naturalHeight;
				const ctx = canvas.getContext('2d');
				ctx.drawImage(preview, 0, 0);
				const dataUrl = canvas.toDataURL('image/png');
				dismissPreview();
				return {{ ok: true, dataUrl, sourceUrl: preview.currentSrc || preview.src || sourceUrl, contentType: 'image/png', width: preview.naturalWidth, height: preview.naturalHeight, buttonIndex: candidate.buttonIndex }};
			}} catch (error) {{
				dismissPreview();
				return {{ ok: false, error: 'preview-canvas-failed', reason: String(error), sourceUrl }};
			}}
		}}
		dismissPreview();
		return {{ ok: false, error: 'preview-image-not-ready', sourceUrl }};
	}}

	if (sourceUrl.startsWith('data:')) {{
		return {{ ok: true, dataUrl: sourceUrl, sourceUrl, width: candidate.width, height: candidate.height }};
	}}

	try {{
		const response = await fetch(sourceUrl, {{ credentials: 'include' }});
		if (!response.ok) throw new Error(`fetch-failed:${{response.status}}`);
		const blob = await response.blob();
		const dataUrl = await toDataUrl(blob);
		return {{
			ok: true,
			dataUrl,
			sourceUrl,
			contentType: blob.type || '',
			width: candidate.width,
			height: candidate.height,
		}};
	}} catch (error) {{
		return {{
			ok: false,
			error: 'image-fetch-failed',
			reason: String(error),
			sourceUrl,
			width: candidate.width,
			height: candidate.height,
		}};
	}}
}})();
"""


class GeminiBridgeService:
	def __init__(self, cfg: ServiceConfig):
		self.cfg = cfg
		self.logger = logging.getLogger(f'{cfg.provider}_bridge')
		self._default_port = _parse_port_from_cdp_url(cfg.cdp_url)
		self._sessions: dict[int, BrowserSession] = {}
		self._startup_lock = asyncio.Lock()
		self._last_foreground_attempt_at = 0.0

	async def startup(self) -> None:
		async with self._startup_lock:
			# Startup should not fail if CDP is not ready yet.
			try:
				await self._ensure_session(force_reconnect=False, cdp_port=self._default_port)
			except AutomationError as e:
				self.logger.info('startup skipped: %s (%s)', e.code, e.message)

	async def shutdown(self) -> None:
		for port, session in list(self._sessions.items()):
			try:
				await session.stop()
			except Exception as e:
				self.logger.warning('failed to stop browser session on port %s: %s', port, e)
			finally:
				self._sessions.pop(port, None)

	def managed_ports(self) -> list[int]:
		ports: list[int] = []
		for port, session in self._sessions.items():
			if session.is_cdp_connected:
				ports.append(port)
		return sorted(set(ports))

	def has_managed_port(self, port: int) -> bool:
		session = self._sessions.get(port)
		return bool(session is not None and session.is_cdp_connected)

	def cdp_url_for_port(self, port: int) -> str:
		return _build_cdp_url_for_port(self.cfg.cdp_url, port)

	async def open_web(
		self,
		*,
		port: int,
		url: str | None,
		new_tab: bool,
		force_reconnect: bool,
	) -> dict[str, Any]:
		session = await self._ensure_session(force_reconnect=force_reconnect, cdp_port=port, allow_auto_launch=True)

		provider_tab_ready = False
		tab_url = None
		try:
			tab = await self._switch_to_gemini_tab(session)
			provider_tab_ready = True
			tab_url = tab.url
		except AutomationError:
			provider_tab_ready = False

		return {
			'provider': self.cfg.provider,
			'port': port,
			'cdp_url': self.cdp_url_for_port(port),
			'navigated_url': tab_url,
			'provider_tab_ready': provider_tab_ready,
			'active_ports': self.managed_ports(),
		}

	async def close_port(self, *, port: int, shutdown_browser: bool) -> bool:
		session = self._sessions.get(port)
		if session is None:
			return False

		if shutdown_browser:
			try:
				cdp_session = await session.get_or_create_cdp_session(focus=False)
				await cdp_session.cdp_client.send.Browser.close()
			except Exception:
				pass

		try:
			await session.stop()
		except Exception as e:
			self.logger.warning('failed to close session on port %s: %s', port, e)
			raise
		finally:
			self._sessions.pop(port, None)

		return True

	async def probe_port(self, port: int) -> PortStatus:
		cdp_url = self.cdp_url_for_port(port)
		active, payload = await asyncio.to_thread(_probe_cdp_sync, cdp_url)
		managed = self.has_managed_port(port)
		return PortStatus(
			port=port,
			active=bool(active or managed),
			cdp_url=cdp_url,
			managed_by=[self.cfg.provider] if managed else [],
			browser=str(payload.get('Browser') or '') or None,
			web_socket_debugger_url=str(payload.get('webSocketDebuggerUrl') or '') or None,
		)

	def _resolve_port(self, cdp_port: int | None) -> int:
		if cdp_port is None:
			return self._default_port
		if cdp_port < 1 or cdp_port > 65535:
			raise AutomationError(
				'INVALID_CDP_PORT',
				'CDP port must be between 1 and 65535.',
				status_code=422,
				details={'port': cdp_port},
			)
		return cdp_port

	def _find_port_for_session(self, session: BrowserSession) -> int:
		for port, candidate in self._sessions.items():
			if candidate is session:
				return port
		return self._default_port

	def _build_browser_session(self, cdp_url: str) -> BrowserSession:
		profile = BrowserProfile(
			cdp_url=cdp_url,
			is_local=False,
			keep_alive=True,
			highlight_elements=False,
		)
		return BrowserSession(browser_profile=profile)

	async def ask(
		self,
		*,
		request_id: str,
		prompt: str,
		cdp_port: int | None,
		mode: ChatMode | None,
		timeout_s: float | None,
	) -> ChatResponse:
		started = time.time()
		resolved_port = self._resolve_port(cdp_port)

		if len(prompt) > self.cfg.max_prompt_len:
			raise AutomationError(
				'PROMPT_TOO_LONG',
				f'Prompt exceeds max length ({self.cfg.max_prompt_len}).',
				status_code=422,
				details={'max_prompt_len': self.cfg.max_prompt_len},
			)

		effective_timeout = timeout_s if timeout_s is not None else self.cfg.default_timeout_s
		request_deadline = started + effective_timeout
		mode_applied: bool | None = None

		try:
			async with asyncio.timeout(effective_timeout):
				session = await self._ensure_session(force_reconnect=False, cdp_port=resolved_port)
				tab = await self._ensure_provider_tab(session, force_activate=True)
				await self._dismiss_blocking_ui(session)

				if mode is not None and self.cfg.supports_mode:
					mode_applied = await self._apply_mode(session, mode)
					if self.cfg.mode_required and not mode_applied:
						raise AutomationError(
							'MODE_SWITCH_FAILED',
							f'Unable to switch {self.cfg.display_name} mode to {mode}.',
							status_code=409,
							details={'mode': mode, 'tab_url': tab.url},
						)
				elif mode not in (None, 'fast'):
					raise AutomationError(
						'MODE_UNSUPPORTED',
						f'{self.cfg.display_name} mode {mode} is not implemented by this bridge yet.',
						status_code=422,
						details={'provider': self.cfg.provider, 'mode': mode},
					)
				elif mode is not None:
					mode_applied = False

				new_chat_result = await self._run_js(session, CLICK_NEW_CHAT_JS)
				if isinstance(new_chat_result, dict) and new_chat_result.get('ok'):
					await asyncio.sleep(0.8)

				baseline = await self._snapshot(session)
				if not baseline.get('composerFound'):
					raise AutomationError(
						'COMPOSER_NOT_FOUND',
						f'{self.cfg.display_name} input box is not available. Open the chat UI and ensure the page is fully loaded.',
						status_code=409,
						details={'tab_url': tab.url},
					)

				send_result = await self._run_js(session, build_send_prompt_js(prompt))
				if not isinstance(send_result, dict) or not send_result.get('ok'):
					raise AutomationError(
						'PROMPT_SEND_FAILED',
						f'Failed to write prompt into {self.cfg.display_name} composer.',
						status_code=502,
						details={'send_result': send_result},
					)

				typed_prompt = str(send_result.get('composerText') or '').strip()
				if typed_prompt != prompt.strip():
					post_type = await self._snapshot(session)
					typed_prompt = str(post_type.get('composerText') or '').strip()
					if typed_prompt != prompt.strip():
						raise AutomationError(
							'PROMPT_WRITE_NOT_CONFIRMED',
							f'Prompt did not persist in {self.cfg.display_name} composer after input attempt.',
							status_code=502,
							details={'send_result': send_result, 'post_type': post_type},
						)

				submit_result = await self._run_js(session, CLICK_SEND_BUTTON_JS)
				if not isinstance(submit_result, dict) or not submit_result.get('ok'):
					raise AutomationError(
						'PROMPT_SUBMIT_FAILED',
						f'Failed to trigger {self.cfg.display_name} prompt submission.',
						status_code=502,
						details={'submit_result': submit_result},
					)

				await asyncio.sleep(0.5)
				post_send = await self._snapshot(session)
				post_send_text = str(post_send.get('composerText') or '').strip()
				if (
					post_send_text == prompt.strip()
					and not bool(post_send.get('isStreaming'))
					and int(post_send.get('responseCount') or 0) <= int(baseline.get('responseCount') or 0)
				):
					retry_send = await self._run_js(session, CLICK_SEND_BUTTON_JS)
					if not isinstance(retry_send, dict) or not retry_send.get('ok'):
						raise AutomationError(
							'PROMPT_SUBMIT_NOT_CONFIRMED',
							f'Prompt remained in {self.cfg.display_name} composer after submission attempt.',
							status_code=502,
							details={'send_result': send_result, 'retry_send': retry_send, 'post_send': post_send},
						)

				answer = await self._wait_for_answer(
					session=session,
					baseline_count=int(baseline.get('responseCount') or 0),
					baseline_last=str(baseline.get('lastResponseText') or ''),
					timeout_s=self._remaining_timeout_or_raise(request_deadline=request_deadline, stage='waiting for final response'),
				)
		except TimeoutError as e:
			raise AutomationError(
				f'{self.cfg.provider.upper()}_REQUEST_TIMEOUT',
				f'{self.cfg.display_name} request exceeded timeout.',
				status_code=504,
				details={'timeout_s': effective_timeout},
			) from e

		elapsed_ms = int((time.time() - started) * 1000)
		return ChatResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			mode_requested=mode,
			mode_applied=mode_applied,
			used_port=resolved_port,
			answer=answer,
			elapsed_ms=elapsed_ms,
		)

	async def create_image(
		self,
		*,
		request_id: str,
		prompt: str,
		cdp_port: int | None,
		timeout_s: float | None,
		max_images: int,
	) -> ImageResponse:
		started = time.time()
		resolved_port = self._resolve_port(cdp_port)

		if len(prompt) > self.cfg.max_prompt_len:
			raise AutomationError(
				'PROMPT_TOO_LONG',
				f'Prompt exceeds max length ({self.cfg.max_prompt_len}).',
				status_code=422,
				details={'max_prompt_len': self.cfg.max_prompt_len},
			)

		effective_timeout = timeout_s if timeout_s is not None else max(self.cfg.default_timeout_s, 600.0)
		request_deadline = started + effective_timeout

		try:
			async with asyncio.timeout(effective_timeout):
				session = await self._ensure_session(force_reconnect=False, cdp_port=resolved_port)
				tab = await self._ensure_provider_tab(session, force_activate=True)
				await self._dismiss_blocking_ui(session)

				new_chat_result = await self._run_js(session, CLICK_NEW_CHAT_JS)
				if isinstance(new_chat_result, dict) and new_chat_result.get('ok'):
					await asyncio.sleep(0.8)

				tool_result = await self._run_js(session, CLICK_CREATE_IMAGE_TOOL_JS)
				if isinstance(tool_result, dict) and tool_result.get('ok'):
					await asyncio.sleep(0.5)

				baseline = await self._snapshot_images(session)
				text_baseline = await self._snapshot(session)
				if not text_baseline.get('composerFound'):
					raise AutomationError(
						'COMPOSER_NOT_FOUND',
						f'{self.cfg.display_name} input box is not available. Open the chat UI and ensure the page is fully loaded.',
						status_code=409,
						details={'tab_url': tab.url},
					)

				send_result = await self._run_js(session, build_send_prompt_js(prompt))
				if not isinstance(send_result, dict) or not send_result.get('ok'):
					raise AutomationError(
						'IMAGE_PROMPT_WRITE_FAILED',
						f'Failed to write image prompt into {self.cfg.display_name} composer.',
						status_code=502,
						details={'send_result': send_result},
					)

				typed_prompt = str(send_result.get('composerText') or '').strip()
				if typed_prompt != prompt.strip():
					post_type = await self._snapshot(session)
					typed_prompt = str(post_type.get('composerText') or '').strip()
					if typed_prompt != prompt.strip():
						raise AutomationError(
							'IMAGE_PROMPT_WRITE_NOT_CONFIRMED',
							'Image prompt did not persist in composer after input attempt.',
							status_code=502,
							details={'send_result': send_result, 'post_type': post_type},
						)

				submit_result = await self._run_js(session, CLICK_SEND_BUTTON_JS)
				if not isinstance(submit_result, dict) or not submit_result.get('ok'):
					raise AutomationError(
						'IMAGE_PROMPT_SUBMIT_FAILED',
						f'Failed to submit image prompt to {self.cfg.display_name}.',
						status_code=502,
						details={'submit_result': submit_result},
					)

				await asyncio.sleep(1.0)
				post_send = await self._snapshot(session)
				if (
					str(post_send.get('composerText') or '').strip() == prompt.strip()
					and not bool(post_send.get('isStreaming'))
				):
					retry_send = await self._run_js(session, CLICK_SEND_BUTTON_JS)
					if not isinstance(retry_send, dict) or not retry_send.get('ok'):
						raise AutomationError(
							'IMAGE_PROMPT_SUBMIT_NOT_CONFIRMED',
							'Image prompt remained in composer after submission attempt.',
							status_code=502,
							details={'submit_result': submit_result, 'retry_send': retry_send, 'post_send': post_send},
						)

				candidates = await self._wait_for_images(
					session=session,
					baseline_count=int(baseline.get('imageCount') or 0),
					baseline_candidates=list(baseline.get('imageCandidates') or []),
					desired_count=max_images,
					timeout_s=self._remaining_timeout_or_raise(request_deadline=request_deadline, stage='waiting for generated images'),
				)

				images: list[GeneratedImage] = []
				seen_image_keys: set[str] = set()
				for idx, candidate in enumerate(candidates[:max_images]):
					extracted = await self._run_js(session, build_extract_image_js(candidate))
					if not isinstance(extracted, dict) or not extracted.get('ok'):
						continue
					image = await self._persist_generated_image(
						request_id=request_id,
						index=idx,
						payload=extracted,
					)
					image_key = f'{image.source_url or image.file_name}|{image.width or 0}|{image.height or 0}'
					if image_key in seen_image_keys:
						continue
					seen_image_keys.add(image_key)
					images.append(image)

				if not images:
					raise AutomationError(
						'IMAGE_DOWNLOAD_FAILED',
						f'{self.cfg.display_name} produced an image candidate but the bridge could not fetch the original asset.',
						status_code=502,
						details={'candidate_count': len(candidates)},
					)
		except TimeoutError as e:
			raise AutomationError(
				f'{self.cfg.provider.upper()}_REQUEST_TIMEOUT',
				f'{self.cfg.display_name} image request exceeded timeout.',
				status_code=504,
				details={'timeout_s': effective_timeout},
			) from e

		elapsed_ms = int((time.time() - started) * 1000)
		return ImageResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			used_port=resolved_port,
			images=images,
			elapsed_ms=elapsed_ms,
		)

	async def _ensure_session(
		self,
		*,
		force_reconnect: bool,
		cdp_port: int | None,
		allow_auto_launch: bool = False,
	) -> BrowserSession:
		port = self._resolve_port(cdp_port)
		session = self._sessions.get(port)
		if session is not None and session.is_cdp_connected and not force_reconnect:
			return session

		if session is not None:
			try:
				await session.stop()
			except Exception:
				pass

		cdp_url = self.cdp_url_for_port(port)
		is_cdp_ready, cdp_meta = await asyncio.to_thread(_probe_cdp_sync, cdp_url)
		auto_launch_result: dict[str, Any] | None = None
		if not is_cdp_ready and allow_auto_launch:
			if _auto_launch_chrome_enabled():
				auto_launch_result = await asyncio.to_thread(_launch_local_chrome_cdp_sync, cdp_url, port)
				is_cdp_ready, cdp_meta = await asyncio.to_thread(_probe_cdp_sync, cdp_url)
			else:
				auto_launch_result = {'attempted': False, 'reason': 'auto_launch_disabled'}

		if not is_cdp_ready:
			details: dict[str, Any] = {'cdp_url': cdp_url, 'cdp_port': port, 'reason': 'cdp_endpoint_unreachable'}
			if auto_launch_result is not None:
				details['auto_launch'] = auto_launch_result
			raise AutomationError(
				'CDP_CONNECT_FAILED',
				f'Cannot reach Chrome CDP at {cdp_url}. Start Chrome with --remote-debugging-port={port}.',
				status_code=503,
				details=details,
			)

		last_error: Exception | None = None
		for attempt in range(self.cfg.cdp_connect_retries):
			session = self._build_browser_session(cdp_url)
			try:
				await session.start()
				self._sessions[port] = session
				return session
			except Exception as e:
				last_error = e
				try:
					await session.stop()
				except Exception:
					pass
				if attempt + 1 < self.cfg.cdp_connect_retries:
					await asyncio.sleep(self.cfg.cdp_connect_retry_delay_s)

		reason = str(last_error) if last_error is not None else 'unknown error'
		raise AutomationError(
			'CDP_CONNECT_FAILED',
			f'Cannot connect to Chrome CDP at {cdp_url}. Start Chrome with --remote-debugging-port={port}.',
			status_code=503,
			details={
				'cdp_url': cdp_url,
				'cdp_port': port,
				'reason': reason,
				'ws': cdp_meta.get('webSocketDebuggerUrl'),
				'browser': cdp_meta.get('Browser'),
				'attempts': self.cfg.cdp_connect_retries,
			},
		)

	async def _switch_to_gemini_tab(self, session: BrowserSession, *, force_activate: bool = False):
		tabs = await session.get_tabs()
		if not tabs:
			raise AutomationError('NO_TABS_FOUND', 'No browser tabs found in connected Chrome instance.', status_code=409)

		current_target = session.agent_focus_target_id
		candidates = []

		for tab in tabs:
			host = (urlparse(tab.url).hostname or '').lower()
			if any(host == h or host.endswith(f'.{h}') for h in self.cfg.tab_hosts):
				candidates.append(tab)

		if not candidates:
			raise AutomationError(
				f'{self.cfg.provider.upper()}_TAB_NOT_FOUND',
				f'No {self.cfg.display_name} tab found for hosts={self.cfg.tab_hosts}. Open {self.cfg.display_name} and login first.',
				status_code=409,
				details={'hosts': list(self.cfg.tab_hosts)},
			)

		selected = None
		for tab in candidates:
			if tab.target_id == current_target:
				selected = tab
				break
		if selected is None:
			selected = candidates[0]

		if selected.target_id != current_target or force_activate:
			event = session.event_bus.dispatch(SwitchTabEvent(target_id=selected.target_id))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)

		return selected

	async def _ensure_provider_tab(self, session: BrowserSession, *, force_activate: bool = False):
		try:
			return await self._switch_to_gemini_tab(session, force_activate=force_activate)
		except AutomationError as error:
			missing_codes = {
				'NO_TABS_FOUND',
				f'{self.cfg.provider.upper()}_TAB_NOT_FOUND',
			}
			if error.code not in missing_codes:
				raise

		target_url = (self.cfg.default_open_url or '').strip()
		if not target_url:
			raise AutomationError(
				'OPEN_URL_MISSING',
				f'No default URL configured for {self.cfg.display_name}.',
				status_code=500,
			)

		await session.navigate_to(target_url, new_tab=True)
		await asyncio.sleep(0.8)
		return await self._switch_to_gemini_tab(session, force_activate=True)

	async def _dismiss_blocking_ui(self, session: BrowserSession) -> None:
		try:
			await self._run_js(session, DISMISS_BLOCKING_UI_JS, ensure_tab=False)
		except Exception:
			return

	async def _apply_mode(self, session: BrowserSession, mode: ChatMode) -> bool:
		targets = MODE_TARGETS.get(mode)
		if not targets:
			return False

		result = await self._run_js(session, build_mode_switch_js(targets))
		if not isinstance(result, dict) or not result.get('ok'):
			return False

		await asyncio.sleep(0.8)
		snapshot = await self._snapshot(session)
		active_mode_text = str(snapshot.get('activeModeText') or '').lower()
		return any(token in active_mode_text for token in targets)

	async def _snapshot(self, session: BrowserSession) -> dict[str, Any]:
		payload = await self._run_js(session, SNAPSHOT_JS)
		if not isinstance(payload, dict):
			raise AutomationError('SNAPSHOT_INVALID', f'Failed to read {self.cfg.display_name} page state.', status_code=502)
		return payload

	async def _snapshot_images(self, session: BrowserSession) -> dict[str, Any]:
		payload = await self._run_js(session, IMAGE_SNAPSHOT_JS)
		if not isinstance(payload, dict):
			raise AutomationError('IMAGE_SNAPSHOT_INVALID', f'Failed to read {self.cfg.display_name} image page state.', status_code=502)
		return payload

	def _remaining_timeout_or_raise(self, *, request_deadline: float, stage: str) -> float:
		remaining = request_deadline - time.time()
		if remaining <= 0:
			raise AutomationError(
				f'{self.cfg.provider.upper()}_REQUEST_TIMEOUT',
				f'Timed out while {stage}.',
				status_code=504,
				details={'stage': stage, 'remaining_s': round(remaining, 3)},
			)
		return max(1.0, remaining)

	async def _wait_for_answer(
		self,
		*,
		session: BrowserSession,
		baseline_count: int,
		baseline_last: str,
		timeout_s: float,
	) -> str:
		started = time.time()
		deadline = started + timeout_s
		hard_deadline = deadline + STREAM_SETTLE_GRACE_SECONDS
		stable_count = 0
		last_seen_signature = ''
		saw_new_response = False
		transient_eval_timeouts = 0
		saw_streaming = False
		last_streaming_at = 0.0
		best_answer = ''
		best_signature = ''
		last_answer_change_at = started
		baseline_norm = baseline_last.strip()

		while True:
			now = time.time()
			if now > hard_deadline:
				break
			if now > deadline:
				streaming_grace_until = min(last_streaming_at + STREAM_SETTLE_GRACE_SECONDS, hard_deadline)
				if not (saw_streaming and last_streaming_at > 0 and now <= streaming_grace_until):
					break
			try:
				snapshot = await self._snapshot(session)
			except AutomationError as e:
				if e.code == 'CDP_EVALUATION_TIMEOUT':
					transient_eval_timeouts += 1
					await asyncio.sleep(self.cfg.poll_interval_s)
					continue
				raise

			error_texts = [str(x) for x in snapshot.get('errorTexts') or []]
			error_text_joined = ' | '.join(error_texts).lower()
			if error_text_joined:
				if any(keyword in error_text_joined for keyword in RATE_LIMIT_KEYWORDS):
					raise AutomationError(
						f'{self.cfg.provider.upper()}_RATE_LIMIT',
						f'{self.cfg.display_name} hit a quota/rate-limit condition. Retry later.',
						status_code=429,
						details={'errors': error_texts[:5]},
					)
				if any(keyword in error_text_joined for keyword in ERROR_KEYWORDS):
					raise AutomationError(
						f'{self.cfg.provider.upper()}_UI_ERROR',
						f'{self.cfg.display_name} reported a UI-level error.',
						status_code=502,
						details={'errors': error_texts[:5]},
					)

			response_count = int(snapshot.get('responseCount') or 0)
			last_text = str(snapshot.get('lastResponseText') or '').strip()
			tail_texts = [str(x).strip() for x in (snapshot.get('responseTextsTail') or []) if str(x).strip()]
			is_streaming = bool(snapshot.get('isStreaming'))
			if is_streaming:
				saw_streaming = True
				last_streaming_at = time.time()

			new_tail = [t for t in tail_texts if t != baseline_norm]
			candidate_texts = new_tail if new_tail else ([last_text] if last_text and last_text != baseline_norm else [])
			candidate_text = candidate_texts[-1] if candidate_texts else ''
			candidate_signature = json.dumps(candidate_texts[-2:], ensure_ascii=False)
			previous_best = best_answer
			if candidate_text and (len(candidate_text) >= len(best_answer) or candidate_signature != best_signature):
				best_answer = candidate_text
				best_signature = candidate_signature
			if best_answer != previous_best:
				last_answer_change_at = time.time()

			new_by_count = response_count > baseline_count
			new_by_text = bool(candidate_text)
			new_after_stream = saw_streaming and bool(best_answer)
			if new_by_count or new_by_text or new_after_stream:
				saw_new_response = True

			if saw_new_response and best_answer:
				signature = candidate_signature if candidate_signature != '[]' else best_signature
				if signature and signature == last_seen_signature:
					stable_count += 1
				else:
					stable_count = 0

				last_seen_signature = signature
				send_ready = bool(snapshot.get('sendButtonFound')) and not bool(snapshot.get('sendDisabled'))
				answer_idle_s = time.time() - last_answer_change_at
				word_count = len([token for token in best_answer.split() if token.strip()])
				has_meaningful_stream_text = word_count >= CHAT_STALE_MIN_WORDS or len(best_answer) >= CHAT_STALE_MIN_CHARS
				if stable_count >= self.cfg.stable_polls and (
					not is_streaming
					or (
						has_meaningful_stream_text
						and send_ready
						and answer_idle_s >= max(1.0, self.cfg.poll_interval_s * 2.0)
					)
					or (
						has_meaningful_stream_text
						and answer_idle_s >= CHAT_STALE_CONTENT_RETURN_S
					)
				):
					return best_answer

			await asyncio.sleep(self.cfg.poll_interval_s)

		if best_answer:
			return best_answer

		raise AutomationError(
			f'{self.cfg.provider.upper()}_RESPONSE_TIMEOUT',
			f'Timed out while waiting for {self.cfg.display_name} final response.',
			status_code=504,
			details={
				'timeout_s': timeout_s,
				'hard_timeout_s': timeout_s + STREAM_SETTLE_GRACE_SECONDS,
				'baseline_count': baseline_count,
				'baseline_last_len': len(baseline_last),
				'saw_new_response': saw_new_response,
				'transient_eval_timeouts': transient_eval_timeouts,
			},
		)

	async def _wait_for_images(
		self,
		*,
		session: BrowserSession,
		baseline_count: int,
		baseline_candidates: list[dict[str, Any]],
		desired_count: int,
		timeout_s: float,
	) -> list[dict[str, Any]]:
		started = time.time()
		deadline = started + timeout_s
		hard_deadline = deadline + STREAM_SETTLE_GRACE_SECONDS
		stable_count = 0
		last_signature = ''
		transient_eval_timeouts = 0
		last_candidates: list[dict[str, Any]] = []
		baseline_keys = {self._candidate_key(candidate) for candidate in baseline_candidates}
		last_streaming_at = 0.0
		saw_streaming = False

		while True:
			now = time.time()
			if now > hard_deadline:
				break
			if now > deadline:
				streaming_grace_until = min(last_streaming_at + STREAM_SETTLE_GRACE_SECONDS, hard_deadline)
				if not (saw_streaming and last_streaming_at > 0 and now <= streaming_grace_until):
					break
			try:
				snapshot = await self._snapshot_images(session)
			except AutomationError as e:
				if e.code == 'CDP_EVALUATION_TIMEOUT':
					transient_eval_timeouts += 1
					await asyncio.sleep(self.cfg.poll_interval_s)
					continue
				raise

			error_texts = [str(x) for x in snapshot.get('errorTexts') or []]
			error_text_joined = ' | '.join(error_texts).lower()
			if error_text_joined:
				if any(keyword in error_text_joined for keyword in RATE_LIMIT_KEYWORDS):
					raise AutomationError(
						f'{self.cfg.provider.upper()}_RATE_LIMIT',
						f'{self.cfg.display_name} hit a quota/rate-limit condition during image generation. Retry later.',
						status_code=429,
						details={'errors': error_texts[:5]},
					)
				if any(keyword in error_text_joined for keyword in ERROR_KEYWORDS):
					raise AutomationError(
						f'{self.cfg.provider.upper()}_IMAGE_UI_ERROR',
						f'{self.cfg.display_name} reported an image-generation UI error.',
						status_code=502,
					details={'errors': error_texts[:5]},
					)

			candidate_count = int(snapshot.get('imageCount') or 0)
			is_streaming = bool(snapshot.get('isStreaming'))
			if is_streaming:
				saw_streaming = True
				last_streaming_at = time.time()

			candidates = list(snapshot.get('imageCandidates') or [])
			new_candidates = [candidate for candidate in candidates if self._candidate_key(candidate) not in baseline_keys]
			if (candidate_count > baseline_count or new_candidates) and new_candidates:
				current_signature = json.dumps(new_candidates[:4], sort_keys=True)
				if current_signature == last_signature:
					stable_count += 1
				else:
					stable_count = 0
				last_signature = current_signature
				last_candidates = new_candidates
				if stable_count >= self.cfg.stable_polls and (len(new_candidates) >= desired_count or not is_streaming):
					return new_candidates

			await asyncio.sleep(self.cfg.poll_interval_s)

		if last_candidates:
			return last_candidates

		raise AutomationError(
			f'{self.cfg.provider.upper()}_IMAGE_TIMEOUT',
			f'Timed out while waiting for {self.cfg.display_name} generated image assets.',
			status_code=504,
			details={
				'timeout_s': timeout_s,
				'hard_timeout_s': timeout_s + STREAM_SETTLE_GRACE_SECONDS,
				'baseline_count': baseline_count,
				'last_candidate_count': len(last_candidates),
				'transient_eval_timeouts': transient_eval_timeouts,
			},
		)

	def _candidate_key(self, candidate: dict[str, Any]) -> str:
		width = _to_int(str(candidate.get('width')) if candidate.get('width') is not None else None, 0)
		height = _to_int(str(candidate.get('height')) if candidate.get('height') is not None else None, 0)
		return f"{str(candidate.get('sourceUrl') or '')}|{width}|{height}"

	async def _persist_generated_image(self, *, request_id: str, index: int, payload: dict[str, Any]) -> GeneratedImage:
		return await asyncio.to_thread(
			self._persist_generated_image_sync,
			request_id=request_id,
			index=index,
			payload=payload,
		)

	def _persist_generated_image_sync(self, *, request_id: str, index: int, payload: dict[str, Any]) -> GeneratedImage:
		data_url = str(payload.get('dataUrl') or '')
		source_url = str(payload.get('sourceUrl') or '') or None
		if not data_url.startswith('data:'):
			if source_url and source_url.startswith(('http://', 'https://')):
				content = self._download_binary_source(source_url)
				content_type = mimetypes.guess_type(source_url)[0] or 'application/octet-stream'
			else:
				raise AutomationError(
					'IMAGE_PAYLOAD_INVALID',
					'Image payload did not contain retrievable original bytes.',
					status_code=502,
					details={'source_url': source_url},
				)
		else:
			try:
				header, b64 = data_url.split(',', 1)
			except ValueError as e:
				raise AutomationError(
					'IMAGE_DATA_URL_INVALID',
					'Image payload returned an invalid data URL.',
					status_code=502,
					details={'source_url': source_url},
				) from e

			content_type = 'application/octet-stream'
			if ';base64' in header:
				content_type = header[5:].split(';', 1)[0] or content_type
			try:
				content = base64.b64decode(b64, validate=True)
			except (binascii.Error, ValueError) as e:
				raise AutomationError(
					'IMAGE_DATA_URL_INVALID',
					'Image payload returned invalid base64 data.',
					status_code=502,
					details={'source_url': source_url},
				) from e

		ext = mimetypes.guess_extension(content_type) or '.bin'
		if ext == '.jpe':
			ext = '.jpg'
		file_name = f'{self.cfg.provider}_{request_id}_{index + 1}_{uuid4().hex[:8]}{ext}'
		file_path = _ensure_image_storage_dir() / file_name
		with file_path.open('wb') as handle:
			handle.write(content)

		return GeneratedImage(
			file_name=file_name,
			content_type=content_type,
			byte_size=len(content),
			local_path=str(file_path),
			download_url=_build_download_url(file_name),
			source_url=source_url,
			width=_to_int(str(payload.get('width')) if payload.get('width') is not None else None, 0) or None,
			height=_to_int(str(payload.get('height')) if payload.get('height') is not None else None, 0) or None,
		)

	def _download_binary_source(self, source_url: str) -> bytes:
		request = Request(source_url, headers={'User-Agent': 'Mozilla/5.0'})
		with urlopen(request, timeout=60) as response:
			return response.read()

	async def _run_js(self, session: BrowserSession, expression: str, *, ensure_tab: bool = True) -> Any:
		active_session = session
		port = self._find_port_for_session(session)
		last_error: Exception | None = None

		for attempt in range(2):
			try:
				if ensure_tab:
					await self._ensure_provider_tab(active_session, force_activate=True)
				now = time.time()
				if FORCE_FOREGROUND_WINDOW and (now - self._last_foreground_attempt_at) >= FOREGROUND_RETRY_INTERVAL_S:
					self._last_foreground_attempt_at = now
					try:
						await asyncio.to_thread(_activate_chrome_window_sync)
					except Exception:
						pass
				cdp_session = await active_session.get_or_create_cdp_session(focus=True)
				if FORCE_RESTORE_WINDOW:
					try:
						target_id = getattr(cdp_session, 'target_id', None) or active_session.agent_focus_target_id
						if target_id:
							window_info = await cdp_session.cdp_client.send.Browser.getWindowForTarget(params={'targetId': target_id})
							window_id = window_info.get('windowId')
							bounds = window_info.get('bounds') or {}
							state = str(bounds.get('windowState') or '').lower()
							if window_id is not None and state == 'minimized':
								await cdp_session.cdp_client.send.Browser.setWindowBounds(
									params={'windowId': window_id, 'bounds': {'windowState': 'normal'}}
								)
					except Exception:
						pass
				try:
					await cdp_session.cdp_client.send.Page.bringToFront(session_id=cdp_session.session_id)
				except Exception:
					pass
				if FORCE_PAGE_ACTIVE_STATE:
					try:
						await cdp_session.cdp_client.send.Page.setWebLifecycleState(
							params={'state': 'active'},
							session_id=cdp_session.session_id,
						)
					except Exception:
						pass
					try:
						await cdp_session.cdp_client.send.Emulation.setFocusEmulationEnabled(
							params={'enabled': True},
							session_id=cdp_session.session_id,
						)
					except Exception:
						pass
				result = await cdp_session.cdp_client.send.Runtime.evaluate(
					params={
						'expression': expression,
						'returnByValue': True,
						'awaitPromise': True,
					},
					session_id=cdp_session.session_id,
				)

				if result.get('exceptionDetails'):
					raise AutomationError(
						'JS_EVALUATION_FAILED',
						f'JavaScript evaluation failed on {self.cfg.display_name} tab.',
						status_code=502,
						details={'exception': result['exceptionDetails']},
					)

				return result.get('result', {}).get('value')
			except AutomationError:
				raise
			except Exception as e:
				last_error = e
				if attempt == 0:
					active_session = await self._ensure_session(force_reconnect=True, cdp_port=port)
					await self._switch_to_gemini_tab(active_session, force_activate=True)
					continue

		reason = str(last_error) if last_error is not None else 'unknown error'
		reason_lower = reason.lower()
		if 'did not respond' in reason_lower or 'timeout' in reason_lower:
			raise AutomationError(
				'CDP_EVALUATION_TIMEOUT',
				f'Chrome CDP timed out while evaluating {self.cfg.display_name} page state.',
				status_code=504,
				details={'reason': reason},
			)

		raise AutomationError(
			'CDP_EVALUATION_FAILED',
				f'Chrome CDP evaluation failed while interacting with {self.cfg.display_name}.',
			status_code=503,
			details={'reason': reason},
		)


GEMINI_CFG = ServiceConfig.from_env(
	provider='gemini',
	display_name='Gemini',
	default_hosts='gemini.google.com',
	default_open_url='https://gemini.google.com/app',
	supports_mode=True,
)
GPT_CFG = ServiceConfig.from_env(
	provider='gpt',
	display_name='ChatGPT',
	default_hosts='chatgpt.com,chat.openai.com',
	default_open_url='https://chatgpt.com/',
	supports_mode=False,
)

DISCOVERY_PORTS = _parse_discovery_ports(os.getenv('CHAT_BRIDGE_DISCOVERY_PORTS', '9222,9223,9224'))
RATE_LIMIT_COOLDOWN_S = max(5.0, _to_float(os.getenv('CHAT_BRIDGE_RATE_LIMIT_COOLDOWN_S'), 45.0))
MAX_BATCH_PROMPTS = max(1, _to_int(os.getenv('CHAT_BRIDGE_MAX_BATCH_PROMPTS'), 24))


class PortScheduler:
	def __init__(self, label: str):
		self.label = label
		self._locks: dict[int, asyncio.Lock] = {}
		self._cooldown_until: dict[int, float] = {}
		self._reserved_ports: set[int] = set()
		self._cursor = 0
		self._state_lock = asyncio.Lock()
		self._state_changed = asyncio.Condition(self._state_lock)
		self._wait_queue: deque[str] = deque()

	def register_ports(self, ports: list[int]) -> None:
		for port in ports:
			if 1 <= port <= 65535 and port not in self._locks:
				self._locks[port] = asyncio.Lock()

	async def acquire_port(self, candidate_ports: list[int], *, deadline: float) -> int:
		ports = sorted({port for port in candidate_ports if 1 <= port <= 65535})
		if not ports:
			raise AutomationError(
				'NO_PORTS_AVAILABLE',
				'No valid ports available for this request.',
				status_code=409,
			)

		self.register_ports(ports)
		token = uuid4().hex

		async with self._state_lock:
			self._wait_queue.append(token)
			self._state_changed.notify_all()

		try:
			while True:
				now = time.time()
				if now >= deadline:
					raise AutomationError(
						'NO_PORTS_AVAILABLE',
						f'No available {self.label} port before request deadline.',
						status_code=504,
					)

				port: int | None = None
				async with self._state_lock:
					while True:
						now = time.time()
						if now >= deadline:
							raise AutomationError(
								'NO_PORTS_AVAILABLE',
								f'No available {self.label} port before request deadline.',
								status_code=504,
							)

						if self._wait_queue and self._wait_queue[0] == token:
							port = self._reserve_ready_port_locked(ports, now)
							if port is not None:
								self._wait_queue.popleft()
								self._state_changed.notify_all()
								break

						wait_for = min(self._wait_duration_locked(ports, now), max(0.05, deadline - now))
						try:
							await asyncio.wait_for(self._state_changed.wait(), timeout=wait_for)
						except TimeoutError:
							pass

				if port is None:
					continue

				lock = self._locks[port]
				await lock.acquire()
				return port
		finally:
			async with self._state_lock:
				if token in self._wait_queue:
					self._wait_queue.remove(token)
					self._state_changed.notify_all()

	async def release_port(self, port: int) -> None:
		async with self._state_lock:
			self._reserved_ports.discard(port)
			lock = self._locks.get(port)
			if lock is not None and lock.locked():
				lock.release()
			self._state_changed.notify_all()

	async def mark_cooldown(self, port: int, cooldown_s: float) -> None:
		until = time.time() + max(1.0, cooldown_s)
		async with self._state_lock:
			current = self._cooldown_until.get(port, 0.0)
			self._cooldown_until[port] = max(current, until)
			self._state_changed.notify_all()

	def _reserve_ready_port_locked(self, ports: list[int], now: float) -> int | None:
		ready = [
			port
			for port in ports
			if self._cooldown_until.get(port, 0.0) <= now
			and port not in self._reserved_ports
			and not self._locks[port].locked()
		]
		if not ready:
			return None

		if self._cursor >= len(ready):
			self._cursor = 0

		selected = ready[self._cursor]
		self._cursor = (self._cursor + 1) % len(ready)
		self._reserved_ports.add(selected)
		return selected

	def _wait_duration_locked(self, ports: list[int], now: float) -> float:
		cooling = [self._cooldown_until.get(port, 0.0) - now for port in ports if self._cooldown_until.get(port, 0.0) > now]
		if not cooling:
			return 0.15
		return min(0.5, max(0.05, min(cooling)))


GEMINI_SERVICE = GeminiBridgeService(GEMINI_CFG)
GPT_SERVICE = GeminiBridgeService(GPT_CFG)
PORT_SCHEDULER = PortScheduler('bridge')
# Keep aliases for backward compatibility in tests and external imports.
GEMINI_SCHEDULER = PORT_SCHEDULER
GPT_SCHEDULER = PORT_SCHEDULER


def _get_service(provider: ChatProvider) -> GeminiBridgeService:
	return GPT_SERVICE if provider == 'gpt' else GEMINI_SERVICE


def _all_services() -> dict[ChatProvider, GeminiBridgeService]:
	return {'gemini': GEMINI_SERVICE, 'gpt': GPT_SERVICE}


def _get_scheduler(provider: ChatProvider) -> PortScheduler:
	return PORT_SCHEDULER


def _is_rate_limit_error(error: AutomationError) -> bool:
	if error.status_code == 429:
		return True
	code = (error.code or '').upper()
	message = (error.message or '').lower()
	return 'RATE_LIMIT' in code or 'QUOTA' in code or any(token in message for token in RATE_LIMIT_KEYWORDS)


def _is_transient_failover_error(error: AutomationError) -> bool:
	code = (error.code or '').upper()
	if _is_rate_limit_error(error):
		return True
	return any(
		token in code
		for token in (
			'REQUEST_TIMEOUT',
			'RESPONSE_TIMEOUT',
			'CDP_CONNECT_FAILED',
			'CDP_EVALUATION_TIMEOUT',
			'CDP_EVALUATION_FAILED',
		)
	)


def _extract_prompts(prompt: list[str]) -> list[str]:
	items: list[str] = []
	for item in prompt:
		candidate = str(item or '').strip()
		if candidate:
			items.append(candidate)

	if not items:
		raise AutomationError(
			'PROMPT_REQUIRED',
			'Provide prompt or prompts with at least one non-empty value.',
			status_code=422,
		)

	if len(items) > MAX_BATCH_PROMPTS:
		raise AutomationError(
			'BATCH_TOO_LARGE',
			f'Batch size exceeds limit ({MAX_BATCH_PROMPTS}).',
			status_code=422,
			details={'max_batch_prompts': MAX_BATCH_PROMPTS},
		)

	return items


def _sanitize_ports(ports: list[int] | None) -> list[int]:
	values: set[int] = set()
	for value in ports or []:
		if 1 <= int(value) <= 65535:
			values.add(int(value))
	return sorted(values)


def _resolve_request_ports(service: GeminiBridgeService) -> list[int]:
	ports: set[int] = set(DISCOVERY_PORTS)
	ports.update(service.managed_ports())
	ports.add(service._default_port)
	return sorted(port for port in ports if 1 <= port <= 65535)


def _collect_candidate_ports(requested_ports: list[int] | None) -> list[int]:
	ports: set[int] = set(DISCOVERY_PORTS)
	for service in _all_services().values():
		ports.update(service.managed_ports())
	for port in requested_ports or []:
		if 1 <= port <= 65535:
			ports.add(port)
	return sorted(ports)


async def _build_port_statuses(ports: list[int]) -> list[PortStatus]:
	statuses: list[PortStatus] = []
	services = _all_services()
	for port in ports:
		base_status = await GEMINI_SERVICE.probe_port(port)
		managed_by: list[ChatProvider] = []
		for provider, service in services.items():
			if service.has_managed_port(port):
				managed_by.append(provider)
		status = base_status.model_copy(update={'managed_by': managed_by, 'active': bool(base_status.active or managed_by)})
		statuses.append(status)
	return statuses


async def _run_chat_prompt(
	*,
	service: GeminiBridgeService,
	scheduler: PortScheduler,
	request_id: str,
	prompt: str,
	mode: ChatMode | None,
	timeout_s: float | None,
	candidate_ports: list[int],
) -> ChatItemResult:
	started = time.time()
	deadline = started + (timeout_s if timeout_s is not None else service.cfg.default_timeout_s)
	max_attempts = max(2, len(candidate_ports) * 2)
	last_rate_limit: AutomationError | None = None

	for attempt in range(max_attempts):
		port = await scheduler.acquire_port(candidate_ports, deadline=deadline)
		try:
			remaining = max(10.0, deadline - time.time())
			response = await service.ask(
				request_id=request_id,
				prompt=prompt,
				cdp_port=port,
				mode=mode,
				timeout_s=min(remaining, timeout_s or service.cfg.default_timeout_s),
			)
			elapsed_ms = int((time.time() - started) * 1000)
			return ChatItemResult(
				prompt=prompt,
				success=True,
				port=port,
				answer=response.answer,
				elapsed_ms=elapsed_ms,
			)
		except AutomationError as error:
			if _is_rate_limit_error(error) and attempt + 1 < max_attempts:
				await scheduler.mark_cooldown(port, RATE_LIMIT_COOLDOWN_S)
				last_rate_limit = error
				continue
			if _is_transient_failover_error(error) and attempt + 1 < max_attempts:
				continue
			elapsed_ms = int((time.time() - started) * 1000)
			return ChatItemResult(
				prompt=prompt,
				success=False,
				port=port,
				error_code=error.code,
				error_message=error.message,
				details=error.details,
				elapsed_ms=elapsed_ms,
			)
		finally:
			await scheduler.release_port(port)

	final_elapsed = int((time.time() - started) * 1000)
	if last_rate_limit is None:
		return ChatItemResult(
			prompt=prompt,
			success=False,
			port=None,
			error_code='NO_PORTS_AVAILABLE',
			error_message='No available ports to process prompt.',
			elapsed_ms=final_elapsed,
		)

	return ChatItemResult(
		prompt=prompt,
		success=False,
		port=None,
		error_code=last_rate_limit.code,
		error_message=last_rate_limit.message,
		details=last_rate_limit.details,
		elapsed_ms=final_elapsed,
	)


async def _run_image_prompt(
	*,
	service: GeminiBridgeService,
	scheduler: PortScheduler,
	request_id: str,
	prompt: str,
	timeout_s: float | None,
	max_images: int,
	candidate_ports: list[int],
) -> ImageItemResult:
	started = time.time()
	deadline = started + (timeout_s if timeout_s is not None else service.cfg.default_timeout_s)
	max_attempts = max(2, len(candidate_ports) * 2)
	last_rate_limit: AutomationError | None = None

	for attempt in range(max_attempts):
		port = await scheduler.acquire_port(candidate_ports, deadline=deadline)
		try:
			remaining = max(20.0, deadline - time.time())
			response = await service.create_image(
				request_id=request_id,
				prompt=prompt,
				cdp_port=port,
				timeout_s=min(remaining, timeout_s or service.cfg.default_timeout_s),
				max_images=max_images,
			)
			elapsed_ms = int((time.time() - started) * 1000)
			return ImageItemResult(
				prompt=prompt,
				success=True,
				port=port,
				images=response.images,
				elapsed_ms=elapsed_ms,
			)
		except AutomationError as error:
			if _is_rate_limit_error(error) and attempt + 1 < max_attempts:
				await scheduler.mark_cooldown(port, RATE_LIMIT_COOLDOWN_S)
				last_rate_limit = error
				continue
			if _is_transient_failover_error(error) and attempt + 1 < max_attempts:
				continue
			elapsed_ms = int((time.time() - started) * 1000)
			return ImageItemResult(
				prompt=prompt,
				success=False,
				port=port,
				error_code=error.code,
				error_message=error.message,
				details=error.details,
				elapsed_ms=elapsed_ms,
			)
		finally:
			await scheduler.release_port(port)

	final_elapsed = int((time.time() - started) * 1000)
	if last_rate_limit is None:
		return ImageItemResult(
			prompt=prompt,
			success=False,
			port=None,
			error_code='NO_PORTS_AVAILABLE',
			error_message='No available ports to process image prompt.',
			elapsed_ms=final_elapsed,
		)

	return ImageItemResult(
		prompt=prompt,
		success=False,
		port=None,
		error_code=last_rate_limit.code,
		error_message=last_rate_limit.message,
		details=last_rate_limit.details,
		elapsed_ms=final_elapsed,
	)


@asynccontextmanager
async def lifespan(app: FastAPI):
	_ensure_image_storage_dir()
	await GEMINI_SERVICE.startup()
	await GPT_SERVICE.startup()
	try:
		yield
	finally:
		await GEMINI_SERVICE.shutdown()
		await GPT_SERVICE.shutdown()


app = FastAPI(
	title='Browser Chat Bridge',
	version='2.0.0',
	lifespan=lifespan,
	docs_url='/docs',
	redoc_url=None,
	openapi_url='/openapi.json',
)


async def _dispatch_chat(provider: ChatProvider, payload: ChatRequest):
	service = _get_service(provider)
	scheduler = _get_scheduler(provider)
	request_id = str(uuid4())
	started = time.time()

	try:
		prompts = _extract_prompts(payload.prompt)
		candidate_ports = _resolve_request_ports(service)
		scheduler.register_ports(candidate_ports)
		results = await asyncio.gather(
			*[
				_run_chat_prompt(
					service=service,
					scheduler=scheduler,
					request_id=request_id,
					prompt=prompt,
					mode=payload.mode,
					timeout_s=payload.timeout_s,
					candidate_ports=candidate_ports,
				)
				for prompt in prompts
			]
		)

		elapsed_ms = int((time.time() - started) * 1000)
		successes = [item for item in results if item.success]
		if len(prompts) == 1:
			item = results[0]
			status_code = 200 if item.success else 429 if item.error_code and 'RATE_LIMIT' in item.error_code else 502
			body = ChatResponse(
				success=item.success,
				request_id=request_id,
				provider=provider,
				mode_requested=payload.mode,
				mode_applied=None,
				used_port=item.port,
				answer=item.answer,
				results=results,
				error_code=item.error_code,
				error_message=item.error_message,
				details=item.details,
				elapsed_ms=elapsed_ms,
			)
			if item.success:
				return body
			return JSONResponse(status_code=status_code, content=body.model_dump(mode='json'))

		body = ChatResponse(
			success=bool(successes),
			request_id=request_id,
			provider=provider,
			mode_requested=payload.mode,
			mode_applied=None,
			used_port=None,
			answer=None,
			results=results,
			error_code=None if successes else 'BATCH_ALL_FAILED',
			error_message=None if successes else 'All prompts failed.',
			details={'total': len(results), 'success': len(successes), 'failure': len(results) - len(successes)},
			elapsed_ms=elapsed_ms,
		)
		if successes:
			return body
		return JSONResponse(status_code=502, content=body.model_dump(mode='json'))
	except AutomationError as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = ChatResponse(
			success=False,
			request_id=request_id,
			provider=provider,
			mode_requested=payload.mode,
			mode_applied=None,
			used_port=None,
			answer=None,
			results=None,
			error_code=e.code,
			error_message=e.message,
			details=e.details,
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=e.status_code, content=body.model_dump(mode='json'))
	except Exception as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = ChatResponse(
			success=False,
			request_id=request_id,
			provider=provider,
			mode_requested=payload.mode,
			mode_applied=None,
			used_port=None,
			answer=None,
			results=None,
			error_code='UNHANDLED_ERROR',
			error_message='Unhandled server error.',
			details={'reason': str(e)},
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=500, content=body.model_dump(mode='json'))


async def _dispatch_image(provider: ChatProvider, payload: ImageRequest):
	service = _get_service(provider)
	scheduler = _get_scheduler(provider)
	request_id = str(uuid4())
	started = time.time()

	try:
		prompts = _extract_prompts(payload.prompt)
		candidate_ports = _resolve_request_ports(service)
		scheduler.register_ports(candidate_ports)
		results = await asyncio.gather(
			*[
				_run_image_prompt(
					service=service,
					scheduler=scheduler,
					request_id=request_id,
					prompt=prompt,
					timeout_s=payload.timeout_s,
					max_images=payload.max_images,
					candidate_ports=candidate_ports,
				)
				for prompt in prompts
			]
		)

		successes = [item for item in results if item.success and item.images]
		elapsed_ms = int((time.time() - started) * 1000)

		if payload.response_format == 'binary':
			if not successes:
				body = ImageResponse(
					success=False,
					request_id=request_id,
					provider=provider,
					used_port=None,
					images=None,
					results=results,
					error_code='BATCH_ALL_FAILED',
					error_message='No successful image result to render as binary.',
					details={'total': len(results), 'success': 0},
					elapsed_ms=elapsed_ms,
				)
				return JSONResponse(status_code=502, content=body.model_dump(mode='json'))

			first_item = successes[0]
			first_image = (first_item.images or [None])[0]
			if first_image is None or not first_image.local_path:
				body = ImageResponse(
					success=False,
					request_id=request_id,
					provider=provider,
					used_port=first_item.port,
					images=None,
					results=results,
					error_code='BINARY_IMAGE_NOT_AVAILABLE',
					error_message='Binary image output requested but no local image path found.',
					details={'port': first_item.port},
					elapsed_ms=elapsed_ms,
				)
				return JSONResponse(status_code=502, content=body.model_dump(mode='json'))

			image_path = Path(first_image.local_path)
			if not image_path.is_file():
				body = ImageResponse(
					success=False,
					request_id=request_id,
					provider=provider,
					used_port=first_item.port,
					images=None,
					results=results,
					error_code='BINARY_IMAGE_NOT_FOUND',
					error_message='Binary image output requested but file is missing on local storage.',
					details={'path': str(image_path)},
					elapsed_ms=elapsed_ms,
				)
				return JSONResponse(status_code=404, content=body.model_dump(mode='json'))

			headers = {
				'Content-Disposition': f'inline; filename={first_image.file_name}',
				'X-Bridge-Provider': provider,
				'X-Bridge-Port': str(first_item.port or ''),
				'X-Bridge-Request-Id': request_id,
			}
			return FileResponse(path=image_path, media_type=first_image.content_type, filename=first_image.file_name, headers=headers)

		if len(prompts) == 1:
			item = results[0]
			status_code = 200 if item.success else 429 if item.error_code and 'RATE_LIMIT' in item.error_code else 502
			body = ImageResponse(
				success=item.success,
				request_id=request_id,
				provider=provider,
				used_port=item.port,
				images=item.images,
				results=results,
				error_code=item.error_code,
				error_message=item.error_message,
				details=item.details,
				elapsed_ms=elapsed_ms,
			)
			if item.success:
				return body
			return JSONResponse(status_code=status_code, content=body.model_dump(mode='json'))

		body = ImageResponse(
			success=bool(successes),
			request_id=request_id,
			provider=provider,
			used_port=successes[0].port if successes else None,
			images=(successes[0].images if successes else None),
			results=results,
			error_code=None if successes else 'BATCH_ALL_FAILED',
			error_message=None if successes else 'All image prompts failed.',
			details={'total': len(results), 'success': len(successes), 'failure': len(results) - len(successes)},
			elapsed_ms=elapsed_ms,
		)
		if successes:
			return body
		return JSONResponse(status_code=502, content=body.model_dump(mode='json'))
	except AutomationError as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = ImageResponse(
			success=False,
			request_id=request_id,
			provider=provider,
			used_port=None,
			images=None,
			results=None,
			error_code=e.code,
			error_message=e.message,
			details=e.details,
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=e.status_code, content=body.model_dump(mode='json'))
	except Exception as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = ImageResponse(
			success=False,
			request_id=request_id,
			provider=provider,
			used_port=None,
			images=None,
			results=None,
			error_code='UNHANDLED_ERROR',
			error_message='Unhandled server error.',
			details={'reason': str(e)},
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=500, content=body.model_dump(mode='json'))


@app.post('/v1/web/open', response_model=OpenWebResponse)
async def open_web(payload: OpenWebRequest):
	started = time.time()
	ports = _sanitize_ports(payload.ports)
	if not ports:
		ports = list(DISCOVERY_PORTS)
	if not ports:
		body = OpenWebResponse(
			success=False,
			port=None,
			cdp_url=None,
			results=[],
			active_ports=[],
			error_code='OPEN_PORT_REQUIRED',
			error_message='Provide port or ports for /v1/web/open.',
			details={},
			elapsed_ms=int((time.time() - started) * 1000),
		)
		return JSONResponse(status_code=422, content=body.model_dump(mode='json'))

	try:
		async def _open_single_port(port: int) -> OpenWebResult:
			try:
				result = await GEMINI_SERVICE.open_web(
					port=port,
					url=None,
					new_tab=False,
					force_reconnect=payload.force_reconnect,
				)
				return OpenWebResult(
					success=True,
					port=port,
					cdp_url=str(result.get('cdp_url') or ''),
				)
			except AutomationError as error:
				return OpenWebResult(
					success=False,
					port=port,
					cdp_url=GEMINI_SERVICE.cdp_url_for_port(port),
					error_code=error.code,
					error_message=error.message,
					details=error.details,
				)

		results = await asyncio.gather(*[_open_single_port(port) for port in ports])

		GEMINI_SCHEDULER.register_ports(ports)
		GPT_SCHEDULER.register_ports(ports)

		statuses = await _build_port_statuses(_collect_candidate_ports(ports))
		active_ports = [item.port for item in statuses if item.active]
		success = any(item.success for item in results)
		elapsed_ms = int((time.time() - started) * 1000)
		primary = results[0] if results else None
		body = OpenWebResponse(
			success=success,
			port=primary.port if primary is not None else None,
			cdp_url=primary.cdp_url if primary is not None else None,
			results=results,
			active_ports=active_ports,
			error_code=None if success else 'OPEN_ALL_FAILED',
			error_message=None if success else 'Unable to connect requested Chrome ports.',
			details={'ports': ports},
			elapsed_ms=elapsed_ms,
		)
		if success:
			return body
		return JSONResponse(status_code=503, content=body.model_dump(mode='json'))
	except Exception as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = OpenWebResponse(
			success=False,
			port=None,
			cdp_url=None,
			results=None,
			active_ports=[],
			error_code='UNHANDLED_ERROR',
			error_message='Unhandled server error.',
			details={'reason': str(e)},
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=500, content=body.model_dump(mode='json'))


@app.post('/v1/ports/ping', response_model=PingPortsResponse)
async def ping_ports(payload: PingPortsRequest):
	ports = _collect_candidate_ports(payload.ports)
	statuses = await _build_port_statuses(ports)
	return PingPortsResponse(success=True, ports=statuses)


@app.get('/v1/ports/ping', response_model=PingPortsResponse)
async def ping_ports_default():
	ports = _collect_candidate_ports(None)
	statuses = await _build_port_statuses(ports)
	return PingPortsResponse(success=True, ports=statuses)


@app.post('/v1/ports/close', response_model=ClosePortResponse)
async def close_port(payload: ClosePortRequest):
	providers: list[ChatProvider] = [payload.provider] if payload.provider is not None else ['gemini', 'gpt']
	closed_by: list[ChatProvider] = []

	for provider in providers:
		service = _get_service(provider)
		if await service.close_port(port=payload.port, shutdown_browser=payload.shutdown_browser):
			closed_by.append(provider)

	if not closed_by:
		body = ClosePortResponse(
			success=False,
			port=payload.port,
			closed_by=[],
			error_code='PORT_NOT_ACTIVE',
			error_message='No managed session found on the requested port.',
			details={'provider': payload.provider},
		)
		return JSONResponse(status_code=404, content=body.model_dump(mode='json'))

	return ClosePortResponse(success=True, port=payload.port, closed_by=closed_by)


@app.post('/v1/chat/gemini', response_model=ChatResponse)
async def chat_gemini(payload: ChatRequest):
	return await _dispatch_chat('gemini', payload)


@app.post('/v1/chat/gpt', response_model=ChatResponse)
async def chat_gpt(payload: ChatRequest):
	return await _dispatch_chat('gpt', payload)

@app.post('/v1/image/gemini', response_model=ImageResponse)
async def create_image_gemini(payload: ImageRequest):
	return await _dispatch_image('gemini', payload)

@app.post('/v1/image/gpt', response_model=ImageResponse)
async def create_image_gpt(payload: ImageRequest):
	return await _dispatch_image('gpt', payload)


@app.get('/v1/image/download/{file_name}')
async def download_image(file_name: str):
	try:
		image_path = _resolve_image_file_path(file_name)
	except AutomationError as error:
		raise HTTPException(status_code=error.status_code, detail=error.message) from error

	if not image_path.is_file():
		raise HTTPException(status_code=404, detail='Image file not found.')

	media_type = mimetypes.guess_type(str(image_path))[0] or 'application/octet-stream'
	return FileResponse(path=image_path, media_type=media_type, filename=image_path.name)


@app.post('/v1/image/clear', response_model=ClearImagesResponse)
async def clear_images(payload: ClearImagesRequest):
	storage_dir = _ensure_image_storage_dir()
	cleared_files = 0

	for candidate in storage_dir.iterdir():
		if not candidate.is_file():
			continue
		if payload.provider is not None and not candidate.name.startswith(f'{payload.provider}_'):
			continue
		try:
			candidate.unlink()
			cleared_files += 1
		except Exception:
			continue

	remaining = 0
	for candidate in storage_dir.iterdir():
		if not candidate.is_file():
			continue
		if payload.provider is not None and not candidate.name.startswith(f'{payload.provider}_'):
			continue
		remaining += 1

	return ClearImagesResponse(
		success=True,
		provider=payload.provider,
		cleared_files=cleared_files,
		remaining_files=remaining,
		folder=str(storage_dir),
	)


if __name__ == '__main__':
	import uvicorn

	uvicorn.run(
		app,
		host=os.getenv('GEMINI_API_HOST', '0.0.0.0'),
		port=int(os.getenv('GEMINI_API_PORT', '8008')),
		log_level=os.getenv('GEMINI_API_LOG_LEVEL', 'info'),
	)
