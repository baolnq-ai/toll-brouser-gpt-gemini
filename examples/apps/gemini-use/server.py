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
import json
import logging
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from browser_use import BrowserProfile, BrowserSession
from browser_use.browser.events import SwitchTabEvent

ChatProvider = Literal['gemini', 'gpt']
ChatMode = Literal['fast', 'reasoning', 'pro']


def _to_bool(value: str | None, default: bool = False) -> bool:
	if value is None:
		return default
	return value.strip().lower() in {'1', 'true', 'yes', 'on'}


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


@dataclass
class ServiceConfig:
	provider: ChatProvider
	display_name: str
	cdp_url: str
	tab_hosts: tuple[str, ...]
	default_timeout_s: float
	poll_interval_s: float
	stable_polls: int
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
			tab_hosts=hosts,
			default_timeout_s=_to_float(os.getenv(f'{prefix}_DEFAULT_TIMEOUT_S', os.getenv('GEMINI_DEFAULT_TIMEOUT_S')), 240.0),
			poll_interval_s=_to_float(os.getenv(f'{prefix}_POLL_INTERVAL_S', os.getenv('GEMINI_POLL_INTERVAL_S')), 1.0),
			stable_polls=max(2, _to_int(os.getenv(f'{prefix}_STABLE_POLLS', os.getenv('GEMINI_STABLE_POLLS')), 3)),
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
	prompt: str = Field(min_length=1, max_length=16000)
	mode: ChatMode | None = None
	timeout_s: float | None = Field(default=None, ge=10.0, le=600.0)


class ChatResponse(BaseModel):
	success: bool
	request_id: str
	provider: ChatProvider
	mode_requested: ChatMode | None = None
	mode_applied: bool | None = None
	answer: str | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


class ImageRequest(BaseModel):
	prompt: str = Field(min_length=1, max_length=16000)
	timeout_s: float | None = Field(default=None, ge=20.0, le=600.0)
	max_images: int = Field(default=4, ge=1, le=4)


class GeneratedImage(BaseModel):
	file_name: str
	content_type: str
	byte_size: int
	base64_data: str
	source_url: str | None = None
	width: int | None = None
	height: int | None = None


class ImageResponse(BaseModel):
	success: bool
	request_id: str
	provider: ChatProvider
	images: list[GeneratedImage] | None = None
	error_code: str | None = None
	error_message: str | None = None
	details: dict[str, Any] | None = None
	elapsed_ms: int


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
		'textarea[aria-label*="message" i]',
		'textarea[placeholder*="message" i]',
		'div[contenteditable="true"][role="textbox"]',
		'div[contenteditable="true"][aria-label*="message" i]',
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
		'button[aria-label*="send" i]',
		'button[data-testid*="send" i]',
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
	const stopButton = allButtons.find((btn) => {
		const t = norm(textOf(btn) + ' ' + (btn.getAttribute('aria-label') || ''));
		return t.includes('stop generating') || t.includes('stop response');
	});

	const responseSelectors = [
		'[data-message-author-role="assistant"]',
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
		'textarea[aria-label*="message" i]',
		'textarea[placeholder*="message" i]',
		'div[contenteditable="true"][role="textbox"]',
		'div[contenteditable="true"][aria-label*="message" i]',
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

	const sendButton = Array.from(document.querySelectorAll('button[aria-label*="send" i], button[data-testid*="send" i], button[type="submit"]'))
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
	const stopButton = allButtons.find((btn) => {
		const t = norm(textOf(btn) + ' ' + (btn.getAttribute('aria-label') || ''));
		return t.includes('stop generating') || t.includes('stop response');
	});

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
				const closeButton = Array.from(document.querySelectorAll('button,[role="button"]')).find((el) => ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '')).toLowerCase().includes('close'));
				if (closeButton) closeButton.click();
				return {{ ok: true, dataUrl, sourceUrl: preview.currentSrc || preview.src || sourceUrl, contentType: 'image/png', width: preview.naturalWidth, height: preview.naturalHeight, buttonIndex: candidate.buttonIndex }};
			}} catch (error) {{
				return {{ ok: false, error: 'preview-canvas-failed', reason: String(error), sourceUrl }};
			}}
		}}
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
	def __init__(self, cfg: ServiceConfig, *, request_lock: asyncio.Lock):
		self.cfg = cfg
		self.logger = logging.getLogger(f'{cfg.provider}_bridge')
		self._session: BrowserSession | None = None
		self._request_lock = request_lock
		self._startup_lock = asyncio.Lock()

	async def startup(self) -> None:
		async with self._startup_lock:
			await self._ensure_session(force_reconnect=True)

	async def shutdown(self) -> None:
		if self._session is None:
			return
		try:
			await self._session.stop()
		except Exception as e:
			self.logger.warning('failed to stop browser session: %s', e)
		finally:
			self._session = None

	async def ask(
		self,
		*,
		request_id: str,
		prompt: str,
		mode: ChatMode | None,
		timeout_s: float | None,
	) -> ChatResponse:
		started = time.time()

		if len(prompt) > self.cfg.max_prompt_len:
			raise AutomationError(
				'PROMPT_TOO_LONG',
				f'Prompt exceeds max length ({self.cfg.max_prompt_len}).',
				status_code=422,
				details={'max_prompt_len': self.cfg.max_prompt_len},
			)

		effective_timeout = timeout_s if timeout_s is not None else self.cfg.default_timeout_s
		mode_applied: bool | None = None

		async with self._request_lock:
			session = await self._ensure_session(force_reconnect=False)
			tab = await self._switch_to_gemini_tab(session)

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
					'Failed to write prompt into Gemini composer.',
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
						'Prompt did not persist in Gemini composer after input attempt.',
						status_code=502,
						details={'send_result': send_result, 'post_type': post_type},
					)

			submit_result = await self._run_js(session, CLICK_SEND_BUTTON_JS)
			if not isinstance(submit_result, dict) or not submit_result.get('ok'):
				raise AutomationError(
					'PROMPT_SUBMIT_FAILED',
					'Failed to trigger Gemini prompt submission.',
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
						'Prompt remained in Gemini composer after submission attempt.',
						status_code=502,
						details={'send_result': send_result, 'retry_send': retry_send, 'post_send': post_send},
					)

			answer = await self._wait_for_answer(
				session=session,
				baseline_count=int(baseline.get('responseCount') or 0),
				baseline_last=str(baseline.get('lastResponseText') or ''),
				timeout_s=effective_timeout,
			)

		elapsed_ms = int((time.time() - started) * 1000)
		return ChatResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			mode_requested=mode,
			mode_applied=mode_applied,
			answer=answer,
			elapsed_ms=elapsed_ms,
		)

	async def create_image(
		self,
		*,
		request_id: str,
		prompt: str,
		timeout_s: float | None,
		max_images: int,
	) -> ImageResponse:
		started = time.time()

		if len(prompt) > self.cfg.max_prompt_len:
			raise AutomationError(
				'PROMPT_TOO_LONG',
				f'Prompt exceeds max length ({self.cfg.max_prompt_len}).',
				status_code=422,
				details={'max_prompt_len': self.cfg.max_prompt_len},
			)

		effective_timeout = timeout_s if timeout_s is not None else max(self.cfg.default_timeout_s, 240.0)

		async with self._request_lock:
			session = await self._ensure_session(force_reconnect=False)
			tab = await self._switch_to_gemini_tab(session)

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
				timeout_s=effective_timeout,
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

		elapsed_ms = int((time.time() - started) * 1000)
		return ImageResponse(
			success=True,
			request_id=request_id,
			provider=self.cfg.provider,
			images=images,
			elapsed_ms=elapsed_ms,
		)

	async def _ensure_session(self, *, force_reconnect: bool) -> BrowserSession:
		if self._session is not None and self._session.is_cdp_connected and not force_reconnect:
			return self._session

		if self._session is not None:
			try:
				await self._session.stop()
			except Exception:
				pass

		profile = BrowserProfile(
			cdp_url=self.cfg.cdp_url,
			is_local=False,
			keep_alive=True,
			highlight_elements=False,
		)
		session = BrowserSession(browser_profile=profile)

		try:
			await session.start()
		except Exception as e:
			raise AutomationError(
				'CDP_CONNECT_FAILED',
				f'Cannot connect to Chrome CDP at {self.cfg.cdp_url}. Start Chrome with --remote-debugging-port.',
				status_code=503,
				details={'cdp_url': self.cfg.cdp_url, 'reason': str(e)},
			) from e

		self._session = session
		return session

	async def _switch_to_gemini_tab(self, session: BrowserSession):
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

		if selected.target_id != current_target:
			event = session.event_bus.dispatch(SwitchTabEvent(target_id=selected.target_id))
			await event
			await event.event_result(raise_if_any=True, raise_if_none=False)

		return selected

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
		stable_count = 0
		last_seen_signature = ''
		saw_new_response = False
		transient_eval_timeouts = 0
		saw_streaming = False
		last_streaming_at = 0.0
		best_answer = ''
		best_signature = ''
		baseline_norm = baseline_last.strip()

		while True:
			now = time.time()
			if now > deadline:
				if not (saw_streaming and last_streaming_at > 0 and now <= last_streaming_at + STREAM_SETTLE_GRACE_SECONDS):
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
			if candidate_text and (len(candidate_text) >= len(best_answer) or candidate_signature != best_signature):
				best_answer = candidate_text
				best_signature = candidate_signature

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
				if stable_count >= self.cfg.stable_polls and not is_streaming:
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
		stable_count = 0
		last_signature = ''
		transient_eval_timeouts = 0
		last_candidates: list[dict[str, Any]] = []
		baseline_keys = {self._candidate_key(candidate) for candidate in baseline_candidates}
		last_streaming_at = 0.0
		saw_streaming = False

		while True:
			now = time.time()
			if now > deadline:
				if not (saw_streaming and last_streaming_at > 0 and now <= last_streaming_at + STREAM_SETTLE_GRACE_SECONDS):
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
		file_name = f'{self.cfg.provider}_{request_id}_{index + 1}{ext}'
		base64_data = base64.b64encode(content).decode('ascii')

		return GeneratedImage(
			file_name=file_name,
			content_type=content_type,
			byte_size=len(content),
			base64_data=base64_data,
			source_url=source_url,
			width=_to_int(str(payload.get('width')) if payload.get('width') is not None else None, 0) or None,
			height=_to_int(str(payload.get('height')) if payload.get('height') is not None else None, 0) or None,
		)

	def _download_binary_source(self, source_url: str) -> bytes:
		request = Request(source_url, headers={'User-Agent': 'Mozilla/5.0'})
		with urlopen(request, timeout=60) as response:
			return response.read()

	async def _run_js(self, session: BrowserSession, expression: str) -> Any:
		active_session = self._session if self._session is not None else session
		last_error: Exception | None = None

		for attempt in range(2):
			try:
				cdp_session = await active_session.get_or_create_cdp_session(focus=True)
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
					active_session = await self._ensure_session(force_reconnect=True)
					await self._switch_to_gemini_tab(active_session)
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


SHARED_REQUEST_LOCK = asyncio.Lock()

GEMINI_CFG = ServiceConfig.from_env(
	provider='gemini',
	display_name='Gemini',
	default_hosts='gemini.google.com',
	supports_mode=True,
)
GPT_CFG = ServiceConfig.from_env(
	provider='gpt',
	display_name='ChatGPT',
	default_hosts='chatgpt.com,chat.openai.com',
	supports_mode=False,
)

GEMINI_SERVICE = GeminiBridgeService(GEMINI_CFG, request_lock=SHARED_REQUEST_LOCK)
GPT_SERVICE = GeminiBridgeService(GPT_CFG, request_lock=SHARED_REQUEST_LOCK)


def _get_service(provider: ChatProvider) -> GeminiBridgeService:
	return GPT_SERVICE if provider == 'gpt' else GEMINI_SERVICE


@asynccontextmanager
async def lifespan(app: FastAPI):
	await GEMINI_SERVICE.startup()
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
	request_id = str(uuid4())
	started = time.time()

	try:
		response = await service.ask(
			request_id=request_id,
			prompt=payload.prompt,
			mode=payload.mode,
			timeout_s=payload.timeout_s,
		)
		return response
	except AutomationError as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = ChatResponse(
			success=False,
			request_id=request_id,
			provider=provider,
			mode_requested=payload.mode,
			mode_applied=None,
			answer=None,
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
			answer=None,
			error_code='UNHANDLED_ERROR',
			error_message='Unhandled server error.',
			details={'reason': str(e)},
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=500, content=body.model_dump(mode='json'))

async def _dispatch_image(provider: ChatProvider, payload: ImageRequest):
	service = _get_service(provider)
	request_id = str(uuid4())
	started = time.time()

	try:
		response = await service.create_image(
			request_id=request_id,
			prompt=payload.prompt,
			timeout_s=payload.timeout_s,
			max_images=payload.max_images,
		)
		return response
	except AutomationError as e:
		elapsed_ms = int((time.time() - started) * 1000)
		body = ImageResponse(
			success=False,
			request_id=request_id,
			provider=provider,
			images=None,
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
			images=None,
			error_code='UNHANDLED_ERROR',
			error_message='Unhandled server error.',
			details={'reason': str(e)},
			elapsed_ms=elapsed_ms,
		)
		return JSONResponse(status_code=500, content=body.model_dump(mode='json'))
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


if __name__ == '__main__':
	import uvicorn

	uvicorn.run(
		app,
		host=os.getenv('GEMINI_API_HOST', '0.0.0.0'),
		port=int(os.getenv('GEMINI_API_PORT', '8008')),
		log_level=os.getenv('GEMINI_API_LOG_LEVEL', 'info'),
	)
