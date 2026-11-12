import pytest


def test_brotli_not_importable():
    """
    CVE-2026-111497: The Brotli decoder out-of-bounds write is reachable only
    if a Brotli package is installed, because httpx/requests only advertise and
    decode 'br' when one is present. This test ensures neither brotli nor brotlicffi
    is installed in the runtime environment, making the CVE negative self-enforcing.
    """
    with pytest.raises(ImportError):
        import brotli  # noqa: F401

    with pytest.raises(ImportError):
        import brotlicffi  # noqa: F401
