"""HTTP utilities with retry and exponential backoff."""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any


def post_json(
    url: str,
    payload: Any,
    *,
    retries: int = 5,
    timeout: int = 30,
) -> Any:
    """POST a JSON payload and return the parsed response.

    Retries on HTTP 429 and transient network errors with exponential backoff.

    Parameters
    ----------
    url : str
        The endpoint URL.
    payload : Any
        JSON-serializable request body.
    retries : int
        Maximum number of attempts (default 5).
    timeout : int
        Request timeout in seconds (default 30).

    Returns
    -------
    Parsed JSON response.

    Raises
    ------
    SystemExit
        On non-recoverable HTTP errors or exhausted retries.
    """
    data = json.dumps(payload).encode()
    for attempt in range(retries):
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = 2**attempt
                print(f"[http] 429 rate limited, backing off {wait}s")
                time.sleep(wait)
                continue
            sys.exit(
                f"[http] {e.code} {e.reason}: "
                f"{e.read().decode(errors='replace')}"
            )
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                wait = 2**attempt
                print(f"[http] network error ({e.reason}), retry in {wait}s")
                time.sleep(wait)
                continue
            sys.exit(f"[http] request failed: {e.reason}")
