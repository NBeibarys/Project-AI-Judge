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

Backend limitation: the Gemini Files API is available on the Developer API
(GOOGLE_API_KEY) only; it is NOT supported on Vertex AI (which uses gs://
URIs instead). When GOOGLE_GENAI_USE_VERTEXAI=TRUE, this module skips the
upload path and falls back to the Tier-1 resolver — so on Vertex, Drive
videos that aren't separately staged in GCS score criterion 6 as 1. The
operator should run R2B against the Developer API for full Drive support.
"""
import os
import tempfile
import time
import uuid
from typing import Optional

from google.oauth2 import service_account

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


def _is_vertex_backend() -> bool:
    return os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE").upper() == "TRUE"


def _drive_service(service_account_path: str):
    """Build a Drive v3 client with readonly scope from the service account.

    The Sheets service account uses the 'spreadsheets' scope; Drive needs
    'drive.readonly'. We build a separate credential from the same JSON.
    The file must be shared with the service-account email to be readable.
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
    """Upload a local video to the Gemini Files API and return its file URI.

    Polls until the file state is ACTIVE. Raises VideoResolutionError on
    upload failure or timeout. The returned URI is passed to the workflow
    as a ResolvedVideo.uri; the workflow constructs a Part.from_uri with it.
    """
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


def _upload_to_gcs(local_path: str, service_account_path: str) -> tuple[str, str]:
    """Upload a local video to Google Cloud Storage and return (gs:// URI, mime_type).

    Vertex AI reads gs:// URIs directly - no Files API needed. The video is
    uploaded to the VIDEO_STAGING_BUCKET bucket with a unique name and
    the gs:// URI is returned for passing to Gemini via Part.from_uri.
    """
    from google.cloud import storage
    import mimetypes

    creds = service_account.Credentials.from_service_account_file(
        service_account_path,
        scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
    )
    client = storage.Client(credentials=creds)
    bucket = client.bucket("VIDEO_STAGING_BUCKET")
    blob_name = f"videos/{uuid.uuid4().hex}.mp4"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)

    # Always use video/mp4 - mimetypes.guess_type() misdetects video files
    # as application/x-msdos-program when the extension is ambiguous.
    # Gemini accepts video/mp4 for all common video formats.
    return f"gs://VIDEO_STAGING_BUCKET/{blob_name}", "video/mp4"


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

    On Vertex AI (where the Files API is unavailable), Tier-2 is skipped —
    the function returns whatever Tier-1 produced (including a webpage
    requiring url_context, or raises if Tier-1 hard-failed). Drive videos
    that aren't separately staged in GCS will then score criterion 6 as 1.
    """
    # Tier 1 first.
    resolved: Optional[ResolvedVideo] = None
    try:
        resolved = resolve_video_url(submitted_url)
        # If Tier-1 resolved to a YouTube URL, use it directly.
        # If it resolved to a Drive/download URL, we need to download and
        # upload to GCS (Vertex AI can't fetch these URLs directly).
        if not resolved.requires_url_context and "youtube" not in resolved.uri and "youtu.be" not in resolved.uri:
            # Non-YouTube URL that Tier-1 thinks is directly fetchable.
            # On Vertex AI, these often fail (robots.txt, Drive auth).
            # Force download + GCS upload for reliability.
            pass
        elif not resolved.requires_url_context:
            return resolved
    except VideoResolutionError:
        resolved = None

    # Tier 2 needs the Files API or GCS. On Vertex AI, use GCS upload.
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
            if _is_vertex_backend():
                uri, mime = _upload_to_gcs(tmp_path, service_account_path)
                return ResolvedVideo(uri=uri, mime_type=mime, source="drive_upload")
            else:
                uri = _upload_to_gemini_files_api(tmp_path, mime_type=None)
                return ResolvedVideo(uri=uri, mime_type=None, source="drive_upload")
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Drive video download failed: {type(exc).__name__}: {exc}"
            ) from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Tier 2: direct HTTPS download (for large files Tier-1 rejected).
    if resolved is not None and resolved.requires_url_context:
        tmp_path = _temp_video_path(submitted_url)
        try:
            _download_https(resolved.uri, tmp_path)
            if _is_vertex_backend():
                uri, mime = _upload_to_gcs(tmp_path, service_account_path)
                return ResolvedVideo(uri=uri, mime_type=mime, source="https_upload")
            else:
                uri = _upload_to_gemini_files_api(tmp_path, mime_type=None)
                return ResolvedVideo(uri=uri, mime_type=None, source="https_upload")
        except VideoResolutionError:
            raise
        except Exception as exc:
            raise VideoResolutionError(
                f"Direct video download failed: {type(exc).__name__}"
            ) from exc
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # Nothing worked. Surface a clear error so the pipeline scores
    # criterion 6 as 1 with rationale "No video submitted."
    raise VideoResolutionError(
        "Video could not be resolved to a downloadable source."
    )
