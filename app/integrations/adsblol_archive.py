from __future__ import annotations

import gzip
import io
import json
import re
import tarfile
from pathlib import Path


class SplitArchiveReader(io.RawIOBase):
    """Read split archive parts as one forward-only binary stream."""

    def __init__(self, parts):
        super().__init__()
        self.parts = [Path(part) for part in parts]
        if not self.parts:
            raise ValueError("At least one archive part is required.")
        missing = [str(part) for part in self.parts if not part.is_file()]
        if missing:
            raise FileNotFoundError(f"Archive parts not found: {', '.join(missing)}")
        self._index = 0
        self._current = self.parts[0].open("rb")

    def readable(self):
        return True

    def seekable(self):
        return False

    def read(self, size=-1):
        if self.closed:
            raise ValueError("I/O operation on closed archive reader.")
        chunks = []
        remaining = size
        while self._current is not None and (remaining < 0 or remaining > 0):
            chunk = self._current.read(-1 if remaining < 0 else remaining)
            if chunk:
                chunks.append(chunk)
                if remaining > 0:
                    remaining -= len(chunk)
                continue
            self._current.close()
            self._index += 1
            self._current = (
                self.parts[self._index].open("rb")
                if self._index < len(self.parts)
                else None
            )
        return b"".join(chunks)

    def close(self):
        if self._current is not None:
            self._current.close()
            self._current = None
        super().close()


def trace_member_name(icao24: str) -> str:
    icao24 = icao24.strip().lower()
    if len(icao24) != 6 or any(char not in "0123456789abcdef" for char in icao24):
        raise ValueError(f"Invalid ICAO24 identifier: {icao24!r}")
    return f"./traces/{icao24[-2:]}/trace_full_{icao24}.json"


def _header_value(text: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"([^"]*)"', text)
    return match.group(1).strip() if match else None


def discover_trace_metadata(archive_parts, registrations) -> tuple[dict[str, dict], dict]:
    """Find ICAO24 and type metadata by registration without extracting all traces."""
    targets = {str(value).strip().upper() for value in registrations if value}
    found = {}
    with SplitArchiveReader(archive_parts) as reader:
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            for member in archive:
                if not member.isfile() or "/trace_full_" not in member.name:
                    continue
                member_file = archive.extractfile(member)
                if member_file is None:
                    continue
                try:
                    with gzip.GzipFile(fileobj=member_file) as trace_file:
                        header = trace_file.read(4096).decode("utf-8", errors="ignore")
                except (OSError, EOFError):
                    continue
                registration = _header_value(header, "r")
                if not registration or registration.upper() not in targets:
                    continue
                registration = registration.upper()
                found[registration] = {
                    "icao24": (_header_value(header, "icao") or "").lower(),
                    "registration": registration,
                    "typecode": (_header_value(header, "t") or "").upper() or None,
                    "description": _header_value(header, "desc"),
                }
                if len(found) == len(targets):
                    break
    return found, {
        "requested_registrations": len(targets),
        "found_registrations": len(found),
        "missing_registrations": sorted(targets - found.keys()),
    }


def extract_trace_payloads(
    archive_parts,
    icao24s,
    *,
    raw_output_dir: str | Path | None = None,
) -> tuple[dict[str, dict], dict]:
    targets = {icao24.strip().lower() for icao24 in icao24s}
    target_members = {trace_member_name(icao24): icao24 for icao24 in targets}
    payloads = {}
    raw_output = Path(raw_output_dir) if raw_output_dir else None
    if raw_output:
        raw_output.mkdir(parents=True, exist_ok=True)

    with SplitArchiveReader(archive_parts) as reader:
        with tarfile.open(fileobj=reader, mode="r|") as archive:
            for member in archive:
                icao24 = target_members.get(member.name)
                if icao24 is None or not member.isfile():
                    continue
                member_file = archive.extractfile(member)
                if member_file is None:
                    continue
                compressed = member_file.read()
                if raw_output:
                    (raw_output / f"trace_full_{icao24}.json.gz").write_bytes(
                        compressed
                    )
                payloads[icao24] = json.loads(gzip.decompress(compressed))
                if len(payloads) == len(targets):
                    break

    return payloads, {
        "requested_aircraft": len(targets),
        "found_aircraft": len(payloads),
        "missing_icao24": sorted(targets - payloads.keys()),
    }
