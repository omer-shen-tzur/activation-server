"""Per-device keys and request signatures: the ONE module for both.

The server uses it to verify, and the simulator (step 6) imports it to sign
like a real device, so the two can never disagree on the scheme.

    device_key = HMAC-SHA256(MASTER_SECRET, normalized MAC)
    X-Signature = hex HMAC-SHA256(device_key, raw request body)

Why a per-device key instead of one shared API key: a device's firmware and
flash can be read by anyone holding the device. With a shared key, one
extracted key would let an attacker sign activations for the whole fleet.
With a per-device key it only signs for that one MAC; the master secret
stays on the server (in production the device keys would be provisioned at
the factory).

Why no timestamp or replay window: replaying a captured activation is a
no-op, because the endpoint is idempotent (it adds a duplicate, never a
second count). And a device on its first boot often has no reliable clock
yet, so a time window would reject exactly the activations we must count.

The signature covers the exact bytes received, never a re-serialized body:
re-serializing can change whitespace, key order or number formatting, and
the signature would then fail for a correct device.

Manual check:  python -m app.signing 00:1A:2B:3C:4D:5E
prints a body on the first line and its X-Signature on the second.
"""

import hashlib
import hmac
import json
import sys

from app.config import ConfigError, load_settings
from app.schemas import normalize_mac


def device_key(master_secret: str, mac: str) -> bytes:
    """The device's key. Raises ValueError if mac is not an accepted device MAC.

    The MAC is normalized here, so a device and the server derive the same
    key from any accepted form (aa-bb-..., AA:BB:..., 12 hex digits).
    """
    return hmac.new(
        master_secret.encode("utf-8"),
        normalize_mac(mac).encode("utf-8"),
        hashlib.sha256,
    ).digest()


def sign(key: bytes, body: bytes) -> str:
    """Lowercase hex HMAC-SHA256 of the exact body bytes."""
    return hmac.new(key, body, hashlib.sha256).hexdigest()


def verify(master_secret: str, mac: str, body: bytes, signature: str | None) -> bool:
    """True if signature is the right X-Signature for this MAC and body."""
    if signature is None:
        return False
    expected = sign(device_key(master_secret, mac), body).encode("ascii")
    # Compared as bytes: compare_digest on str raises TypeError for
    # non-ASCII text, which would turn a garbage header into a 500. Header
    # values arrive decoded as latin-1, so encoding back to latin-1 always
    # works. compare_digest takes the same time wherever the first
    # difference is, so the response time does not leak the right value.
    received = signature.strip().lower().encode("latin-1")
    return hmac.compare_digest(expected, received)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m app.signing <MAC>", file=sys.stderr)
        return 2
    mac = argv[0]
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        key = device_key(settings.master_secret, mac)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    body = json.dumps({"device_id": mac, "model": "X100"}, separators=(",", ":")).encode("utf-8")
    # json.dumps escapes non-ASCII, so these printed characters are exactly
    # the bytes that were signed.
    print(body.decode("ascii"))
    print(sign(key, body))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
