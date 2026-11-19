"""
Make a VAPID key pair for Web Push.

    python tools/vapid_keys.py

Prints two lines to set as environment variables on the server (Render):
  VAPID_PUBLIC_KEY   — given to the browser when a device subscribes; not a secret
  VAPID_PRIVATE_KEY  — signs every push; a secret. Keep it out of the repository.

Run it once. Changing the keys later ends every device's subscription: each device has to
switch notifications off and on again in Settings.
"""

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def make_pair() -> tuple[str, str]:
    """(public key, private key), both base64url without padding: the uncompressed P-256
    point the browser's ``applicationServerKey`` takes, and the raw 32-byte private value."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(serialization.Encoding.X962,
                                           serialization.PublicFormat.UncompressedPoint)
    private = key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(private)


if __name__ == "__main__":
    public_key, private_key = make_pair()
    print(f"VAPID_PUBLIC_KEY={public_key}")
    print(f"VAPID_PRIVATE_KEY={private_key}")
