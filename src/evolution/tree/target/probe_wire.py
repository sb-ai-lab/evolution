"""Shared stdlib-only framing limits for the mediator and copied probe client."""

from typing import Final

MAX_REQUEST_BYTES: Final = 128 * 1024
# A 4,000-character role body may use six-byte JSON control-character escapes,
# plus the fixed result envelope.
MAX_RESPONSE_BYTES: Final = 32 * 1024
