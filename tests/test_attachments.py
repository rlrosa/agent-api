import base64
import hashlib
import os
import tempfile
import pytest
from app.attachments import (
    compose_prompt,
    fetch_url_attachment,
    materialize_attachment,
    materialize_base64_attachment,
    materialize_bytes_attachment,
    sanitize_filename,
    validate_url_ssrf,
)


def test_ssrf_guard():
    with pytest.raises(ValueError, match="SSRF guard"):
        validate_url_ssrf("http://127.0.0.1:8090/healthz")

    with pytest.raises(ValueError, match="SSRF guard"):
        validate_url_ssrf("http://169.254.169.254/latest/meta-data/")

    with pytest.raises(ValueError, match="SSRF guard"):
        validate_url_ssrf("http://10.0.0.1/internal")

    with pytest.raises(ValueError, match="SSRF guard"):
        validate_url_ssrf("file:///etc/passwd")

    # Allowed public host
    host = validate_url_ssrf("https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf")
    assert host == "www.w3.org"


def test_filename_traversal_sanitization(tmp_path):
    attachments_dir = str(tmp_path / "attachments")
    os.makedirs(attachments_dir, exist_ok=True)

    f1 = sanitize_filename("../../etc/passwd")
    assert f1 == "passwd"

    f2 = sanitize_filename("/etc/passwd")
    assert f2 == "passwd"

    f3 = sanitize_filename("null\x00byte.txt")
    assert f3 == "nullbyte.txt"

    # Materialize bytes with traversal filename
    res_name, size = materialize_bytes_attachment(
        "../../etc/passwd",
        b"secret data",
        attachments_dir,
        max_attachment_bytes=1000,
        total_bytes_written=0,
        max_total_bytes=5000,
    )
    assert res_name == "passwd"
    expected_path = os.path.join(attachments_dir, "passwd")
    assert os.path.exists(expected_path)
    assert not os.path.exists("/etc/passwd_test_fake")


def test_base64_and_bytes_intake(tmp_path):
    attachments_dir = str(tmp_path / "attachments")
    os.makedirs(attachments_dir, exist_ok=True)

    content = b"The quick brown fox jumps over the lazy dog"
    expected_sha256 = hashlib.sha256(content).hexdigest()

    # Base64 path
    b64_str = base64.b64encode(content).decode("utf-8")
    fname_b64, size_b64 = materialize_base64_attachment(
        "sample.txt",
        b64_str,
        attachments_dir,
        max_attachment_bytes=1000,
        total_bytes_written=0,
        max_total_bytes=5000,
    )
    assert fname_b64 == "sample.txt"
    with open(os.path.join(attachments_dir, fname_b64), "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == expected_sha256

    # Raw bytes path
    fname_bytes, size_bytes = materialize_bytes_attachment(
        "sample_raw.txt",
        content,
        attachments_dir,
        max_attachment_bytes=1000,
        total_bytes_written=0,
        max_total_bytes=5000,
    )
    assert fname_bytes == "sample_raw.txt"
    with open(os.path.join(attachments_dir, fname_bytes), "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == expected_sha256


def test_compose_prompt():
    prompt = "Summarize this document"
    files = ["doc.pdf", "photo.png"]
    composed = compose_prompt(prompt, files)

    expected = (
        "Summarize this document\n\n"
        "Attached files (read them from disk as needed):\n"
        "- ./attachments/doc.pdf\n"
        "- ./attachments/photo.png"
    )
    assert composed == expected
