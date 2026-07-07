"""Tier-2 video ingestion for R2B: download from sources video_urls.py
cannot resolve to a Gemini-fetchable URI, then upload to the Gemini Files
API so the analyst/grader receive the video as a native multimodal Part.

Scope of this module:
  - Google Drive share links (download via Drive API → Files API upload)
  - Direct HTTPS videos that Tier-1 rejected as too large or as a webpage
    requiring url_context (download via urllib → Files API upload)

What stays on the Tier-1 resolver (video_urls.resolve_video_url):
  - Public YouTube URLs (native Gemini input, no download)
  - Direct HTTPS video ≤100MB with a known MIME
  - Webpages with discoverable og:video / <video> / JSON-LD metadata

Auth: the Drive download reuses the same service-account JSON as Sheets,
but with the drive.readonly scope. The Drive file MUST be shared with the
service-account email (the same one the Sheet is shared with). If it is
not, the download 404s and the caller treats it as "no video" — criterion 6
(Presentation & Clarity) then scores 1 with rationale "No video submitted."

Backend: this module uses the Gemini Files API (Developer API / API key)
exclusively. The Files API auto-expires uploaded objects after 48 hours,
so no explicit cleanup is needed. The previous Vertex AI / GCS upload
path has been removed — the project no longer carries GCP billing
dependencies (no Cloud Storage, no Vertex AI).
"""
import os
import re
import tempfile
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
# Chunk size for streaming direct-HTTPS downloads into a temp file.
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


def _download_drive_file(service, file_id: str, dest_path: str) -> str:
    """Stream-download a Drive file to dest_path. Returns dest_path."""
    from googleapiclient.http import MediaIoBaseDownload

    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=DOWNLOAD_CHUNK_BYTES)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
    return dest_path


def _download_https(url: str, dest_path: str) -> str:
    """Download a direct HTTPS URL to dest_path with a size guard."""
    # Reuse video_urls' SSRF-safe redirect handler for consistency.
    from urllib.request import build_opener, Request
    from .video_urls import _SafeRedirectHandler

    opener = build_opener(_SafeRedirectHandler())
    headers = {"User-Agent": "AI-Fellowship-Agent/1.0"}
    req = Request(url, headers=headers, method="GET")
    with opener.open(req, timeout=300) as response, open(dest_path, "wb") as fh:
        written = 0
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_BYTES)
            if not chunk:
                break
            written += len(chunk)
            if written > FILES_API_MAX_BYTES:
                raise VideoResolutionError(
                    "Video exceeds the 2GB Gemini Files API ceiling."
                )
            fh.write(chunk)
    return dest_path


def _upload_to_gemini_files_api(
    local_path: str,
    mime_type: Optional[str],
) -> str:
    """Upload a local file and return a URI for the workflow.

    On the Developer API (API key): uses the Gemini Files API, polls until
    ACTIVE, and returns the file URI. The Files API auto-expires after 48h.

    On Vertex AI: the Files API is not available. Instead, returns the local
    file path with a file:// prefix so the workflow can load it as
    Part.from_bytes (inline data). This avoids GCS entirely.
    """
    # Vertex AI mode: return local path for inline upload.
    if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        return f"file://{local_path}"

    # Developer API mode: use Files API.
    from google import genai

    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY"))
    try:
        upload = client.files.upload(
            file=local_path,
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
            return uri
        if "FAILED" in state:
            raise VideoResolutionError("Gemini Files API processing failed.")
        time.sleep(FILES_API_POLL_INTERVAL_SECONDS)
    raise VideoResolutionError("Gemini Files API upload timed out (5 min).")


def _temp_video_path(url: str) -> str:
    """Allocate a temp path with a best-effort extension from the URL."""
    ext = ""
    if "." in url:
        candidate = url.rsplit(".", 1)[-1].split("/")[0].split("?")[0].lower()
        if candidate and candidate.isalnum() and len(candidate) <= 5:
            ext = "." + candidate
    tmp = tempfile.NamedTemporaryFile(
        suffix=ext or ".mp4", delete=False, dir=tempfile.gettempdir(),
    )
    path = tmp.name
    tmp.close()
    return path


def ingest_video_for_r2b(
    submitted_url: str,
    service_account_path: str,
) -> ResolvedVideo:
    """Resolve a submitted video URL for R2B grading.

    Tries Tier-1 (video_urls.resolve_video_url) first — it's the cheap path
    and covers YouTube natively without download. If Tier-1 succeeds without
    needing url_context, returns that result. If Tier-1 fails or only
    resolves to a webpage, falls back to Tier-2: download the source (Google
    Drive via the Drive API, or direct HTTPS) and upload to the Gemini Files
    API, returning a ResolvedVideo whose uri is the Files API file URI.
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

    # Tier 2: Google Drive link → download via Drive API → Files API upload.
    drive_id = extract_drive_file_id(submitted_url)
    if drive_id:
        tmp_path = _temp_video_path(submitted_url)
        try:
            service = _drive_service(service_account_path)
            _download_drive_file(service, drive_id, tmp_path)
            if os.path.getsize(tmp_path) > FILES_API_MAX_BYTES:
                raise VideoResolutionError(
                    "Drive video exceeds the 2GB size limit."
                )
            uri = _upload_to_gemini_files_api(tmp_path, mime_type=None)
            return ResolvedVideo(uri=uri, mime_type=None, source="drive_upload")
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Drive video download failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            # On Vertex AI, the file:// URI points to this local file.
            # Keep it alive for all agent calls (analyst, grader, head).
            # Only delete on Developer API (file already uploaded to Files API).
            if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() != "true":
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # Tier 2: direct HTTPS download (for large files Tier-1 rejected).
    if resolved is not None and resolved.requires_url_context:
        tmp_path = _temp_video_path(submitted_url)
        try:
            _download_https(resolved.uri, tmp_path)
            uri = _upload_to_gemini_files_api(tmp_path, mime_type=None)
            return ResolvedVideo(uri=uri, mime_type=None, source="https_upload")
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Direct video download failed: {type(exc).__name__}"
            ) from exc
        finally:
            # Keep temp file on Vertex AI (file:// URI needs it for all agent calls).
            if os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() != "true":
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

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


def _temp_pitch_deck_path(url: str) -> str:
    """Allocate a temp path with a .pdf suffix for pitch deck downloads."""
    tmp = tempfile.NamedTemporaryFile(
        suffix=".pdf", delete=False, dir=tempfile.gettempdir(),
    )
    path = tmp.name
    tmp.close()
    return path


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

    The downloaded PDF is uploaded to the Gemini Files API and the file
    URI is returned. The Files API auto-expires the object after 48 hours,
    so no cleanup is needed. The returned ResolvedVideo carries
    mime_type=application/pdf.
    """
    tmp_path = _temp_pitch_deck_path(submitted_url)

    try:
        # Google Slides: export as PDF via HTTPS.
        if _is_google_slides_url(submitted_url):
            export_url = _slides_export_url(submitted_url)
            _download_https(export_url, tmp_path)
        else:
            # Google Drive file link: download via the Drive API.
            drive_id = extract_drive_file_id(submitted_url)
            if drive_id:
                service = _drive_service(service_account_path)
                _download_drive_file(service, drive_id, tmp_path)
            else:
                # Direct HTTPS link to a PDF.
                _download_https(submitted_url, tmp_path)

            if os.path.getsize(tmp_path) > FILES_API_MAX_BYTES:
                raise VideoResolutionError(
                    "Pitch deck exceeds the 2GB size limit."
                )

        uri = _upload_to_gemini_files_api(tmp_path, mime_type=PITCH_DECK_MIME_TYPE)
        return ResolvedVideo(uri=uri, mime_type=PITCH_DECK_MIME_TYPE, source="pitch_deck_upload")

    except VideoResolutionError:
        raise
    except Exception as exc:
        raise VideoResolutionError(
            f"Pitch deck download failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
