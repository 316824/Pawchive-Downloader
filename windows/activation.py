"""Offline device-bound invitation verification. No signing secrets ship here.

Local settings and open-source programs can be modified by the machine owner;
this is an invitation gate, not an unremovable DRM or identity-verification system.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

APP_ID = 'pw.pawchive.downloader.windows'
PUBLIC_KEY_SHA256 = '119e9d60aa5349bcfdc324698f698acce69bb7b95fc70e1e5e4f17cbd12f85c5'
ATTEMPT_LIMIT = 10
UNLOCK_BACKOFF = 24 * 60 * 60
TOKEN_KEY = 'activation_token'
FAILURES_KEY = 'activation_failures'
UNLOCK_FAILURES_KEY = 'activation_unlock_failures'
UNLOCK_UNTIL_KEY = 'activation_unlock_blocked_until'
UNLOCK_USED_KEY = 'activation_unlock_used_nonces'


class ActivationError(ValueError):
    def __init__(self, message, code='invalid_code'):
        super().__init__(message)
        self.code = code


def _b64decode(value, limit=1024):
    if not isinstance(value, str) or not value or len(value) > limit or not re.fullmatch(r'[A-Za-z0-9_-]+', value):
        raise ActivationError('邀请码格式不正确。', 'invalid_format')
    try:
        return base64.b64decode(value + '=' * (-len(value) % 4), altchars=b'-_', validate=True)
    except (ValueError, binascii.Error) as error:
        raise ActivationError('邀请码格式不正确。', 'invalid_format') from error


def normalize_device(value):
    if not isinstance(value, str):
        raise ActivationError('设备码格式不正确。', 'invalid_device')
    compact = re.sub(r'[\s-]', '', value).upper()
    if not re.fullmatch(r'PDW1[A-Z2-7]{32}', compact):
        raise ActivationError('设备码格式不正确。', 'invalid_device')
    body = compact[4:]
    return 'PDW1-' + '-'.join(body[i:i+8] for i in range(0, 32, 8))


def device_code(machine_guid=None):
    """Expose only a domain-separated installation hash, never the registry ID."""
    if machine_guid is None:
        if os.name != 'nt':
            raise ActivationError('设备绑定功能仅支持 Windows。', 'unsupported_platform')
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                    r'SOFTWARE\Microsoft\Cryptography', 0,
                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                machine_guid = winreg.QueryValueEx(key, 'MachineGuid')[0]
        except OSError as error:
            raise ActivationError('无法读取本机设备码，请检查系统权限。', 'device_unavailable') from error
    value = str(machine_guid).strip().lower()
    if len(value) < 8 or len(value) > 256:
        raise ActivationError('无法读取有效的设备标识。', 'device_unavailable')
    digest = hashlib.sha256((APP_ID + '\0device-v1\0' + value).encode('utf-8')).digest()[:20]
    return normalize_device('PDW1' + base64.b32encode(digest).decode('ascii'))


def load_public_key(path=None):
    try:
        path = Path(path) if path is not None else Path(__file__).with_name('activation_public.json')
        if path.stat().st_size > 4096:
            raise ValueError('oversized public-key manifest')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(manifest, dict) or manifest.get('schema') != 1 or manifest.get('app_id') != APP_ID:
            raise ValueError('wrong public-key manifest')
        raw = _b64decode(manifest['public_key_ed25519'], 128)
        digest = hashlib.sha256(raw).hexdigest()
        if len(raw) != 32 or digest != PUBLIC_KEY_SHA256 or manifest.get('key_id') != digest[:16]:
            raise ValueError('public-key fingerprint mismatch')
        return Ed25519PublicKey.from_public_bytes(raw)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ActivationError('授权验证文件不可用，请重新获取完整的软件包。', 'public_key_unavailable') from error


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate payload field')
        result[key] = value
    return result


def verify_token(token, device=None, public_key=None, *, purpose='activate'):
    if not isinstance(token, str) or len(token) > 2048:
        raise ActivationError('邀请码格式不正确。', 'invalid_format')
    pieces = token.strip().split('.')
    if len(pieces) != 3 or pieces[0] != 'PWI1':
        raise ActivationError('邀请码格式不正确。', 'invalid_format')
    payload, signature = _b64decode(pieces[1], 1024), _b64decode(pieces[2], 128)
    if len(payload) > 512 or len(signature) != 64:
        raise ActivationError('邀请码格式不正确。', 'invalid_format')
    public_key = public_key if public_key is not None else load_public_key()
    try:
        public_key.verify(signature, payload)
    except (InvalidSignature, ValueError) as error:
        raise ActivationError('邀请码无效或已被修改。', 'invalid_signature') from error
    try:
        claims = json.loads(payload, object_pairs_hook=_unique_object)
        if not isinstance(claims, dict) or set(claims) != {'v','app','device','purpose','nonce','issued'}:
            raise ValueError('unexpected payload fields')
        canonical = json.dumps(claims, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')
        if canonical != payload or type(claims['v']) is not int or claims['v'] != 1:
            raise ValueError('non-canonical payload')
        if claims['app'] != APP_ID or claims['purpose'] != purpose:
            raise ValueError('wrong token purpose')
        if type(claims['issued']) is not int or claims['issued'] < 0:
            raise ValueError('invalid issue time')
        if len(_b64decode(claims['nonce'], 64)) != 16:
            raise ValueError('invalid nonce')
        assigned = normalize_device(claims['device'])
    except (ValueError, TypeError, KeyError, UnicodeError) as error:
        raise ActivationError('邀请码内容不正确。', 'invalid_payload') from error
    expected = normalize_device(device if device is not None else device_code())
    if assigned != expected:
        raise ActivationError('该邀请码不属于此设备。', 'wrong_device')
    return claims


def _counter(store, key):
    value = store.get_setting(key, 0)
    return max(0, min(ATTEMPT_LIMIT, value)) if type(value) is int else ATTEMPT_LIMIT


def remaining_attempts(store):
    return ATTEMPT_LIMIT - _counter(store, FAILURES_KEY)


def is_activated(store, device=None):
    token = store.get_setting(TOKEN_KEY, '')
    if not token:
        return False
    try:
        verify_token(token, device=device)
        return True
    except ActivationError:
        return False


def activate(store, token, device=None):
    if is_activated(store, device):
        return verify_token(store.get_setting(TOKEN_KEY), device=device)
    if remaining_attempts(store) <= 0:
        raise ActivationError('尝试次数已用完，请向签发者申请设备解锁码。', 'activation_locked')
    # Operational key/device failures are not failed guesses.
    key = load_public_key()
    assigned = device if device is not None else device_code()
    try:
        claims = verify_token(token, assigned, key)
    except ActivationError:
        store.set_setting(FAILURES_KEY, _counter(store, FAILURES_KEY) + 1)
        raise
    store.set_setting(TOKEN_KEY, token.strip())
    return claims


def accept_unlock(store, token, device=None):
    now = int(time.time())
    blocked = store.get_setting(UNLOCK_UNTIL_KEY, 0)
    if type(blocked) is not int:
        raise ActivationError('解锁记录损坏，请联系签发者。', 'unlock_storage_error')
    if blocked > now:
        raise ActivationError('解锁尝试次数过多，请在24小时冷却结束后重试。', 'unlock_backoff')
    if blocked:
        store.set_setting(UNLOCK_FAILURES_KEY, 0)
        store.set_setting(UNLOCK_UNTIL_KEY, 0)
    key = load_public_key()
    assigned = device if device is not None else device_code()
    try:
        claims = verify_token(token, assigned, key, purpose='unlock')
        digest = hashlib.blake2b(claims['nonce'].encode('ascii'), digest_size=16).hexdigest()
        used = store.get_setting(UNLOCK_USED_KEY, [])
        if not isinstance(used, list) or any(not isinstance(item, str) for item in used):
            raise ActivationError('解锁记录损坏，请联系签发者。', 'unlock_storage_error')
        if digest in used:
            raise ActivationError('此解锁码已经使用过，请申请新的解锁码。', 'unlock_replayed')
    except ActivationError:
        failures = _counter(store, UNLOCK_FAILURES_KEY) + 1
        store.set_setting(UNLOCK_FAILURES_KEY, failures)
        if failures >= ATTEMPT_LIMIT:
            store.set_setting(UNLOCK_UNTIL_KEY, now + UNLOCK_BACKOFF)
        raise
    # Keep every compact nonce digest; pruning would re-enable old unlock codes.
    store.set_setting(UNLOCK_USED_KEY, used + [digest])
    store.set_setting(FAILURES_KEY, 0)
    store.set_setting(UNLOCK_FAILURES_KEY, 0)
    store.set_setting(UNLOCK_UNTIL_KEY, 0)
    return claims
