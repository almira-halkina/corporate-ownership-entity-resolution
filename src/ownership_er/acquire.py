"""Downloading source snapshots, with a manifest for provenance.

Every download records URL, resolved snapshot date, byte size, SHA-256 and
fetch timestamp into ``data/raw/manifest.json``. That is not bookkeeping for
its own sake: these sources are *mutable*. Companies House replaces the PSC
snapshot every morning, and OpenSanctions rebuilds continuously. A figure
reported from "the PSC snapshot" without a date and a hash is not reproducible,
and in this domain — where the output is a claim about who controls a company —
being unable to say which version of the register a claim came from makes the
claim unusable.

Downloads resume and verify. The PSC snapshot is several GB, and a truncated
file that parses as valid JSON lines up to the cut point is the worst possible
failure: it produces plausible, quietly incomplete results.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

from ownership_er.config import Settings, get_settings

__all__ = [
    "SnapshotRecord",
    "download",
    "fetch_companies_house",
    "fetch_opensanctions",
    "read_manifest",
    "resolve_psc_snapshot_date",
]


@dataclass(slots=True)
class SnapshotRecord:
    """Provenance for one downloaded artefact."""

    source: str
    url: str
    path: str
    bytes: int
    sha256: str
    fetched_at: str
    snapshot_date: str | None = None


def _sha256(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def _manifest_path(settings: Settings) -> Path:
    return settings.paths.raw / "manifest.json"


def read_manifest(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    path = _manifest_path(settings)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _record_manifest(settings: Settings, record: SnapshotRecord) -> None:
    path = _manifest_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(settings)
    manifest[record.source] = asdict(record)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def download(
    url: str,
    dest: Path,
    *,
    settings: Settings | None = None,
    source: str = "unknown",
    snapshot_date: str | None = None,
    force: bool = False,
) -> SnapshotRecord:
    """Stream a URL to disk, then hash and record it.

    Writes to a ``.part`` file and renames only on success, so an interrupted
    download can never be mistaken for a complete one on the next run.
    """
    settings = settings or get_settings()
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        record = SnapshotRecord(
            source=source,
            url=url,
            path=str(dest),
            bytes=dest.stat().st_size,
            sha256=_sha256(dest),
            fetched_at=date.today().isoformat(),
            snapshot_date=snapshot_date,
        )
        _record_manifest(settings, record)
        return record

    partial = dest.with_suffix(dest.suffix + ".part")
    last_error: Exception | None = None
    for attempt in range(settings.sources.max_retries):
        try:
            with requests.get(
                url, stream=True, timeout=settings.sources.request_timeout
            ) as response:
                response.raise_for_status()
                expected = int(response.headers.get("Content-Length") or 0)
                written = 0
                with partial.open("wb") as fh:
                    for chunk in response.iter_content(chunk_size=1 << 20):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
                if expected and written != expected:
                    raise OSError(f"truncated download: got {written:,} of {expected:,} bytes")
            partial.replace(dest)
            break
        except Exception as exc:  # network, truncation, HTTP error
            last_error = exc
            partial.unlink(missing_ok=True)
            if attempt < settings.sources.max_retries - 1:
                time.sleep(2**attempt)
    else:
        raise RuntimeError(f"failed to download {url}: {last_error}")

    record = SnapshotRecord(
        source=source,
        url=url,
        path=str(dest),
        bytes=dest.stat().st_size,
        sha256=_sha256(dest),
        fetched_at=date.today().isoformat(),
        snapshot_date=snapshot_date,
    )
    _record_manifest(settings, record)
    return record


def resolve_psc_snapshot_date(settings: Settings | None = None) -> str:
    """Find the most recent available PSC snapshot date.

    Companies House publishes before 10am GMT and keeps only a short window, so
    the useful date is 'yesterday, or the most recent day that exists'. Probed
    with HEAD requests rather than assumed, because guessing wrong produces a
    404 several minutes into a multi-gigabyte job.
    """
    settings = settings or get_settings()
    if settings.sources.snapshot_date:
        return settings.sources.snapshot_date

    today = date.today()
    for delta in range(0, 8):
        candidate = (today - timedelta(days=delta)).isoformat()
        url = settings.sources.psc_snapshot_template.format(date=candidate)
        try:
            response = requests.head(url, timeout=30, allow_redirects=True)
            if response.status_code == 200:
                return candidate
        except requests.RequestException:
            continue
    raise RuntimeError(
        "No PSC snapshot found in the last 8 days. Check "
        "https://download.companieshouse.gov.uk/en_pscdata.html and set "
        "OER_SOURCE_SNAPSHOT_DATE explicitly."
    )


def fetch_companies_house(
    settings: Settings | None = None, *, force: bool = False
) -> dict[str, SnapshotRecord]:
    """Download the company snapshot and the PSC snapshot."""
    settings = settings or get_settings()
    settings.paths.ensure()
    snapshot_date = resolve_psc_snapshot_date(settings)

    psc_url = settings.sources.psc_snapshot_template.format(date=snapshot_date)
    psc = download(
        psc_url,
        settings.paths.raw / f"psc-snapshot-{snapshot_date}.zip",
        settings=settings,
        source="ch_psc",
        snapshot_date=snapshot_date,
        force=force,
    )

    # The company product is published on the 1st of each month.
    first_of_month = date.today().replace(day=1).isoformat()
    company_url = settings.sources.company_snapshot_template.format(date=first_of_month)
    company = download(
        company_url,
        settings.paths.raw / f"companies-{first_of_month}.zip",
        settings=settings,
        source="ch_companies",
        snapshot_date=first_of_month,
        force=force,
    )
    return {"ch_psc": psc, "ch_companies": company}


def fetch_opensanctions(settings: Settings | None = None, *, force: bool = False) -> SnapshotRecord:
    """Download the OpenSanctions consolidated default dataset."""
    settings = settings or get_settings()
    settings.paths.ensure()
    return download(
        settings.sources.opensanctions_url,
        settings.paths.raw / "opensanctions-default.ftm.json",
        settings=settings,
        source="opensanctions",
        snapshot_date=date.today().isoformat(),
        force=force,
    )
