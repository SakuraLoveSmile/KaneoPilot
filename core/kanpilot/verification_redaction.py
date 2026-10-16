"""Known environment-value redaction scoped to verification subprocesses."""
import re

from . import util


def values(environment, keys=()):
    selected = set(keys)
    return sorted({value for key, value in environment.items() if isinstance(value, str) and value
                   and (any(word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTHORIZATION"))
                        or (key in selected and len(value) >= 8))}, key=len, reverse=True)


def redact(text, secrets):
    if isinstance(text, str) and text and secrets:
        # One substitution avoids replacing inside the redaction marker when a
        # configured sensitive value is only one character long.
        text = re.sub("|".join(re.escape(value) for value in secrets), lambda _: "«redacted»", text)
    return util.redact(text)
