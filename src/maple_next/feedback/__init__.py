"""Match Feedback Loop v1: MATCH -> EXPORT -> PUBLISH -> ChatGPT review transport.

Publishes the existing, unmodified canonical ``maple-match`` export to GitHub
so it can be reviewed externally. This package never redefines the match
export schema or its safety contract (see
``maple_next.application.match_export_v3``); it only validates, queues, and
transports the exact bytes that exporter already wrote. It never mutates
battle state, never calls a provider (Gemini) or OCR, never touches the
database, and a GitHub failure never blocks the local match export.
"""

from __future__ import annotations
