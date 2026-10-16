"""Transport adapters: they run the HTTP request the gate allowed and record
the shape of any failure (connect timeout, reset, refusal, TLS, HTTP status)
the same way whatever the client. The core never imports an HTTP client.
"""
