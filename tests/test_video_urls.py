import unittest
from unittest.mock import MagicMock, patch

from src.video_urls import (
    VideoResolutionError,
    _public_https_host,
    canonicalize_youtube_url,
    resolve_video_url,
)


class YouTubeUrlTests(unittest.TestCase):
    def test_canonicalizes_dangling_timestamp_query(self) -> None:
        self.assertEqual(
            canonicalize_youtube_url(
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t"
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    def test_canonicalizes_timestamped_short_url(self) -> None:
        self.assertEqual(
            canonicalize_youtube_url(
                "https://youtu.be/dQw4w9WgXcQ?si=share-token&t=2061"
            ),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )

    @patch("src.video_urls._public_https_host", return_value="www.youtube.com")
    def test_resolver_never_sends_query_noise_to_gemini(
        self,
        mock_public_host: MagicMock,
    ) -> None:
        metadata_fetcher = MagicMock()

        resolved = resolve_video_url(
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t",
            metadata_fetcher=metadata_fetcher,
        )

        self.assertEqual(
            resolved.uri,
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )
        self.assertEqual(resolved.source, "youtube")
        metadata_fetcher.assert_not_called()
        mock_public_host.assert_called_once()


class PublicHostTests(unittest.TestCase):
    @patch("socket.getaddrinfo")
    def test_rejects_nat64_mapped_loopback(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        # ipaddress calls 64:ff9b::7f00:1 global, but on a NAT64 network it
        # is 127.0.0.1; the explicit prefix deny is what stops it.
        mock_getaddrinfo.return_value = [
            (10, 1, 6, "", ("64:ff9b::7f00:1", 443, 0, 0))
        ]

        with self.assertRaises(VideoResolutionError):
            _public_https_host("https://nat64.example.com/video.mp4")


if __name__ == "__main__":
    unittest.main()
