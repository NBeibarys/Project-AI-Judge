"""Tier-2 media ingestion: download video and pitch-deck sources that
video_urls.py cannot resolve to a Gemini-fetchable URI, then hand the
bytes to Gemini so the agents receive the media as a native multimodal
Part. Used by every program (R2B, Fellowship V2, Alchemist).

Scope of this module:
  - Google Drive share links (download via Drive API)

What stays on the Tier-1 resolver (video_urls.resolve_video_url) and is
never downloaded here:
  - Public YouTube URLs (native Gemini input, no download)
  - Direct HTTPS video ≤100MB with a known MIME

A URL with no discoverable direct video (not YouTube, no direct video
file, no video metadata on the page, e.g. a Canva/Loom share page) is a
genuine resolution failure, not a fallback source — resolve_video_url
raises instead of returning a "webpage" reference. There used to be a
url_context-based fallback here (having the analyst read the page live);
it was removed after causing a real production failure (a 400 from its
own ~15MB fetch cap on a webpage source) and because a page's HTML was
never a reliable substitute for actual pitch video content. The caller
now routes rows with an unresolvable-but-submitted video link to human
review instead.

Auth: the Drive download reuses the same service-account JSON as Sheets,
but with the drive.readonly scope. The Drive file MUST be shared with the
service-account email (the same one the Sheet is shared with). If it is
not, the download 404s and the caller treats it as "no video" — criterion 6
(Presentation & Clarity) then scores 1 with rationale "No video submitted."

Backend: downloads are held entirely in memory, never written to disk.
  - On Vertex AI: the Files API isn't available, so ResolvedMedia.data
    carries the raw bytes directly for inline Part.from_bytes.
  - On the Developer API (API key): bytes are streamed straight into the
    Gemini Files API via an in-memory buffer; ResolvedMedia.uri carries the
    resulting file URI. The Files API auto-expires objects after 48h, so no
    explicit cleanup is needed there either.
Holding bytes in memory instead of a temp file avoids a pointless
write-then-read-back round trip, and means there's nothing left on disk to
clean up after a row finishes.

Requires the ffmpeg and ffprobe binaries on PATH (transcode to MP4,
duration probing, size-targeted recompression); they are invoked as
subprocesses and are not Python dependencies.
"""
import io
import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urlparse
from urllib.request import Request

from .google_clients import extract_drive_file_id, is_drive_folder_url
from .video_urls import (
    USER_AGENT,
    ResolvedMedia,
    VideoResolutionError,
    _public_https_host,
    resolve_video_url,
    safe_opener,
)

# Gemini Files API: 2GB per file (paid tier 20GB). We cap downloads at 2GB
# to avoid downloading a file we then can't upload.
FILES_API_MAX_BYTES = 2 * 1024 * 1024 * 1024
# Vertex AI inline Part.from_bytes PDF limit — a fixed platform limit, not
# an adjustable quota (Google's own docs: ~50MB for PDFs specifically, vs
# ~100MB general inline cap). A 90MB real deck confirmed failing against
# this. Kept with a safety margin below the documented ceiling.
VERTEX_INLINE_PDF_MAX_BYTES = 45 * 1024 * 1024
# Vertex AI inline Part.from_bytes video limit — same kind of fixed
# platform limit, general ~100MB inline cap. Kept with a safety margin.
VERTEX_INLINE_VIDEO_MAX_BYTES = 95 * 1024 * 1024
# Separate, much stricter limit: when a video is attached as a URI
# reference (Part.from_uri, e.g. a direct HTTPS link Tier-1 resolved
# without downloading it), Vertex fetches that URL server-side itself —
# confirmed live to hard-cap at exactly 15728640 bytes (15MB): "400
# INVALID_ARGUMENT ... File content exceeded the size limit.
# max_bytes_fetched: 15728640". This is unrelated to
# VERTEX_INLINE_VIDEO_MAX_BYTES above, which only gates OUR OWN
# inline-embedding decision after bytes are already downloaded. A safety
# margin below the exact server limit avoids boundary flakiness.
VERTEX_URI_FETCH_MAX_BYTES = 14 * 1024 * 1024
# 5-minute ceiling for the PROCESSING → ACTIVE polling loop.
FILES_API_POLL_TIMEOUT_SECONDS = 300
FILES_API_POLL_INTERVAL_SECONDS = 3
# Chunk size for streaming downloads.
DOWNLOAD_CHUNK_BYTES = 10 * 1024 * 1024
# _download_drive_file used to have NO timeout at all — the one genuinely
# unbounded blocking call found across this module (every other network
# call here — _download_https, _https_content_length,
# _https_pdf_head_metadata, the Files API upload poll — already has one).
# A per-chunk timeout, not a total-request one: MediaIoBaseDownload streams
# DOWNLOAD_CHUNK_BYTES (10MB) at a time via repeated next_chunk() calls, so
# this bounds how long any single chunk fetch may hang, not the whole
# (possibly large, legitimately slow) download. Matches
# FILES_API_POLL_TIMEOUT_SECONDS's 300s ceiling as this file's established
# "generous but not unbounded" convention for a genuinely slow-but-healthy
# operation, rather than the short ~15-20s used for HEAD/small requests.
DRIVE_DOWNLOAD_CHUNK_TIMEOUT_SECONDS = 300


def _drive_service(service_account_path: str):
    """Build a Drive v3 client with readonly scope from the service account.

    The Sheets service account uses the 'spreadsheets' scope; Drive needs
    'drive.readonly'. We build a separate credential from the same JSON.
    The file must be shared with the service-account email to be readable.

    Imports are local because googleapiclient is only needed on the Drive
    download path; keeping them local avoids importing google.auth and
    googleapiclient at module load when the caller only ever hits the
    HTTPS/Files-API path (the common case for R2B with YouTube links).

    Builds its own httplib2.Http with a timeout (rather than passing
    credentials= straight to build(), which leaves the underlying HTTP
    transport with no timeout at all) so every call made through this
    service — including the streamed download in _download_drive_file — is
    bounded instead of able to hang forever on a stalled connection.
    """
    import google_auth_httplib2
    import httplib2
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        service_account_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    authorized_http = google_auth_httplib2.AuthorizedHttp(
        creds, http=httplib2.Http(timeout=DRIVE_DOWNLOAD_CHUNK_TIMEOUT_SECONDS)
    )
    return build("drive", "v3", http=authorized_http, cache_discovery=False)


def _download_drive_file(service, file_id: str) -> bytes:
    """Stream-download a Drive file into memory and return its bytes.

    Each chunk fetch is bounded by the timeout set on the service's
    underlying http transport (see _drive_service) — previously this had no
    timeout at all, so a stalled connection mid-download could hang a
    worker thread indefinitely.
    """
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=DOWNLOAD_CHUNK_BYTES)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


# 180s was enough for typical short pitch videos but not for shrinking a
# large upload: a real 233MB, long-duration Drive video hit the 180s wall
# mid-encode (confirmed live — the whole point of shrinking is that the
# source is big, so the timeout must budget for the big case, not the
# typical one). 600s plus the veryfast preset below keeps the worst case
# bounded without failing legitimate large submissions.
TRANSCODE_TIMEOUT_SECONDS = 600


def _transcode_to_mp4(data: bytes) -> bytes:
    """Re-encode arbitrary video bytes to MP4 (H.264/AAC) in memory.

    Only video/mp4 is confirmed to work with Vertex AI's inline
    Part.from_bytes video input (see ingest_video's docstring) — a
    real .webm sent inline, even labeled correctly, gets a 400 from Gemini.
    Runs ffmpeg as a subprocess piping bytes via stdin/stdout (pipe:0/pipe:1)
    — nothing touches disk. Re-encodes rather than stream-copies: copying
    would only work if the source codec is already MP4-compatible, and
    re-encoding to the most universally-supported codec pair is more
    reliable than trying to detect and special-case source codecs.
    """
    proc = subprocess.run(
        [
            "ffmpeg", "-y",
            "-i", "pipe:0",
            "-c:v", "libx264", "-c:a", "aac",
            "-movflags", "frag_keyframe+empty_moov",  # streamable to a pipe; no seekable-file requirement
            "-f", "mp4",
            "pipe:1",
        ],
        input=data,
        capture_output=True,
        timeout=TRANSCODE_TIMEOUT_SECONDS,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise VideoResolutionError(
            "Video transcode to MP4 failed: "
            + proc.stderr[-500:].decode("utf-8", errors="replace")
        )
    return proc.stdout


VIDEO_SHRINK_AUDIO_BITRATE_BPS = 128_000
VIDEO_SHRINK_SAFETY_MARGIN = 0.92  # headroom for container/muxing overhead
VIDEO_SHRINK_MIN_VIDEO_BITRATE_BPS = 100_000  # floor so we never target an unwatchable bitrate


def _shrink_video_to_fit(data: bytes, max_bytes: int) -> bytes:
    """Re-encode a video to fit under max_bytes, minimizing quality loss.

    Rather than a fixed low bitrate (wastes quality on short videos, still
    fails to fit long ones), probes the video's actual duration and derives
    a target bitrate from the byte budget — the standard "encode to target
    file size" technique, so quality is only reduced as much as the size
    constraint actually requires. Runs entirely via ffmpeg/ffprobe
    stdin/stdout pipes — nothing touches disk.
    """
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json", "-show_format", "-show_streams",
            # A fragmented MP4 (empty_moov — this function's own output,
            # when called a second time on an already-shrunk video) has no
            # duration in its top-level format box; ffprobe needs to scan
            # further into the stream to compute it. Without these, the
            # format-level probe below silently returns no "duration" key
            # at all rather than erroring, which reads as "unprobable".
            "-analyzeduration", "100M", "-probesize", "100M",
            "-i", "pipe:0",
        ],
        input=data,
        capture_output=True,
        timeout=60,
    )
    if probe.returncode != 0:
        raise VideoResolutionError(
            "Video duration probe failed: "
            + probe.stderr[-300:].decode("utf-8", errors="replace")
        )
    try:
        probe_json = json.loads(probe.stdout)
        duration_str = probe_json.get("format", {}).get("duration")
        if duration_str is None:
            # Fall back to the longest stream's own duration — format-level
            # duration can be absent (fragmented containers) even though
            # per-stream duration is present.
            stream_durations = [
                float(s["duration"]) for s in probe_json.get("streams", [])
                if s.get("duration") is not None
            ]
            if not stream_durations:
                raise KeyError("duration")
            duration = max(stream_durations)
        else:
            duration = float(duration_str)
    except Exception as exc:
        raise VideoResolutionError(
            f"Could not determine video duration: {type(exc).__name__}"
        ) from exc
    if duration <= 0:
        raise VideoResolutionError("Video has zero or unknown duration.")

    target_total_bps = (max_bytes * 8 * VIDEO_SHRINK_SAFETY_MARGIN) / duration
    video_bps = max(
        int(target_total_bps - VIDEO_SHRINK_AUDIO_BITRATE_BPS),
        VIDEO_SHRINK_MIN_VIDEO_BITRATE_BPS,
    )

    # The encode reads from a temp FILE, not stdin — an MP4 with its moov
    # atom at the END (how phones/screen recorders typically write them)
    # cannot be demuxed from a non-seekable pipe: ffmpeg reaches EOF still
    # looking for the index and dies with "Invalid data found when
    # processing input" (confirmed live on a real 233MB Drive upload —
    # the grader then reported the video as "inaccessible and corrupted").
    # ffprobe above survives pipe input only because of its large
    # -analyzeduration/-probesize scan buffers; the encoder gets no such
    # luxury. Output stays a pipe — frag_keyframe+empty_moov exists
    # precisely to make the OUTPUT writable without seeking.
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as src_file:
        src_file.write(data)
        src_path = src_file.name
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", src_path,
                "-c:v", "libx264",
                # veryfast: ~3-5x faster than the default medium preset.
                # At the low bitrates this budget-driven shrink targets,
                # preset quality differences are marginal — the bitrate cap
                # dominates quality — but encode TIME is what times out on
                # large sources.
                "-preset", "veryfast",
                "-b:v", str(video_bps),
                "-maxrate", str(int(video_bps * 1.5)),
                "-bufsize", str(int(video_bps * 2)),
                "-c:a", "aac", "-b:a", str(VIDEO_SHRINK_AUDIO_BITRATE_BPS),
                "-movflags", "frag_keyframe+empty_moov",
                "-f", "mp4",
                "pipe:1",
            ],
            capture_output=True,
            timeout=TRANSCODE_TIMEOUT_SECONDS,
        )
    finally:
        os.unlink(src_path)
    if proc.returncode != 0 or not proc.stdout:
        raise VideoResolutionError(
            "Video compress failed: "
            + proc.stderr[-500:].decode("utf-8", errors="replace")
        )
    return proc.stdout


def _drive_video_mime_type(service, file_id: str) -> str:
    """Return the video MIME type to declare to Gemini for a Drive file.

    Drive knows the real container format (mp4, webm, mov, ...) from the
    upload itself — trust it rather than assuming every Drive video is an
    mp4. A real .webm sent to Gemini labeled video/mp4 fails with an opaque
    400 INVALID_ARGUMENT (confirmed live: Drive reported
    'video/webm' for a file that was hardcoded to 'video/mp4' here, and
    Gemini rejected it). Falls back to video/mp4 only if Drive's reported
    type isn't one Gemini documents support for — better an honest guess at
    a supported type than sending a format Gemini is certain to reject.
    """
    from .video_urls import SUPPORTED_VIDEO_MIME_TYPES

    try:
        meta = service.files().get(fileId=file_id, fields="mimeType").execute()
        reported = meta.get("mimeType", "")
    except Exception:
        return "video/mp4"
    return reported if reported in SUPPORTED_VIDEO_MIME_TYPES else "video/mp4"


def _download_https(url: str) -> bytes:
    """Download a direct HTTPS URL into memory, with a size guard.

    timeout=20 (not the 300s this used to be): a legitimate PDF/Slides
    export or direct video responds in seconds. Cloudflare-protected pages
    (Canva, etc.) often don't reject a suspected bot with a clean error —
    they silently stall the connection instead, which is worse than a fast
    failure (confirmed live: one such row was still hanging past 4m50s).
    A short timeout turns that into a fast, honest VideoResolutionError
    instead of tying up a worker thread for most of 5 minutes per attempt.
    """
    # SSRF guard, same policy as Tier-1: this helper is reachable with raw
    # applicant-submitted URLs (Alchemist pitch-deck links reach it without
    # passing resolve_video_url), so it must validate the destination itself
    # instead of relying on callers. safe_opener rejects non-HTTPS,
    # credential-bearing and private-network targets before the first
    # request, and returns an opener that can only dispatch HTTPS; every
    # caller already treats the resulting error as "link unresolvable".
    opener = safe_opener(url)
    headers = {"User-Agent": USER_AGENT}
    req = Request(url, headers=headers, method="GET")
    chunks = []
    written = 0
    with opener.open(req, timeout=20) as response:
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > FILES_API_MAX_BYTES:
                raise VideoResolutionError(
                    "Video exceeds the 2GB Gemini Files API ceiling."
                )
            chunks.append(chunk)
    return b"".join(chunks)


def _upload_or_wrap(
    data: bytes,
    mime_type: str | None,
) -> tuple[str | None, bytes | None]:
    """Turn downloaded bytes into whatever the workflow needs to build a Part.

    Returns (uri, data) — exactly one is non-None, matching ResolvedMedia's
    contract:
      - On Vertex AI: the Files API isn't available, so this just hands the
        bytes straight back for inline Part.from_bytes. No network call.
      - On the Developer API: uploads the bytes to the Gemini Files API via
        an in-memory buffer, polls until ACTIVE, and returns the file URI.
    """
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        return None, data

    from google import genai

    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
    try:
        upload = client.files.upload(
            file=io.BytesIO(data),
            config={"mime_type": mime_type} if mime_type else None,
        )
    except Exception as exc:
        raise VideoResolutionError(
            f"Gemini Files API upload failed: {type(exc).__name__}"
        ) from exc

    deadline = time.time() + FILES_API_POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        info = client.files.get(name=upload.name)
        # State may be an enum-like string ("STATE_ACTIVE") or a plain
        # string ("ACTIVE"). Normalize to uppercase suffix for robustness.
        state = str(getattr(info, "state", "") or "").upper()
        if "ACTIVE" in state:
            uri = getattr(info, "uri", None)
            if not uri:
                raise VideoResolutionError(
                    "Files API returned ACTIVE file with no uri."
                )
            return uri, None
        if "FAILED" in state:
            raise VideoResolutionError("Gemini Files API processing failed.")
        time.sleep(FILES_API_POLL_INTERVAL_SECONDS)
    raise VideoResolutionError("Gemini Files API upload timed out (5 min).")


def _https_content_length(url: str) -> int | None:
    """HEAD request for Content-Length. Returns None if unavailable —
    callers should treat unknown size as "assume it needs downloading"
    rather than risk sending an oversized URI reference to Vertex."""
    # SSRF guard: same rationale as _download_https — this helper also takes
    # raw applicant-submitted URLs, so it validates before it connects.
    opener = safe_opener(url)
    headers = {"User-Agent": USER_AGENT}
    req = Request(url, headers=headers, method="HEAD")
    try:
        with opener.open(req, timeout=15) as response:
            length = response.headers.get("Content-Length")
            return int(length) if length is not None else None
    except Exception:
        return None


def _https_pdf_head_metadata(url: str) -> tuple[str | None, int | None]:
    """HEAD request for Content-Type and Content-Length together.

    Used to qualify a direct pitch-deck URL for the no-download fast path
    below — returns (None, None) on any failure so the caller falls back
    to the existing, fully-validated download path rather than risk
    sending an unverified reference to Gemini.
    """
    # SSRF guard: same rationale as _download_https — this helper also takes
    # raw applicant-submitted URLs, so it validates before it connects.
    opener = safe_opener(url)
    headers = {"User-Agent": USER_AGENT}
    req = Request(url, headers=headers, method="HEAD")
    try:
        with opener.open(req, timeout=15) as response:
            content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            length = response.headers.get("Content-Length")
            return (content_type or None), (int(length) if length is not None else None)
    except Exception:
        return None, None


def _finalize_downloaded_video(data: bytes, mime_type: str, source: str) -> ResolvedMedia:
    """Shared post-download processing: transcode/shrink for Vertex's inline
    limits, then upload-or-wrap. Used by both the Drive-download and the
    oversized-direct-URL paths below, which otherwise duplicated this."""
    original_size_bytes = len(data)
    if (
        mime_type != "video/mp4"
        and os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
    ):
        # On Vertex, video goes inline (Part.from_bytes) — no Files API to
        # properly ingest arbitrary containers there. Only video/mp4 is
        # confirmed to work inline; a real .webm sent inline labeled
        # correctly still 400s (confirmed live). Transcode to MP4 in memory
        # via ffmpeg rather than losing the video as evidence entirely.
        data = _transcode_to_mp4(data)
        mime_type = "video/mp4"
    if (
        len(data) > VERTEX_INLINE_VIDEO_MAX_BYTES
        and os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
    ):
        # A correctly-formatted mp4 can still be too big for Vertex's inline
        # limit — same failure mode as the oversized PDF (crashes the whole
        # row, deck+text included). Shrink rather than drop it, same
        # reasoning as the PDF case: don't let one oversized file take the
        # whole row down.
        data = _shrink_video_to_fit(data, VERTEX_INLINE_VIDEO_MAX_BYTES)
    uri, inline_data = _upload_or_wrap(data, mime_type=mime_type)
    return ResolvedMedia(
        uri=uri or "", mime_type=mime_type, source=source, data=inline_data,
        original_size_bytes=original_size_bytes,
    )


def ingest_video(
    submitted_url: str,
    service_account_path: str,
) -> ResolvedMedia:
    """Resolve a submitted video URL for R2B grading.

    Tries Tier-1 (video_urls.resolve_video_url) first — it's the cheap path
    and covers YouTube natively without download. If Tier-1 resolves a real
    video, returns that result. Otherwise falls back to Tier-2: download
    the source from Google Drive via the Drive API, returning a
    ResolvedMedia with the media as in-memory bytes (Vertex) or a Files
    API URI (Developer API). If neither resolves, raises — the caller
    routes that to human review.
    """
    # Tier 1 first.
    resolved: ResolvedMedia | None = None
    try:
        resolved = resolve_video_url(submitted_url)
    except VideoResolutionError:
        resolved = None

    if resolved is not None:
        is_youtube = "youtube" in resolved.uri or "youtu.be" in resolved.uri
        if is_youtube:
            return resolved
        # Non-YouTube URL that Tier-1 resolved directly (not via Drive) —
        # e.g. a video hosted on a CDN. A small file can just be handed to
        # Gemini as a URI reference (Part.from_uri); Gemini fetches it
        # itself. But that server-side fetch has its own hard cap —
        # confirmed live: "400 INVALID_ARGUMENT ... File content exceeded
        # the size limit. max_bytes_fetched: 15728640" (exactly 15MB) — a
        # completely separate, much stricter limit than
        # VERTEX_INLINE_VIDEO_MAX_BYTES (95MB), which only applies once we
        # download bytes ourselves. So: stay on the cheap direct-URI path
        # when the file is small enough to survive that fetch; only pay for
        # a full download+inline-embed when it's actually over the limit
        # (or size can't be determined at all, which is the safer default).
        content_length = _https_content_length(submitted_url)
        if content_length is not None and content_length <= VERTEX_URI_FETCH_MAX_BYTES:
            return resolved
        try:
            data = _download_https(submitted_url)
            if len(data) > FILES_API_MAX_BYTES:
                raise VideoResolutionError("Video exceeds the 2GB size limit.")
            mime_type = resolved.mime_type or "video/mp4"
            return _finalize_downloaded_video(data, mime_type, source="direct_download")
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Direct video download failed: {type(exc).__name__}: {exc}"
            ) from exc

    # Tier 2: Google Drive link → download via Drive API.
    drive_id = extract_drive_file_id(submitted_url)
    if drive_id:
        try:
            service = _drive_service(service_account_path)
            mime_type = _drive_video_mime_type(service, drive_id)
            data = _download_drive_file(service, drive_id)
            if len(data) > FILES_API_MAX_BYTES:
                raise VideoResolutionError(
                    "Drive video exceeds the 2GB size limit."
                )
            return _finalize_downloaded_video(data, mime_type, source="drive_upload")
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Drive video download failed: {type(exc).__name__}: {exc}"
            ) from exc

    # Nothing worked (not YouTube, not a direct video file, not Drive).
    # Surface a clear error — the caller (pipeline.py) routes a submitted-
    # but-unresolvable video link to human review rather than downloading
    # whatever the URL actually points to (e.g. a webpage) and disguising
    # it as video data.
    raise VideoResolutionError(
        "Video could not be resolved to a downloadable source."
    )


# ---------------------------------------------------------------------------
# Pitch deck ingestion (Alchemist: Google Slides / Google Drive PDF download)
# ---------------------------------------------------------------------------

# PDF MIME for Gemini. Pitch decks are downloaded as PDF regardless of
# whether the source is a Google Slides presentation or a Drive PDF.
PITCH_DECK_MIME_TYPE = "application/pdf"


def _is_google_slides_url(url: str) -> bool:
    """Google Slides URLs look like:
      https://docs.google.com/presentation/d/<ID>/...

    Host and path are checked separately rather than as one substring:
    the substring test also matched a URL that merely mentions the Slides
    address elsewhere (https://evil.tld/#docs.google.com/presentation/...),
    which would send _slides_export_url's regex looking for an ID on a
    host we do not control.
    """
    parsed = urlparse(url.lower())
    return parsed.hostname == "docs.google.com" and parsed.path.startswith(
        "/presentation/d/"
    )


def _slides_export_url(url: str) -> str:
    """Convert a Google Slides URL to its PDF export endpoint."""
    match = re.search(r"/presentation/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise VideoResolutionError(
            "Could not extract presentation ID from Google Slides URL."
        )
    pres_id = match.group(1)
    return f"https://docs.google.com/presentation/d/{pres_id}/export/pdf"


def _looks_like_pdf(data: bytes) -> bool:
    """Reject downloads that aren't actually a PDF.

    A non-Slides, non-Drive pitch deck URL (e.g. a Canva share page) has no
    guaranteed export endpoint — _download_https happily fetches whatever is
    at that URL, which is often the page's HTML, not a PDF. Checking the PDF
    magic bytes turns that into an honest "deck unavailable" instead of
    uploading HTML to Gemini mislabeled application/pdf.
    """
    return data[:5] == b"%PDF-"


PDF_COMPRESS_MAX_IMAGE_DIMENSION = 1600  # px, long edge
PDF_COMPRESS_JPEG_QUALITY = 85


def _compress_pdf(data: bytes) -> bytes:
    """Shrink a PDF by downsampling only its oversized embedded images.

    Minimizes quality loss by being targeted rather than uniform: each
    image is checked individually and only touched if it's larger than
    PDF_COMPRESS_MAX_IMAGE_DIMENSION on its long edge (most oversized decks
    are a handful of screenshots/photos exported at print resolution, far
    higher than useful for a model reading the deck) — already-reasonable
    images are left untouched. Text, vector graphics, and layout are
    unaffected; only raster images are re-encoded.
    """
    import fitz
    from PIL import Image

    # Pitch-deck PDFs are applicant-supplied, and Pillow only WARNS between
    # its default MAX_IMAGE_PIXELS and twice that, so a decompression-bomb
    # image decodes silently at ~1GB peak per row (times MAX_CONCURRENCY).
    # Set here rather than at module scope so importing this module never
    # changes Pillow's behaviour for the rest of the Streamlit host.
    Image.MAX_IMAGE_PIXELS = 40_000_000

    doc = fitz.open(stream=data, filetype="pdf")
    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                pil_img = Image.open(io.BytesIO(base_image["image"]))
                pil_img.load()
            except Exception:
                continue
            w, h = pil_img.size
            if max(w, h) <= PDF_COMPRESS_MAX_IMAGE_DIMENSION:
                continue
            scale = PDF_COMPRESS_MAX_IMAGE_DIMENSION / max(w, h)
            new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
            resized = pil_img.convert("RGB").resize(new_size, Image.LANCZOS)
            out = io.BytesIO()
            resized.save(out, format="JPEG", quality=PDF_COMPRESS_JPEG_QUALITY)
            try:
                page.replace_image(xref, stream=out.getvalue())
            except Exception:
                continue

    out_buf = io.BytesIO()
    doc.save(out_buf, garbage=4, deflate=True)
    doc.close()
    return out_buf.getvalue()


def _extract_chart_images(data: bytes) -> list[tuple[bytes, str]]:
    """Extract image-only slides (no selectable text) from a pitch deck.

    Many deck slides are a single flat screenshot — a financial chart, a
    traction graph, a projections table — pasted in with no separate text
    layer, and these are the most common place exact revenue/ARR/user
    numbers live. Feeds _read_chart_images, which converts them to text via
    one bundled Gemini call (see that function for why text, not raw
    images, is what downstream calls actually use).

    Only pages with ZERO extractable text and at least one embedded image
    qualify — a deliberately narrow, high-precision match (a slide with any
    real text is left to the normal deck read) rather than pulling every
    image on every slide, most of which are logos/icons/photos with nothing
    checkable on them.
    """
    import fitz
    from PIL import Image

    # Pitch-deck PDFs are applicant-supplied, and Pillow only WARNS between
    # its default MAX_IMAGE_PIXELS and twice that, so a decompression-bomb
    # image decodes silently at ~1GB peak per row (times MAX_CONCURRENCY).
    # Set here rather than at module scope so importing this module never
    # changes Pillow's behaviour for the rest of the Streamlit host.
    Image.MAX_IMAGE_PIXELS = 40_000_000

    results: list[tuple[bytes, str]] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return results
    for page in doc:
        if page.get_text().strip():
            continue
        images = page.get_images(full=True)
        if not images:
            continue
        # Largest embedded image on the page (by pixel area) is almost
        # always the actual slide content; smaller ones tend to be
        # background textures or decorative elements on the same page.
        best_xref = None
        best_area = 0
        for img_info in images:
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
                pil_img = Image.open(io.BytesIO(base_image["image"]))
                w, h = pil_img.size
            except Exception:
                continue
            if w * h > best_area:
                best_area = w * h
                best_xref = xref
        if best_xref is None:
            continue
        try:
            base_image = doc.extract_image(best_xref)
        except Exception:
            continue
        ext = base_image.get("ext", "png").lower()
        mime_type = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
        results.append((base_image["image"], mime_type))
    doc.close()
    return results


def _build_genai_client():
    from google import genai

    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        return genai.Client(
            vertexai=True,
            project=os.environ.get("GOOGLE_CLOUD_PROJECT"),
            location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"),
        )
    return genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))


def _read_chart_images(images: list[tuple[bytes, str]], model: str) -> str:
    """Convert image-only deck slides to plain text via ONE bundled Gemini
    call — every image goes in a single request, not one call per image,
    so this adds exactly one extra call per deck regardless of how many
    image-only slides it has.

    Best-effort: returns "" (no text) on any failure — a chart-reading
    problem should never fail pitch deck ingestion, it just means that
    deck's charts don't get the pre-extraction benefit for this row.
    """
    if not images:
        return ""
    from google.genai import types

    try:
        client = _build_genai_client()
        parts = [
            "The following images are slides from a startup pitch deck "
            "that have no selectable text (charts, financial tables, "
            "screenshots). For EACH image, in order, list every number, "
            "label, and axis you can read on it. If an image has no "
            "readable numbers (e.g. it's a logo or photo), say so briefly. "
            "Label each response by image number (Image 1, Image 2, ...)."
        ]
        for image_bytes, mime_type in images:
            parts.append(types.Part.from_bytes(data=image_bytes, mime_type=mime_type))
        response = client.models.generate_content(model=model, contents=parts)
        return (response.text or "").strip()
    except Exception:
        return ""


def ingest_pitch_deck(
    submitted_url: str,
    service_account_path: str,
    analyzer_model: str = "",
) -> ResolvedMedia:
    """Resolve a submitted pitch deck link for Alchemist grading.

    Handles three source shapes:
      - Direct HTTPS links to a PDF, confirmed via a HEAD request
        (Content-Type: application/pdf, size within Gemini's ~14MB direct-
        fetch limit) — no download at all; Gemini fetches the URI itself,
        same as Tier-1 video. Trades away chart/table OCR extraction and
        magic-bytes PDF validation (neither is possible without the
        bytes) for genuinely direct, download-free ingestion. Falls back
        to downloading below if the HEAD check doesn't cleanly qualify.
      - Google Slides links (docs.google.com/presentation/d/<ID>/...)
        — converted to a PDF export URL and downloaded via HTTPS.
      - Google Drive file links (drive.google.com/... or open?id=...)
        — downloaded via the Drive API (same path as video ingestion).

    The downloaded PDF's bytes are handed to Gemini directly (Vertex) or
    uploaded to the Gemini Files API (Developer API). The Files API
    auto-expires the object after 48 hours, so no cleanup is needed there.
    The returned ResolvedMedia carries mime_type=application/pdf.
    """
    try:
        # Google Slides: export as PDF via HTTPS.
        if _is_google_slides_url(submitted_url):
            export_url = _slides_export_url(submitted_url)
            data = _download_https(export_url)
        else:
            # Google Drive file link: download via the Drive API.
            drive_id = extract_drive_file_id(submitted_url)
            if drive_id:
                service = _drive_service(service_account_path)
                data = _download_drive_file(service, drive_id)
            elif is_drive_folder_url(submitted_url):
                # A folder link, not a file — downloading it as if it were
                # a PDF would just fetch a folder-listing page and fail
                # slowly/unpredictably further down. Fail immediately with
                # an honest, specific message instead.
                raise VideoResolutionError(
                    "Pitch deck link is a Google Drive folder, not a file — "
                    "share a direct file link instead."
                )
            else:
                # Direct HTTPS link: try the no-download fast path first —
                # only qualifies with a confirmed application/pdf
                # Content-Type and a size within Gemini's direct-fetch
                # limit. Anything uncertain (HEAD fails, wrong/missing
                # Content-Type, oversized, or unknown size) falls through
                # to the existing, fully-validated download below.
                content_type, content_length = _https_pdf_head_metadata(submitted_url)
                if (
                    content_type == "application/pdf"
                    and content_length is not None
                    and content_length <= VERTEX_URI_FETCH_MAX_BYTES
                ):
                    # The fast path hands this raw URL to Gemini, which
                    # fetches it server-side, so the URI must be validated
                    # in its own right rather than inheriting the HEAD
                    # request's guard — never emit an unvalidated file_uri.
                    _public_https_host(submitted_url)
                    return ResolvedMedia(
                        uri=submitted_url,
                        mime_type=PITCH_DECK_MIME_TYPE,
                        source="pitch_deck_direct",
                        original_size_bytes=content_length,
                    )
                data = _download_https(submitted_url)

        if len(data) > FILES_API_MAX_BYTES:
            raise VideoResolutionError(
                "Pitch deck exceeds the 2GB size limit."
            )

        if not _looks_like_pdf(data):
            raise VideoResolutionError(
                "Downloaded pitch deck is not a valid PDF — the source URL "
                "may not offer a direct export (e.g. a Canva share page)."
            )

        use_vertex = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
        if use_vertex and len(data) > VERTEX_INLINE_PDF_MAX_BYTES:
            # Deck is the required, primary source for Alchemist — unlike
            # video, degrading to "unavailable" here has a real fairness
            # cost (caps Product/MVP & Innovation, cripples the rest of the
            # rubric). Compress rather than give up: recompresses only the
            # oversized embedded images (see _compress_pdf), preserving
            # text/layout, before falling back to an honest error.
            data = _compress_pdf(data)
            if len(data) > VERTEX_INLINE_PDF_MAX_BYTES:
                raise VideoResolutionError(
                    "Pitch deck exceeds Vertex AI's inline PDF size limit "
                    "even after image compression."
                )

        # Best-effort: a chart-extraction or pre-read failure should never
        # fail the whole deck ingestion — both functions degrade to an
        # empty result internally on any error.
        chart_images = _extract_chart_images(data)
        chart_text = _read_chart_images(chart_images, analyzer_model) if analyzer_model else ""

        uri, inline_data = _upload_or_wrap(data, mime_type=PITCH_DECK_MIME_TYPE)
        return ResolvedMedia(
            uri=uri or "",
            mime_type=PITCH_DECK_MIME_TYPE,
            source="pitch_deck_upload",
            data=inline_data,
            chart_text=chart_text,
        )

    except VideoResolutionError:
        raise
    except Exception as exc:
        raise VideoResolutionError(
            f"Pitch deck download failed: {type(exc).__name__}: {exc}"
        ) from exc
