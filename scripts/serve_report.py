"""Serve the offline report locally with byte-range support for video seeking.

The report itself remains a standalone index.html plus relative assets. This
optional loopback-only preview server is useful for browser-based verification.
"""
from __future__ import annotations

import argparse
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re


class RangeHandler(SimpleHTTPRequestHandler):
    """Support a single HTTP byte range without loading videos into memory."""

    def list_directory(self, path):
        self.send_error(HTTPStatus.NOT_FOUND, "No report index at this location")
        return None

    def send_head(self):
        self._bytes_remaining = None
        path = Path(self.translate_path(self.path))
        if not path.resolve().is_relative_to(Path(self.directory).resolve()):
            self.send_error(HTTPStatus.FORBIDDEN)
            return None
        if path.is_dir():
            return super().send_head()
        try:
            stream = path.open("rb")
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return None
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested.strip())
            valid = match is not None and bool(match.group(1) or match.group(2)) and size > 0
            if valid:
                first, last = match.groups()
                if first:
                    start = int(first)
                    end = min(int(last), size - 1) if last else size - 1
                else:
                    length = int(last)
                    start = max(0, size - length)
                    valid = length > 0
                valid = valid and 0 <= start <= end < size
            if not valid:
                stream.close()
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
        self.send_response(HTTPStatus.PARTIAL_CONTENT if requested else HTTPStatus.OK)
        self.send_header("Content-Type", self.guess_type(str(path)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Last-Modified", self.date_time_string(path.stat().st_mtime))
        self.send_header("Cache-Control", "no-cache")
        if requested:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        stream.seek(start)
        self._bytes_remaining = end - start + 1
        return stream

    def copyfile(self, source, outputfile):
        remaining = getattr(self, "_bytes_remaining", None)
        if remaining is None:
            return super().copyfile(source, outputfile)
        try:
            while remaining > 0:
                block = source.read(min(remaining, 64 * 1024))
                if not block:
                    break
                outputfile.write(block)
                remaining -= len(block)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Browsers routinely cancel old requests when the user seeks.
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8795)
    parser.add_argument("--directory", type=Path,
                        default=Path(__file__).resolve().parents[1] / "artifacts/report")
    args = parser.parse_args(argv)
    directory = args.directory.resolve()
    if not (directory / "index.html").is_file():
        parser.error(f"Report index is missing: {directory / 'index.html'}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(RangeHandler, directory=str(directory)))
    print(f"KiroClops report: http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
