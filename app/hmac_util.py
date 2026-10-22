import hmac
from hashlib import sha256


def sign_body(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, sha256).hexdigest()


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = sign_body(secret, body)
    # Header values are not guaranteed to be ASCII. Comparing str values with
    # non-ASCII characters raises TypeError instead of rejecting the signature.
    return hmac.compare_digest(expected.encode("ascii"), signature.encode("utf-8"))
