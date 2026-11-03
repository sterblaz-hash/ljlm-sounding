"""Bounded, retry-aware DWD Open Data retrieval.

No bulk download of unrelated endpoints: callers pass a known listing or
BUFR file URL. Transient failures retry; malformed/oversized responses fail.
"""

from __future__ import annotations

import socket
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from http.client import IncompleteRead, RemoteDisconnected
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


MAX_ATTEMPTS = 3
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
RETRY_DELAYS = (2, 6)


def retry_delay(exc, attempt):
    default = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
    if not isinstance(exc, HTTPError) or exc.code != 429:
        return default
    header = exc.headers.get("Retry-After") if exc.headers else None
    if not header:
        return default
    try:
        value = float(header)
    except (ValueError, TypeError):
        try:
            value = (parsedate_to_datetime(header).astimezone(timezone.utc)
                     - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return default
    # Do not violate server Retry-After. A longer pause is better handled by
    # a later scheduled job than by blocking an Actions runner.
    if value > 30:
        return None
    return max(default, value)


def download_dwd(url, *, opener=urlopen, sleeper=time.sleep, logger=print,
                 max_attempts=MAX_ATTEMPTS, max_bytes=MAX_RESPONSE_BYTES):
    if max_attempts < 1 or max_bytes < 1:
        raise ValueError("retry and size budgets must be positive")
    request = Request(url, headers={"User-Agent": "ljlm-sounding/2.0"})
    for attempt in range(1, max_attempts + 1):
        try:
            with opener(request, timeout=30) as response:
                headers = getattr(response, "headers", {})
                size = headers.get("Content-Length") if headers else None
                if size is not None and int(size) > max_bytes:
                    raise ValueError("DWD response exceeds bounded size budget")
                # Use an upper bound even if Content-Length is absent/wrong.
                data = response.read(max_bytes + 1)
                if len(data) > max_bytes:
                    raise ValueError("DWD response exceeded bounded size budget")
                return data
        except HTTPError as exc:
            should_retry = exc.code in RETRYABLE_HTTP
            if not should_retry or attempt >= max_attempts:
                raise
            delay = retry_delay(exc, attempt)
            if delay is None:
                logger("DWD rate limit requests more than 30s; not retrying now")
                raise
            logger(f"DWD HTTP {exc.code} attempt {attempt}/{max_attempts}; "
                   f"retrying after {delay:.0f}s")
            sleeper(delay)
        except (URLError, TimeoutError, socket.timeout, ConnectionError,
                IncompleteRead, RemoteDisconnected) as exc:
            if attempt >= max_attempts:
                raise
            delay = RETRY_DELAYS[min(attempt - 1, len(RETRY_DELAYS) - 1)]
            logger(f"DWD network attempt {attempt}/{max_attempts} failed: "
                   f"{exc}; retrying after {delay}s")
            sleeper(delay)
    raise RuntimeError("unreachable DWD retry state")
