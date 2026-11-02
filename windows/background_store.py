"""Safe, per-window background metadata and thumbnail storage.

The original image is never copied into the application state directory.  A
user-selected source path and a normalized crop rectangle are recorded in a
small JSON document; the UI writes a bounded, display-ready thumbnail beside
that document.  Runtime pages only load those thumbnails, which keeps page
switches predictable even when the source image is very large.
"""
from __future__ import annotations

from collections import OrderedDict
import json
import math
import os
from pathlib import Path
import tempfile
import threading
import weakref


BACKGROUND_PAGES = (
    'workspace',
    'calendar',
    'schedules',
    'activation',
    'picker',
    'close_choice',
    'resume_choice',
    'scan',
)
BACKGROUND_MODES = ('default', 'custom', 'reward')
ALLOWED_IMAGE_SUFFIXES = frozenset({'.jpg', '.jpeg', '.png', '.webp'})
MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_SOURCE_PIXELS = 24_000_000
MAX_SOURCE_DIMENSION = 10_000
MAX_PREVIEW_BYTES = 4 * 1024 * 1024
MAX_PREVIEW_DIMENSION = 2_048
DEFAULT_OVERLAY = 56
MIN_OVERLAY = 15
MAX_OVERLAY = 85


class BackgroundError(ValueError):
    """A user-facing background configuration or image validation error."""


def _finite_number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def normalize_page(page):
    page = str(page or '').strip().lower()
    if page not in BACKGROUND_PAGES:
        raise BackgroundError('Unknown background page.')
    return page


def validate_source_path(path):
    """Validate only cheap filesystem properties before Qt decodes an image."""
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as error:
        raise BackgroundError('The selected image could not be opened.') from error
    if not resolved.is_file():
        raise BackgroundError('Choose an image file, not a folder.')
    if resolved.suffix.lower() not in ALLOWED_IMAGE_SUFFIXES:
        raise BackgroundError('Only JPG, JPEG, PNG and WEBP images are supported.')
    try:
        size = resolved.stat().st_size
    except OSError as error:
        raise BackgroundError('The selected image size could not be read.') from error
    if size <= 0 or size > MAX_SOURCE_BYTES:
        raise BackgroundError('The image must be between 1 byte and 20 MB.')
    return resolved, size


def normalize_crop(crop):
    """Return a safe normalized crop rectangle with coordinates in [0, 1]."""
    if not isinstance(crop, dict):
        crop = {}
    x = min(1.0, max(0.0, _finite_number(crop.get('x'))))
    y = min(1.0, max(0.0, _finite_number(crop.get('y'))))
    width = min(1.0 - x, max(0.0001, _finite_number(crop.get('width'), 1.0)))
    height = min(1.0 - y, max(0.0001, _finite_number(crop.get('height'), 1.0)))
    return {'x': round(x, 6), 'y': round(y, 6),
            'width': round(width, 6), 'height': round(height, 6)}


def normalize_overlay(value):
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        value = DEFAULT_OVERLAY
    return max(MIN_OVERLAY, min(MAX_OVERLAY, value))


def _safe_relative_preview(root, value):
    if not isinstance(value, str) or not value:
        return None
    try:
        path = (root / value).resolve()
        if not path.is_relative_to(root.resolve()):
            return None
    except (OSError, RuntimeError, TypeError):
        return None
    return path


class BackgroundStore:
    """Persist background metadata and retain a small in-memory thumbnail pool."""

    def __init__(self, data_dir, *, cache_limit=3):
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.root = self.data_dir / 'backgrounds'
        self.root.mkdir(parents=True, exist_ok=True)
        self.json_path = self.data_dir / 'backgrounds.json'
        self.cache_limit = max(1, int(cache_limit))
        self._lock = threading.RLock()
        self._cache = OrderedDict()
        self._layers = {}
        self._data = self._load()

    def _empty(self):
        return {'version': 1, 'mode': 'default', 'pages': {}}

    def _load(self):
        try:
            raw = json.loads(self.json_path.read_text(encoding='utf-8'))
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return self._empty()
        if not isinstance(raw, dict):
            return self._empty()
        mode = raw.get('mode') if raw.get('mode') in BACKGROUND_MODES else 'default'
        pages = {}
        candidate_pages = raw.get('pages', {})
        if isinstance(candidate_pages, dict):
            for page, value in candidate_pages.items():
                try:
                    page = normalize_page(page)
                except BackgroundError:
                    continue
                if not isinstance(value, dict):
                    continue
                preview = _safe_relative_preview(self.data_dir, value.get('preview'))
                if preview is None or not preview.is_file():
                    continue
                crop = normalize_crop(value.get('crop'))
                source = value.get('source')
                source = str(source)[:4096] if isinstance(source, str) else ''
                source_size = value.get('source_size', [0, 0])
                preview_size = value.get('preview_size', [0, 0])
                if not isinstance(source_size, list) or len(source_size) != 2:
                    source_size = [0, 0]
                if not isinstance(preview_size, list) or len(preview_size) != 2:
                    preview_size = [0, 0]
                pages[page] = {
                    'source': source,
                    'preview': str(preview.relative_to(self.data_dir)),
                    'crop': crop,
                    'source_size': [max(0, int(_finite_number(item))) for item in source_size],
                    'preview_size': [max(0, int(_finite_number(item))) for item in preview_size],
                    'aspect': max(0.0001, _finite_number(value.get('aspect'), 1.0)),
                    'overlay': normalize_overlay(value.get('overlay', DEFAULT_OVERLAY)),
                }
        return {'version': 1, 'mode': mode, 'pages': pages}

    def _write(self):
        payload = json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True)
        if len(payload.encode('utf-8')) > 512 * 1024:
            raise BackgroundError('Background settings are unexpectedly large.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=self.data_dir,
                                             prefix='.backgrounds-', suffix='.tmp',
                                             delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.json_path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def mode(self):
        with self._lock:
            return self._data['mode']

    def set_mode(self, mode):
        mode = str(mode)
        if mode not in BACKGROUND_MODES:
            raise BackgroundError('Unknown background mode.')
        with self._lock:
            self._data['mode'] = mode
            self._write()
        return mode

    def page_config(self, page):
        page = normalize_page(page)
        with self._lock:
            value = self._data['pages'].get(page)
            return dict(value) if value else None

    def pages_with_background(self):
        with self._lock:
            return tuple(sorted(self._data['pages']))

    def has_custom(self):
        with self._lock:
            return bool(self._data['pages'])

    def set_overlay(self, page, value):
        page = normalize_page(page)
        with self._lock:
            config = self._data['pages'].get(page)
            if config is None:
                return normalize_overlay(value)
            config['overlay'] = normalize_overlay(value)
            self._write()
            return config['overlay']

    def save_page(self, page, *, source, crop, preview_bytes, source_size, preview_size, aspect):
        """Atomically save a bounded JPEG/PNG/WebP preview and its metadata."""
        page = normalize_page(page)
        source, source_bytes = validate_source_path(source)
        if not isinstance(preview_bytes, (bytes, bytearray)):
            raise BackgroundError('The preview image is invalid.')
        preview_bytes = bytes(preview_bytes)
        if not preview_bytes or len(preview_bytes) > MAX_PREVIEW_BYTES:
            raise BackgroundError('The generated preview is too large.')
        try:
            source_width, source_height = (int(source_size[0]), int(source_size[1]))
            preview_width, preview_height = (int(preview_size[0]), int(preview_size[1]))
        except (TypeError, ValueError, IndexError):
            raise BackgroundError('The image dimensions are invalid.')
        if not (1 <= source_width <= MAX_SOURCE_DIMENSION and
                1 <= source_height <= MAX_SOURCE_DIMENSION and
                source_width * source_height <= MAX_SOURCE_PIXELS):
            raise BackgroundError('The image dimensions exceed the safe limit.')
        if not (1 <= preview_width <= MAX_PREVIEW_DIMENSION and
                1 <= preview_height <= MAX_PREVIEW_DIMENSION):
            raise BackgroundError('The preview dimensions exceed the safe limit.')
        preview_path = self.root / f'{page}.jpg'
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('wb', dir=self.root, prefix=f'.{page}-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(preview_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, preview_path)
        except OSError as error:
            raise BackgroundError('The preview could not be saved.') from error
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        with self._lock:
            self._data['pages'][page] = {
                'source': str(source),
                'source_bytes': source_bytes,
                'preview': str(preview_path.relative_to(self.data_dir)),
                'crop': normalize_crop(crop),
                'source_size': [source_width, source_height],
                'preview_size': [preview_width, preview_height],
                'aspect': max(0.0001, _finite_number(aspect, preview_width / preview_height)),
                'overlay': normalize_overlay(self._data['pages'].get(page, {}).get('overlay', DEFAULT_OVERLAY)),
            }
            self._cache.pop(page, None)
            self._write()
        return self.page_config(page)

    def remove_page(self, page):
        page = normalize_page(page)
        with self._lock:
            old = self._data['pages'].pop(page, None)
            self._cache.pop(page, None)
            if old:
                preview = _safe_relative_preview(self.data_dir, old.get('preview'))
                if preview is not None:
                    preview.unlink(missing_ok=True)
            self._write()
            return bool(old)

    def preview_path(self, page):
        config = self.page_config(page)
        if not config:
            return None
        path = _safe_relative_preview(self.data_dir, config.get('preview'))
        return path if path is not None and path.is_file() else None

    def cached_preview(self, page, loader):
        """Return a UI-owned preview object, loading it at most once per page.

        ``loader`` is supplied by the Qt layer so this module remains usable in
        state-only tests.  The cache is an LRU pool bounded by ``cache_limit``.
        """
        page = normalize_page(page)
        with self._lock:
            if page in self._cache:
                value = self._cache.pop(page)
                self._cache[page] = value
                return value
            path = self.preview_path(page)
        if path is None:
            return None
        value = loader(path)
        if value is None:
            return None
        with self._lock:
            self._cache[page] = value
            while len(self._cache) > self.cache_limit:
                self._cache.popitem(last=False)
        return value

    def release_except(self, page=None):
        page = normalize_page(page) if page is not None else None
        with self._lock:
            for key in list(self._cache):
                if key != page:
                    self._cache.pop(key, None)

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    def register_layer(self, page, layer):
        """Register a live Qt layer so inactive pages can release their pixmap."""
        page = normalize_page(page)
        with self._lock:
            self._layers[page] = weakref.ref(layer)

    def activate_page(self, page):
        """Keep only the active page's rendered pixmap and thumbnail cache."""
        page = normalize_page(page)
        with self._lock:
            for key, reference in list(self._layers.items()):
                layer = reference()
                if layer is None:
                    self._layers.pop(key, None)
                elif key != page:
                    try:
                        layer.clear_background()
                    except RuntimeError:
                        self._layers.pop(key, None)
            self.release_except(page)

