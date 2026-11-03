"""Canonical Pawchive profile/post link extraction shared by Windows features."""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit, urlunsplit


SUPPORTED_SECTIONS = frozenset({'fanbox', 'patreon', 'subscribestar', 'discord', 'boosty'})
PROFILE_PATH_RE = re.compile(
    r'^/(?P<section>fanbox|patreon|subscribestar|discord|boosty)/user/(?P<user>[^/?#]+)'
    r'(?:/post/(?P<post>[^/?#]+))?/?$', re.IGNORECASE)
URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
TRAILING_PUNCTUATION = '.,!?;:)]}>\'"'


@dataclass(frozen=True)
class LinkInfo:
    """A canonical profile URL and an optional post URL from the same creator."""

    profile_url: str
    post_url: str | None = None

    @property
    def is_post(self):
        return bool(self.post_url)


def _candidate_urls(text):
    if not isinstance(text, str):
        return ()
    return tuple(match.group(0).rstrip(TRAILING_PUNCTUATION) for match in URL_RE.finditer(text))


def parse_url(value):
    """Return a canonical :class:`LinkInfo` for one URL, or ``None``."""
    if not isinstance(value, str):
        return None
    value = value.strip().rstrip(TRAILING_PUNCTUATION)
    if not value:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme.lower() != 'https' or (parsed.username or parsed.password):
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = (parsed.hostname or '').lower().rstrip('.')
    if host not in {'pawchive.pw', 'www.pawchive.pw'}:
        return None
    match = PROFILE_PATH_RE.fullmatch(parsed.path)
    if match is None:
        return None
    section = match.group('section').lower()
    user = match.group('user')
    profile = f'https://pawchive.pw/{section}/user/{user}'
    post_id = match.group('post')
    post = f'{profile}/post/{post_id}' if post_id else None
    return LinkInfo(profile_url=profile, post_url=post)


def extract_links(text):
    """Extract unique canonical links in appearance order."""
    result = []
    seen = set()
    for candidate in _candidate_urls(text):
        info = parse_url(candidate)
        if info is None:
            continue
        key = info.post_url or info.profile_url
        if key in seen:
            continue
        seen.add(key)
        result.append(info)
    return tuple(result)


def extract_link(text):
    """Extract the first supported profile or post link from arbitrary text."""
    links = extract_links(text)
    return links[0] if links else None


def extract_profile_url(text):
    """Return the first canonical creator profile URL in arbitrary text."""
    info = extract_link(text)
    return info.profile_url if info else None


__all__ = ['LinkInfo', 'SUPPORTED_SECTIONS', 'extract_link', 'extract_links',
           'extract_profile_url', 'parse_url']
