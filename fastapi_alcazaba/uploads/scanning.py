"""Scanning uploads for malware: `FastAPIUploads(scanner=...)` runs a scanner on each file once
its size and type are checked. `ClamAVScanner` talks to clamd (ClamAV's daemon) over TCP or a
Unix socket, with nothing more installed: pip install "fastapi-alcazaba[clamav]".

    uploads = FastAPIUploads(types=OFFICE_DOCUMENTS, scanner=ClamAVScanner("tcp://clamav:3310"))

A scanner only knows the malware it has signatures for: a script in a PDF or a macro written
for the target goes through. Keep it, and its signatures (freshclam), up to date.
"""

import asyncio
import struct
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import UploadFile

#: Bytes sent to clamd at a time.
CHUNK_SIZE = 64 * 1024
#: The longest answer read from clamd.
_MAX_REPLY = 4096


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What a scanner found: nothing (`clean`), or the threat's name."""

    clean: bool
    threat: str | None = None


class ScannerUnavailable(Exception):
    """The scanner didn't answer, or not with a verdict: the file wasn't scanned."""


class Scanner(Protocol):
    """Scans a file: `FastAPIUploads` gives it the upload at its start, and reads it from the
    start again afterwards. ScannerUnavailable (or any other exception, or the timeout) when it
    can't tell."""

    async def scan(self, file: UploadFile) -> ScanResult: ...


class ClamAVScanner:
    """Scans with clamd's INSTREAM command: the file goes to the daemon in chunks, never to a
    shared disk.

    :param address: where clamd listens: `tcp://host:3310`, or `unix:///run/clamav/clamd.ctl`
        (a bare path too).
    :param timeout: seconds for connecting, and for the whole scan.
    """

    def __init__(self, address: str, *, timeout: float = 30.0) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        parts = urlsplit(address)
        if parts.scheme == "tcp":
            if not parts.hostname or parts.port is None:
                raise ValueError(f"a TCP address is tcp://host:port, not {address!r}")
            self._tcp: tuple[str, int] | None = (parts.hostname, parts.port)
            self._path = ""
        elif parts.scheme in ("unix", "") and (parts.path or address):
            self._tcp, self._path = None, parts.path if parts.scheme else address
        else:
            raise ValueError(f"clamd's address is tcp://host:port or unix:///path: {address!r}")
        self.address = address
        self.timeout = timeout

    async def ping(self) -> None:
        """Ask clamd for a PONG; ScannerUnavailable when it doesn't answer one (for a health
        check)."""
        reply = await self._command(b"zPING\0", None)
        if reply != "PONG":
            raise ScannerUnavailable(f"clamd answered {reply!r} to PING")

    async def scan(self, file: UploadFile) -> ScanResult:
        return await self.scan_chunks(_chunks(file))

    async def scan_chunks(self, chunks: AsyncIterator[bytes]) -> ScanResult:
        """Scan what `chunks` yield, as one file."""
        reply = await self._command(b"zINSTREAM\0", chunks)
        # "stream: OK", "stream: <name> FOUND", or an error ("... ERROR").
        verdict = reply.removeprefix("stream:").strip()
        if verdict == "OK":
            return ScanResult(clean=True)
        if verdict.endswith(" FOUND"):
            return ScanResult(clean=False, threat=verdict.removesuffix(" FOUND").strip())
        raise ScannerUnavailable(f"clamd couldn't scan the file: {reply[:200]!r}")

    async def _command(self, command: bytes, chunks: AsyncIterator[bytes] | None) -> str:
        try:
            async with asyncio.timeout(self.timeout):
                if self._tcp is not None:
                    reader, writer = await asyncio.open_connection(*self._tcp)
                else:
                    reader, writer = await asyncio.open_unix_connection(self._path)
                try:
                    writer.write(command)
                    if chunks is not None:
                        async for chunk in chunks:
                            writer.write(struct.pack("!I", len(chunk)) + chunk)
                            await writer.drain()
                        writer.write(struct.pack("!I", 0))
                    await writer.drain()
                    reply = await reader.readuntil(b"\0")
                finally:
                    writer.close()
                    await writer.wait_closed()
        except (
            OSError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            raise ScannerUnavailable(f"clamd at {self.address} didn't answer: {exc!r}") from exc
        return reply[:-1][:_MAX_REPLY].decode("utf-8", "replace")


async def _chunks(file: UploadFile) -> AsyncIterator[bytes]:
    while chunk := await file.read(CHUNK_SIZE):
        yield chunk
