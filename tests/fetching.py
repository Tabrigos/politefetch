"""A caller that fetches through the gate with plain urllib.

This is what the adapters of stage T5 will do properly; here it is the
smallest honest version, used by the simulated scenarios and the worst-case
counts.
"""

import http.client
import urllib.error
import urllib.request

from politefetch.gate import Attempt, Gate


def fetch(gate: Gate, url: str, *, timeout: float = 0.3) -> Attempt:
    """One request through the gate; raises `Refused` if it may not start."""
    with gate.attempt(url) as attempt:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                response.read()
                status = response.status
                retry_after = response.headers.get("Retry-After")
        except urllib.error.HTTPError as error:
            attempt.response(error.code, retry_after=error.headers.get("Retry-After"))
            error.close()
        except http.client.IncompleteRead:
            attempt.failure("truncated")
        except TimeoutError:
            attempt.failure("timeout")
        except ConnectionResetError:
            attempt.failure("reset")
        except urllib.error.URLError as error:
            reason = error.reason
            attempt.failure(
                "timeout" if isinstance(reason, TimeoutError) else type(reason).__name__
            )
        else:
            if content_type.startswith("text/html"):
                # The source answered, but not with data: treated as a failure,
                # as the content checks of stage T3 will.
                attempt.failure("html")
            else:
                attempt.response(status, retry_after=retry_after)
    return attempt
