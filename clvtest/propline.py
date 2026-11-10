"""Client PropLine: X-API-Key în header, respectă 429/Retry-After, numără cererile local."""
from __future__ import annotations

import gzip
import hashlib
import json
import time
from dataclasses import dataclass, field

import httpx

from .config import PROPLINE_BASE

MIN_INTERVAL_S = 0.15        # sub limita de 10/s, cu marjă
MAX_ATTEMPTS = 5


class HTTPStatusError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class Response:
    endpoint: str
    params: dict
    status: int
    headers: dict
    body: bytes
    data: object

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body).hexdigest()

    def gzipped(self) -> bytes:
        return gzip.compress(self.body, compresslevel=6)


@dataclass
class PropLine:
    api_key: str
    requests: int = 0
    daily_remaining: int | None = None
    _last: float = 0.0
    _client: httpx.Client = field(default=None, repr=False)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self._client = httpx.Client(
            base_url=PROPLINE_BASE,
            headers={"X-API-Key": self.api_key, "Accept": "application/json",
                     "User-Agent": "clv-paper-test/0.1"},
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    def close(self) -> None:
        self._client.close()

    def get(self, endpoint: str, params: dict | None = None) -> Response:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        last_err: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            wait = MIN_INTERVAL_S - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = self._client.get(endpoint, params=params)
            except httpx.TransportError as e:          # rețea: reîncearcă cu așteptare crescătoare
                last_err = e
                time.sleep(2 ** attempt)
                continue
            self.requests += 1
            rem = r.headers.get("x-daily-remaining")
            if rem is not None and rem.isdigit():
                self.daily_remaining = int(rem)
            if r.status_code == 429:
                time.sleep(min(float(r.headers.get("retry-after") or 2 ** attempt), 120))
                continue
            if r.status_code >= 500:
                last_err = RuntimeError(f"{endpoint} -> HTTP {r.status_code}")
                time.sleep(2 ** attempt)
                continue
            if r.status_code != 200:
                raise HTTPStatusError(r.status_code, f"{endpoint} {params} -> HTTP {r.status_code}: {r.text[:300]}")
            return Response(
                endpoint=endpoint, params=params, status=r.status_code,
                headers={k: v for k, v in r.headers.items() if k.lower().startswith(("x-", "ratelimit"))},
                body=r.content, data=json.loads(r.content),
            )
        raise RuntimeError(f"{endpoint} {params}: eșuat după {MAX_ATTEMPTS} încercări ({last_err})")
