"""Resolve public video links without downloading video bodies.

The resolver understands web standards rather than a fixed provider catalog.
It reads response headers and a bounded amount of HTML metadata, while Gemini's
URL-context tool handles semantically ambiguous webpages inside the analyst.
"""

import ipaddress
import json
import mimetypes
import re
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


# These are Gemini's documented video inputs, not deployment-specific policy.
SUPPORTED_VIDEO_MIME_TYPES = frozenset(
    {
        "video/mp4",
        "video/mpeg",
        "video/mov",
        "video/avi",
        "video/x-flv",
        "video/mpg",
        "video/webm",
        "video/wmv",
        "video/3gpp",
    }
)
# Gemini documents a 100 MB ceiling for externally fetched HTTPS files.
MAX_EXTERNAL_VIDEO_BYTES = 100 * 1024 * 1024
# Bounded HTML protects memory while covering ordinary metadata-heavy pages.
MAX_METADATA_HTML_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
METADATA_TIMEOUT_SECONDS = 15
YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


class VideoResolutionError(ValueError):
    """A submitted link cannot be used safely as public video input."""


@dataclass(frozen=True)
class ResourceMetadata:
    """Headers and optional bounded HTML discovered without reading video bytes."""

    final_url: str
    content_type: str
    content_length: int | None
    html: str | None = None


@dataclass(frozen=True)
class ResolvedVideo:
    """A validated media reference shared by the analyst and grader-head.

    Exactly one of `uri`/`data` carries the actual media: Tier-1 (URL
    resolution, no download) always sets `uri`; Tier-2 (video_ingestion.py's
    downloads) sets `data` directly to in-memory bytes on Vertex AI, or `uri`
    to a Gemini Files API URI on the Developer API.
    """

    uri: str
    mime_type: str | None
    source: str
    requires_url_context: bool = False
    data: bytes | None = None
    # Size in bytes BEFORE any compression/transcoding — None when not
    # downloaded ourselves (Tier-1 URI references) or not tracked. Lets
    # callers recognize "this required heavy compression to fit Vertex's
    # inline limit" (video_ingestion.VERTEX_INLINE_VIDEO_MAX_BYTES) as a
    # signal that a quota/resource-exhausted error on this row is likely to
    # recur on retry rather than being genuinely transient — confirmed live:
    # a 398MB video compressed down to the inline limit still exhausted the
    # trial-tier quota when actually sent, in complete isolation from any
    # concurrency contention.
    original_size_bytes: int | None = None
    # Plain-text readout of any image-only deck slides (charts, financial
    # tables, screenshots — no selectable text). Populated only by
    # video_ingestion.py's ingest_pitch_deck, via one bundled Gemini call
    # over all such slides at once (see _read_chart_images). Empty for
    # video and for decks with no image-only slides.
    #
    # Why text, not raw images: Gemini reads these charts correctly when
    # given the image alone (confirmed live), but unreliably when the same
    # image is attached alongside the full deck/video/application text in
    # one big call — a documented "multimodal needle in a haystack"
    # weakness (arXiv:2406.11230): image-embedded retrieval degrades in
    # large multi-image context even though plain-text retrieval stays
    # reliable. Converting the chart to text up front sidesteps the weak
    # point entirely — the main analyst/grader/head calls only ever have to
    # do text-based cross-checking, which is the strong point.
    chart_text: str = ""


def _public_https_host(url: str) -> str:
    """Validate the URL boundary before the application makes any request."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    if parsed.scheme != "https" or not host or parsed.username or parsed.password:
        raise VideoResolutionError(
            "Video URLs must use public HTTPS without embedded credentials."
        )
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(host, 443)}
    except socket.gaierror as exc:
        raise VideoResolutionError(f"Video host could not be resolved: {host}.") from exc
    if not addresses or any(
        not ipaddress.ip_address(address).is_global for address in addresses
    ):
        raise VideoResolutionError(
            "Video host resolves to a private or non-public network address."
        )
    return host


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Validate every redirect hop and prevent unbounded redirect chains."""

    def __init__(self) -> None:
        super().__init__()
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirect_count += 1
        if self.redirect_count > MAX_REDIRECTS:
            raise VideoResolutionError("Video URL exceeded the redirect limit.")
        _public_https_host(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _normalized_content_type(raw_value: str | None, url: str) -> str:
    """Prefer authoritative headers, then infer a standard type from the path."""

    declared = (raw_value or "").split(";", 1)[0].strip().lower()
    if declared and declared != "application/octet-stream":
        return declared
    inferred, _encoding = mimetypes.guess_type(url)
    return (inferred or declared).lower()


def _content_length(raw_value: str | None) -> int | None:
    """Treat absent length as unknown while rejecting malformed negative values."""

    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise VideoResolutionError("Video server returned an invalid content length.") from exc
    if value < 0:
        raise VideoResolutionError("Video server returned a negative content length.")
    return value


def _request_metadata(url: str) -> ResourceMetadata:
    """Read headers and, only for HTML, a bounded page fragment."""

    _public_https_host(url)
    opener = build_opener(_SafeRedirectHandler())
    headers = {"User-Agent": "AI-Fellowship-Agent/1.0"}
    used_head = True
    # Every opener.open() below (the initial HEAD, the 403/405/501 GET
    # fallback, and the HTML-body GET) is network I/O and can raise
    # HTTPError/URLError/TimeoutError. All three call sites are wrapped in
    # this one try/except rather than individually, so a host that also
    # rejects the fallback GET (confirmed live: a Canva link blocking both
    # HEAD and GET with 403) degrades to an honest "video unavailable"
    # VideoResolutionError instead of an unhandled exception that crashes
    # the whole row in pipeline.py's process_row/run_batch.
    try:
        try:
            response = opener.open(
                Request(url, headers=headers, method="HEAD"),
                timeout=METADATA_TIMEOUT_SECONDS,
            )
        except HTTPError as exc:
            # Some otherwise valid media servers reject HEAD; GET still reads no video
            # bytes because the response is closed after headers unless it is HTML.
            if exc.code not in {403, 405, 501}:
                raise VideoResolutionError(f"Video metadata request failed: HTTP {exc.code}.") from exc
            used_head = False
            response = opener.open(
                Request(url, headers=headers, method="GET"),
                timeout=METADATA_TIMEOUT_SECONDS,
            )

        if used_head:
            preliminary_type = _normalized_content_type(
                response.headers.get("Content-Type"),
                response.geturl(),
            )
            if preliminary_type in {"text/html", "application/xhtml+xml"}:
                # HEAD has no page body, so replace it with one bounded HTML request.
                response.close()
                response = opener.open(
                    Request(url, headers=headers, method="GET"),
                    timeout=METADATA_TIMEOUT_SECONDS,
                )
    except VideoResolutionError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise VideoResolutionError(
            f"Video metadata request failed: {type(exc).__name__}."
        ) from exc

    with response:
        final_url = response.geturl()
        _public_https_host(final_url)
        content_type = _normalized_content_type(
            response.headers.get("Content-Type"),
            final_url,
        )
        length = _content_length(response.headers.get("Content-Length"))
        html = None
        if content_type in {"text/html", "application/xhtml+xml"}:
            # Reading page markup is metadata inspection, not video downloading.
            raw_html = response.read(MAX_METADATA_HTML_BYTES + 1)
            if len(raw_html) > MAX_METADATA_HTML_BYTES:
                raise VideoResolutionError("Video webpage metadata is too large.")
            html = raw_html.decode(
                response.headers.get_content_charset() or "utf-8",
                errors="replace",
            )
        return ResourceMetadata(final_url, content_type, length, html)


class _VideoMetadataParser(HTMLParser):
    """Collect standards-based video candidates from arbitrary webpages."""

    def __init__(self, page_url: str) -> None:
        super().__init__()
        self.page_url = page_url
        self.candidates: list[tuple[str, str | None, str]] = []
        self._json_ld = False
        self._json_ld_chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = {
            key.lower(): value
            for key, value in attrs
            if key and value is not None
        }
        tag = tag.lower()
        if tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if content and name in {
                "og:video",
                "og:video:url",
                "og:video:secure_url",
                "twitter:player:stream",
            }:
                self.candidates.append(
                    (urljoin(self.page_url, content), None, name)
                )
        elif tag in {"video", "source"} and values.get("src"):
            self.candidates.append(
                (
                    urljoin(self.page_url, values["src"]),
                    values.get("type"),
                    f"html:{tag}",
                )
            )
        elif (
            tag == "script"
            and values.get("type", "").lower() == "application/ld+json"
        ):
            self._json_ld = True
            self._json_ld_chunks = []

    def handle_data(self, data):
        if self._json_ld:
            self._json_ld_chunks.append(data)

    def handle_endtag(self, tag):
        if tag.lower() != "script" or not self._json_ld:
            return
        self._json_ld = False
        try:
            document = json.loads("".join(self._json_ld_chunks))
        except json.JSONDecodeError:
            return
        self._collect_json_ld(document)

    def _collect_json_ld(self, value):
        if isinstance(value, list):
            for item in value:
                self._collect_json_ld(item)
            return
        if not isinstance(value, dict):
            return
        graph = value.get("@graph")
        if graph is not None:
            self._collect_json_ld(graph)
        content_url = value.get("contentUrl")
        if isinstance(content_url, str):
            self.candidates.append(
                (urljoin(self.page_url, content_url), None, "jsonld:contentUrl")
            )


def _validate_direct_video(
    metadata: ResourceMetadata,
    declared_mime: str | None = None,
) -> ResolvedVideo | None:
    """Return only media Gemini can fetch within its documented external limit."""

    if metadata.content_type in SUPPORTED_VIDEO_MIME_TYPES:
        mime_type = metadata.content_type
    elif metadata.content_type in {"", "application/octet-stream"}:
        mime_type = _normalized_content_type(declared_mime, metadata.final_url)
    else:
        return None
    if mime_type not in SUPPORTED_VIDEO_MIME_TYPES:
        return None
    if (
        metadata.content_length is not None
        and metadata.content_length > MAX_EXTERNAL_VIDEO_BYTES
    ):
        raise VideoResolutionError("Video exceeds Gemini's 100 MB external URL limit.")
    return ResolvedVideo(
        uri=metadata.final_url,
        mime_type=mime_type,
        source="direct_video",
    )


def _youtube_video_id(url: str) -> str | None:
    """Extract a video ID from public YouTube shapes supported by Gemini."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").lower()
    path = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be":
        if len(path) == 1 and YOUTUBE_VIDEO_ID.fullmatch(path[0]):
            return path[0]
        return None
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
        return None
    if parsed.path == "/watch":
        video_ids = parse_qs(
            parsed.query,
            keep_blank_values=True,
        ).get("v", [])
        if len(video_ids) == 1 and YOUTUBE_VIDEO_ID.fullmatch(video_ids[0]):
            return video_ids[0]
        return None
    if (
        len(path) == 2
        and path[0] in {"shorts", "embed", "live"}
        and YOUTUBE_VIDEO_ID.fullmatch(path[1])
    ):
        return path[1]
    return None


def canonicalize_youtube_url(url: str) -> str:
    """Strip query noise that can make Gemini reject an otherwise valid video."""
    submitted_url = (url or "").strip()
    video_id = _youtube_video_id(submitted_url)
    if video_id is None:
        return submitted_url
    return f"https://www.youtube.com/watch?v={video_id}"


def _is_native_youtube_video(url: str) -> bool:
    """Identify public YouTube video shapes supported natively by Gemini."""
    return _youtube_video_id(url) is not None


def resolve_video_url(
    url: str,
    *,
    metadata_fetcher: Callable[[str], ResourceMetadata] = _request_metadata,
) -> ResolvedVideo:
    """Resolve any standards-compliant public video link without video download."""

    submitted_url = (url or "").strip()
    _public_https_host(submitted_url)
    if _is_native_youtube_video(submitted_url):
        # Google's native YouTube input explicitly omits MIME. Query noise is
        # removed because a dangling timestamp (for example, trailing ``&t``)
        # makes Vertex report LOGIN_REQUIRED even though the video is public.
        return ResolvedVideo(
            canonicalize_youtube_url(submitted_url),
            None,
            "youtube",
        )

    page_or_media = metadata_fetcher(submitted_url)
    direct = _validate_direct_video(page_or_media)
    if direct is not None:
        return direct

    if page_or_media.html:
        parser = _VideoMetadataParser(page_or_media.final_url)
        parser.feed(page_or_media.html)
        # Declared video types are tried before undeclared ones to minimize
        # outbound metadata probes.
        candidates = sorted(
            dict.fromkeys(parser.candidates),
            key=lambda item: (
                item[1] not in SUPPORTED_VIDEO_MIME_TYPES,
                _normalized_content_type(item[1], item[0])
                not in SUPPORTED_VIDEO_MIME_TYPES,
            ),
        )
        for candidate_url, declared_mime, source in candidates[:30]:
            try:
                _public_https_host(candidate_url)
                candidate_metadata = metadata_fetcher(candidate_url)
            except (OSError, VideoResolutionError):
                continue
            candidate = _validate_direct_video(candidate_metadata, declared_mime)
            if candidate is not None:
                return ResolvedVideo(
                    candidate.uri,
                    candidate.mime_type,
                    source,
                )

    # The analyst uses URL context to interpret non-standard or JS-rendered pages.
    return ResolvedVideo(
        uri=page_or_media.final_url,
        mime_type=None,
        source="webpage",
        requires_url_context=True,
    )
