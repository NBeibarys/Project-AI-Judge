"""Tier-2 video ingestion for R2B: download from sources video_urls.py
cannot resolve to a Gemini-fetchable URI, then hand the bytes to Gemini
so the analyst/grader receive the video as a native multimodal Part.

Scope of this module:
  - Google Drive share links (download via Drive API)

What stays on the Tier-1 resolver (video_urls.resolve_video_url) and is
never downloaded here:
  - Public YouTube URLs (native Gemini input, no download)
  - Direct HTTPS video ≤100MB with a known MIME
  - Webpages with no discoverable direct video (requires_url_context=True,
    e.g. Canva/Loom share pages) — handed to the analyst's url_context tool
    to read live, never downloaded as if it were video data. A page's HTML
    is not a video file; disguising one as the other sends Gemini garbage
    input instead of an honest "video unavailable."

Auth: the Drive download reuses the same service-account JSON as Sheets,
but with the drive.readonly scope. The Drive file MUST be shared with the
service-account email (the same one the Sheet is shared with). If it is
not, the download 404s and the caller treats it as "no video" — criterion 6
(Presentation & Clarity) then scores 1 with rationale "No video submitted."

Backend: downloads are held entirely in memory, never written to disk.
  - On Vertex AI: the Files API isn't available, so ResolvedVideo.data
    carries the raw bytes directly for inline Part.from_bytes.
  - On the Developer API (API key): bytes are streamed straight into the
    Gemini Files API via an in-memory buffer; ResolvedVideo.uri carries the
    resulting file URI. The Files API auto-expires objects after 48h, so no
    explicit cleanup is needed there either.
Holding bytes in memory instead of a temp file avoids a pointless
write-then-read-back round trip, and means there's nothing left on disk to
clean up after a row finishes.
"""
import io
import os
import re
import time
from typing import Optional

from .google_clients import extract_drive_file_id
from .video_urls import (
    ResolvedVideo,
    VideoResolutionError,
    resolve_video_url,
)

# Gemini Files API: 2GB per file (paid tier 20GB). We cap downloads at 2GB
# to avoid downloading a file we then can't upload.
FILES_API_MAX_BYTES = 2 * 1024 * 1024 * 1024
# 5-minute ceiling for the PROCESSING → ACTIVE polling loop.
FILES_API_POLL_TIMEOUT_SECONDS = 300
FILES_API_POLL_INTERVAL_SECONDS = 3
# Chunk size for streaming downloads.
DOWNLOAD_CHUNK_BYTES = 10 * 1024 * 1024


def _drive_service(service_account_path: str):
    """Build a Drive v3 client with readonly scope from the service account.

    The Sheets service account uses the 'spreadsheets' scope; Drive needs
    'drive.readonly'. We build a separate credential from the same JSON.
    The file must be shared with the service-account email to be readable.

    Imports are local because googleapiclient is only needed on the Drive
    download path; keeping them local avoids importing google.auth and
    googleapiclient at module load when the caller only ever hits the
    HTTPS/Files-API path (the common case for R2B with YouTube links).
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        service_account_path,
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _download_drive_file(service, file_id: str) -> bytes:
    """Stream-download a Drive file into memory and return its bytes."""
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=DOWNLOAD_CHUNK_BYTES)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    return buf.getvalue()


def _download_https(url: str) -> bytes:
    """Download a direct HTTPS URL into memory, with a size guard."""
    # Reuse video_urls' SSRF-safe redirect handler for consistency.
    from urllib.request import build_opener, Request
    from .video_urls import _SafeRedirectHandler

    opener = build_opener(_SafeRedirectHandler())
    headers = {"User-Agent": "AI-Fellowship-Agent/1.0"}
    req = Request(url, headers=headers, method="GET")
    chunks = []
    written = 0
    with opener.open(req, timeout=300) as response:
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
    mime_type: Optional[str],
) -> tuple[Optional[str], Optional[bytes]]:
    """Turn downloaded bytes into whatever the workflow needs to build a Part.

    Returns (uri, data) — exactly one is non-None, matching ResolvedVideo's
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


def ingest_video_for_r2b(
    submitted_url: str,
    service_account_path: str,
) -> ResolvedVideo:
    """Resolve a submitted video URL for R2B grading.

    Tries Tier-1 (video_urls.resolve_video_url) first — it's the cheap path
    and covers YouTube natively without download. If Tier-1 succeeds without
    needing url_context, returns that result. Otherwise falls back to
    Tier-2: download the source from Google Drive via the Drive API,
    returning a ResolvedVideo with the media as in-memory bytes (Vertex) or
    a Files API URI (Developer API).
    """
    # Tier 1 first.
    resolved: Optional[ResolvedVideo] = None
    try:
        resolved = resolve_video_url(submitted_url)
        # If Tier-1 resolved to a YouTube URL, use it directly.
        # If it resolved to a Drive/download URL, we need to download and
        # upload to the Files API (the model cannot fetch these URLs
        # directly: Drive serves an HTML interstitial or requires auth).
        if not resolved.requires_url_context and "youtube" not in resolved.uri and "youtu.be" not in resolved.uri:
            # Non-YouTube URL that Tier-1 thinks is directly fetchable.
            # These often fail in practice (robots.txt, Drive auth).
            # Force download + Files API upload for reliability.
            pass
        elif not resolved.requires_url_context:
            return resolved
    except VideoResolutionError:
        resolved = None

    # Tier 2: Google Drive link → download via Drive API.
    drive_id = extract_drive_file_id(submitted_url)
    if drive_id:
        try:
            service = _drive_service(service_account_path)
            data = _download_drive_file(service, drive_id)
            if len(data) > FILES_API_MAX_BYTES:
                raise VideoResolutionError(
                    "Drive video exceeds the 2GB size limit."
                )
            uri, inline_data = _upload_or_wrap(data, mime_type="video/mp4")
            return ResolvedVideo(
                uri=uri or "",
                mime_type="video/mp4",
                source="drive_upload",
                data=inline_data,
            )
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Drive video download failed: {type(exc).__name__}: {exc}"
            ) from exc

    # No direct-HTTPS-download fallback here: if resolve_video_url() only
    # found a webpage (requires_url_context=True), that URL is not video
    # data — the caller keeps it as a Tier-1 result for the analyst's
    # url_context tool to read live instead of calling this function at all.
    # Downloading such a webpage and re-uploading it labeled "video/mp4"
    # previously sent raw HTML to Gemini disguised as a video file.

    # Nothing worked. Surface a clear error so the pipeline scores
    # criterion 6 as 1 with rationale "No video submitted."
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
    """
    return "docs.google.com/presentation" in url.lower()


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


def ingest_pitch_deck(
    submitted_url: str,
    service_account_path: str,
) -> ResolvedVideo:
    """Resolve a submitted pitch deck link for Alchemist grading.

    Handles two source shapes:
      - Google Slides links (docs.google.com/presentation/d/<ID>/...)
        — converted to a PDF export URL and downloaded via HTTPS.
      - Google Drive file links (drive.google.com/... or open?id=...)
        — downloaded via the Drive API (same path as video ingestion).

    The downloaded PDF's bytes are handed to Gemini directly (Vertex) or
    uploaded to the Gemini Files API (Developer API). The Files API
    auto-expires the object after 48 hours, so no cleanup is needed there.
    The returned ResolvedVideo carries mime_type=application/pdf.
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
            else:
                # Direct HTTPS link to a PDF.
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

        uri, inline_data = _upload_or_wrap(data, mime_type=PITCH_DECK_MIME_TYPE)
        return ResolvedVideo(
            uri=uri or "",
            mime_type=PITCH_DECK_MIME_TYPE,
            source="pitch_deck_upload",
            data=inline_data,
        )

    except VideoResolutionError:
        raise
    except Exception as exc:
        raise VideoResolutionError(
            f"Pitch deck download failed: {type(exc).__name__}: {exc}"
        ) from exc
