"""Small, bounded retries for transient OpenRouter request limits."""
from __future__ import annotations

import io
import json
import time
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


def _transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in {408, 425, 429, 500, 502, 503, 504}:
            return True
        if error.code == 402:
            body = error.read()
            error.fp = io.BytesIO(body)
            try:
                metadata = json.loads(body).get('error', {}).get('metadata', {})
            except (ValueError, AttributeError):
                return False
            return metadata.get('reason') == 'in_flight_budget_exhausted'
        return False
    return isinstance(error, (urllib.error.URLError, ConnectionError, TimeoutError))


def _delay(error: Exception, attempt: int) -> float:
    header = error.headers.get('Retry-After') if isinstance(error, urllib.error.HTTPError) and error.headers else None
    if header:
        try:
            return max(0.0, float(header))
        except ValueError:
            try:
                instant = parsedate_to_datetime(header)
                return max(0.0, (instant - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError):
                pass
    return float(min(30, 2 ** attempt))


def call_with_retries(operation, max_attempts: int = 3):
    for attempt in range(1, max_attempts + 1):
        try:
            return operation()
        except Exception as error:
            if attempt == max_attempts or not _transient(error):
                raise
            time.sleep(_delay(error, attempt))
