import unittest
from unittest.mock import MagicMock, patch

from src.video_ingestion import (
    _download_https,
    _https_content_length,
    _https_pdf_head_metadata,
)
from src.video_urls import VideoResolutionError


class Tier2SsrfGuardTests(unittest.TestCase):
    """The Tier-2 helpers take raw applicant-submitted URLs (Alchemist
    pitch-deck links reach them without passing Tier-1's resolver), so
    each must reject non-public destinations itself, before any
    connection is even constructed."""

    @staticmethod
    def _getaddrinfo_returning(address: str):
        def getaddrinfo(host, port):
            # Shape: (family, type, proto, canonname, sockaddr); the guard
            # inspects sockaddr[0].
            return [(2, 1, 6, "", (address, port))]

        return getaddrinfo

    # Patching OpenerDirector (not build_opener) is what proves nothing was
    # dispatched: safe_opener constructs the opener itself instead of asking
    # urllib for a default one.
    @patch("src.video_urls.OpenerDirector")
    def test_download_https_rejects_private_host_before_connecting(
        self, mock_opener_director: MagicMock
    ) -> None:
        with patch(
            "socket.getaddrinfo",
            side_effect=self._getaddrinfo_returning("127.0.0.1"),
        ):
            with self.assertRaises(VideoResolutionError):
                _download_https("https://internal.example.com/video.mp4")
        mock_opener_director.assert_not_called()

    @patch("socket.getaddrinfo")
    def test_download_https_rejects_plain_http_before_dns(
        self, mock_getaddrinfo: MagicMock
    ) -> None:
        # The scheme check fires before any DNS resolution or connection.
        with self.assertRaises(VideoResolutionError):
            _download_https("http://169.254.169.254/latest/meta-data")
        mock_getaddrinfo.assert_not_called()

    @patch("src.video_urls.OpenerDirector")
    @patch("socket.getaddrinfo")
    def test_download_https_rejects_file_scheme(
        self, mock_getaddrinfo: MagicMock, mock_opener_director: MagicMock
    ) -> None:
        # file:// is the scheme the explicit handler set exists to make
        # undispatchable; the scheme check rejects it before that matters.
        with self.assertRaises(VideoResolutionError):
            _download_https("file:///etc/passwd")
        mock_getaddrinfo.assert_not_called()
        mock_opener_director.assert_not_called()

    @patch("src.video_urls.OpenerDirector")
    def test_pdf_head_metadata_rejects_private_host_before_connecting(
        self, mock_opener_director: MagicMock
    ) -> None:
        with patch(
            "socket.getaddrinfo",
            side_effect=self._getaddrinfo_returning("10.0.0.1"),
        ):
            with self.assertRaises(VideoResolutionError):
                _https_pdf_head_metadata("https://internal.example.com/deck.pdf")
        mock_opener_director.assert_not_called()

    @patch("src.video_urls.OpenerDirector")
    def test_content_length_rejects_private_host_before_connecting(
        self, mock_opener_director: MagicMock
    ) -> None:
        with patch(
            "socket.getaddrinfo",
            side_effect=self._getaddrinfo_returning("192.168.1.1"),
        ):
            with self.assertRaises(VideoResolutionError):
                _https_content_length("https://internal.example.com/video.mp4")
        mock_opener_director.assert_not_called()


if __name__ == "__main__":
    unittest.main()
