"""Resolve submitted video links into URLs Gemini can read directly.

Only small HTML pages may be fetched (for Loom metadata). Video bytes are
never downloaded or copied by this module.
"""
from html.parser import HTMLParser
from typing import Callable, Optional
from urllib.request import Request, urlopen

from .google_clients import extract_drive_file_id


class VideoResolutionError(ValueError):
    """A submitted link could not be resolved to video content."""


class _OpenGraphVideoParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.video_url: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "meta" or self.video_url:
            return
        values = {key.lower(): value for key, value in attrs if value is not None}
        if values.get("property", "").lower() in {
            "og:video",
            "og:video:url",
            "og:video:secure_url",
        }:
            self.video_url = values.get("content")


def _fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=15) as response:
        return response.read(2_000_000).decode(
            response.headers.get_content_charset() or "utf-8",
            errors="replace",
        )


def resolve_video_url(
    url: str,
    *,
    html_fetcher: Optional[Callable[[str], str]] = None,
) -> str:
    """Return a Gemini-readable URL without downloading video bytes."""
    url = (url or "").strip()
    if not url.startswith(("https://", "http://")):
        raise VideoResolutionError("Video link is missing or is not an HTTP URL.")

    if "drive.google.com" in url:
        file_id = extract_drive_file_id(url)
        if not file_id:
            raise VideoResolutionError("Could not extract a file ID from the Drive link.")
        return f"https://drive.google.com/uc?export=download&id={file_id}"

    if "loom.com/" in url:
        parser = _OpenGraphVideoParser()
        try:
            parser.feed((html_fetcher or _fetch_html)(url))
        except (OSError, ValueError) as exc:
            raise VideoResolutionError(f"Could not read Loom page: {exc}") from exc
        if not parser.video_url:
            raise VideoResolutionError("Loom page did not expose a public video URL.")
        return parser.video_url

    return url
