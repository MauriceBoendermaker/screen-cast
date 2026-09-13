"""How the stream is served matters as much as what is in it."""

from __future__ import annotations

import http.client
import shutil
import tempfile
import unittest
from pathlib import Path

from castlib.streaming import start_http_server


class KeepAlive(unittest.TestCase):
    """A fresh TCP connection per segment cannot feed a distant player.

    The Chromecast here answers pings at ~58ms. On a new connection TCP
    slow-start opens at roughly 14KB and doubles once per round trip, so
    a segment that arrives easily over a warm connection never gets
    going on a cold one. Closing after every response, as HTTP/1.0
    requires, made every single fetch pay that cost.
    """

    PORT = 8791

    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp(prefix="server-test-"))

        (self.directory / "live.m3u8").write_text(
            "#EXTM3U\n#EXT-X-TARGETDURATION:2\n", encoding="utf-8"
        )

        (self.directory / "segment_00000.ts").write_bytes(b"\x47" * 4096)

        self.server = start_http_server(self.directory, self.PORT)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

        shutil.rmtree(self.directory, ignore_errors=True)

    def test_server_speaks_http_1_1(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=5)

        try:
            connection.request("GET", "/live.m3u8")
            response = connection.getresponse()
            response.read()

            self.assertEqual(response.version, 11, "server replied as HTTP/1.0")
        finally:
            connection.close()

    def test_connection_is_not_closed_after_a_response(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=5)

        try:
            connection.request("GET", "/live.m3u8")
            response = connection.getresponse()
            response.read()

            self.assertFalse(
                response.will_close,
                "server hung up, forcing a new connection per request",
            )
        finally:
            connection.close()

    def test_the_same_socket_serves_several_requests(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=5)

        try:
            connection.request("GET", "/live.m3u8")
            connection.getresponse().read()

            first = connection.sock.fileno()

            for _ in range(3):
                connection.request("GET", "/segment_00000.ts")
                body = connection.getresponse().read()

                self.assertEqual(len(body), 4096)

            self.assertEqual(
                connection.sock.fileno(),
                first,
                "a new socket was opened mid-conversation",
            )
        finally:
            connection.close()

    def test_segments_carry_a_content_length(self) -> None:
        """Keep-alive only works if the client knows where a body ends."""
        connection = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=5)

        try:
            connection.request("GET", "/segment_00000.ts")
            response = connection.getresponse()
            response.read()

            self.assertEqual(response.getheader("Content-Length"), "4096")
        finally:
            connection.close()

    def test_a_missing_segment_does_not_wedge_the_connection(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.PORT, timeout=5)

        try:
            connection.request("GET", "/segment_99999.ts")
            response = connection.getresponse()
            response.read()

            self.assertEqual(response.status, 404)

            connection.request("GET", "/live.m3u8")

            self.assertEqual(connection.getresponse().status, 200)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
