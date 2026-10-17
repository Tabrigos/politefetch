"""A caller that fetches through the gate with plain urllib.

This is what the adapters of stage T5 will do properly; here it is the
smallest honest version, used by the simulated scenarios and the worst-case
counts. It sends the conditional headers the gate remembers and records the
body bytes (urllib does not decompress, so wire and decoded bytes match).
"""

import http.client
import urllib.error
import urllib.request

from politefetch.gate import Attempt, Gate


def fetch(gate: Gate, url: str, *, timeout: float = 0.3) -> Attempt:
    """One request through the gate; raises `Refused` if it may not start."""
    with gate.attempt(url) as attempt:
        request = urllib.request.Request(url, headers=attempt.conditional_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                headers = response.headers
                body = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            body = error.read()
            attempt.response(
                error.code,
                headers=error.headers,
                wire_bytes=len(body),
                decoded_bytes=len(body),
            )
            error.close()
        except http.client.IncompleteRead as error:
            attempt.failure("truncated", wire_bytes=len(error.partial))
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
            if headers.get("Content-Type", "").startswith("text/html"):
                # The source answered, but not with data: treated as a failure,
                # as the content checks of stage T3 will.
                attempt.failure("html", wire_bytes=len(body))
            else:
                attempt.response(
                    status,
                    headers=headers,
                    wire_bytes=len(body),
                    decoded_bytes=len(body),
                )
    return attempt
