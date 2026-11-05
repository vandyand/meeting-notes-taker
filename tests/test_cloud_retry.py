import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cloud_retry


class CloudRetryTests(unittest.TestCase):
    def test_429_honors_retry_after_then_succeeds(self):
        error = urllib.error.HTTPError('https://test', 429, 'limited', {'Retry-After': '3'}, io.BytesIO(b'{}'))
        attempts = iter([error, 'ok'])
        def operation():
            result = next(attempts)
            if isinstance(result, Exception):
                raise result
            return result
        with mock.patch.object(cloud_retry.time, 'sleep') as sleep:
            self.assertEqual(cloud_retry.call_with_retries(operation), 'ok')
        sleep.assert_called_once_with(3.0)

    def test_only_in_flight_402_is_retried(self):
        body = json.dumps({'error': {'metadata': {'reason': 'in_flight_budget_exhausted'}}}).encode()
        error = urllib.error.HTTPError('https://test', 402, 'budget', {'Retry-After': '2'}, io.BytesIO(body))
        calls = 0
        def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise error
            return 'ok'
        with mock.patch.object(cloud_retry.time, 'sleep') as sleep:
            self.assertEqual(cloud_retry.call_with_retries(operation), 'ok')
        self.assertEqual(calls, 2)
        sleep.assert_called_once_with(2.0)

    def test_insufficient_credit_402_stops_without_retry(self):
        error = urllib.error.HTTPError('https://test', 402, 'credits', {}, io.BytesIO(b'{"error":{"metadata":{"limit_source":"openrouter_credits"}}}'))
        with mock.patch.object(cloud_retry.time, 'sleep') as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                cloud_retry.call_with_retries(lambda: (_ for _ in ()).throw(error))
        sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
