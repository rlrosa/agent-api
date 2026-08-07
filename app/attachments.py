import base64
import ipaddress
import mimetypes
import os
import re
import socket
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple, Union
import httpx
from app.config import get_settings


def validate_url_ssrf(url: str, allowlist: Optional[List[str]] = None) -> str:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"SSRF guard: Invalid scheme '{parsed.scheme}'. Only http and https are allowed.")

    hostname = parsed.hostname
    if not hostname:
        raise ValueError("SSRF guard: Invalid URL, hostname is missing.")

    # Check explicit allowlist
    if allowlist:
        clean_allowlist = {h.strip().lower() for h in allowlist if h.strip()}
        if hostname.lower() in clean_allowlist:
            return hostname

    # Try parsing direct IP
    try:
        ip = ipaddress.ip_address(hostname)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"SSRF guard: IP address {hostname} is in a restricted range.")
        return hostname
    except ValueError as exc:
        if "restricted range" in str(exc):
            raise
        # Not a direct IP literal, resolve hostname via DNS
        pass

    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"SSRF guard: Failed to resolve hostname '{hostname}': {e}")

    for family, _, _, _, sockaddr in addr_info:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise ValueError(f"SSRF guard: Host '{hostname}' resolves to restricted IP {ip_str}.")
        except ValueError as exc:
            if "restricted IP" in str(exc):
                raise

    return hostname


def sanitize_filename(filename: str, content_type: Optional[str] = None) -> str:
    # 1. Remove null bytes
    filename = filename.replace("\x00", "")
    # 2. Convert backslashes and extract basename
    filename = os.path.basename(filename.replace("\\", "/"))
    # 3. Strip leading dots / spaces
    filename = filename.lstrip(". ").strip()
    if not filename:
        filename = "attachment"

    name_part, ext_part = os.path.splitext(filename)
    if not ext_part and content_type:
        inferred = mimetypes.guess_extension(content_type.split(";")[0].strip())
        if inferred:
            ext_part = inferred
            filename = name_part + ext_part

    return filename


def get_unique_filepath(attachments_dir: str, filename: str) -> str:
    base_name, ext = os.path.splitext(filename)
    target_path = os.path.join(attachments_dir, filename)
    counter = 1
    while os.path.exists(target_path):
        target_path = os.path.join(attachments_dir, f"{base_name}_{counter}{ext}")
        counter += 1

    abs_attachments = os.path.abspath(attachments_dir)
    abs_target = os.path.abspath(target_path)
    if not abs_target.startswith(abs_attachments + os.sep) and abs_target != abs_attachments:
        raise ValueError("Path traversal attempt detected")

    return target_path


def get_filename_from_response(response: httpx.Response, url: str) -> str:
    cd = response.headers.get("content-disposition", "")
    if cd:
        match = re.search(r'filename\*?=(?:["\']?([^"\';]+)["\']?|UTF-8\'\'(.+))', cd, re.IGNORECASE)
        if match:
            fn = match.group(2) or match.group(1)
            if fn:
                return urllib.parse.unquote(fn)

    parsed = urllib.parse.urlparse(url)
    path_name = os.path.basename(parsed.path)
    if path_name:
        return path_name

    return "downloaded_file"


async def fetch_url_attachment(
    url: str,
    attachments_dir: str,
    max_attachment_bytes: int,
    total_bytes_written: int,
    max_total_bytes: int,
    allowlist: Optional[List[str]] = None,
) -> Tuple[str, int]:
    validate_url_ssrf(url, allowlist=allowlist)

    async with httpx.AsyncClient(follow_redirects=True, max_redirects=5, timeout=10.0) as client:
        async with client.stream("GET", url) as response:
            if response.status_code != 200:
                raise ValueError(f"Failed to fetch attachment URL {url}: HTTP {response.status_code}")

            content_type = response.headers.get("content-type")
            raw_filename = get_filename_from_response(response, url)
            clean_filename = sanitize_filename(raw_filename, content_type=content_type)
            target_path = get_unique_filepath(attachments_dir, clean_filename)

            bytes_written_this_file = 0
            temp_path = target_path + ".tmp"

            try:
                with open(temp_path, "wb") as f:
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        chunk_len = len(chunk)
                        if bytes_written_this_file + chunk_len > max_attachment_bytes:
                            raise ValueError(
                                f"Attachment exceeds max single file size limit ({max_attachment_bytes} bytes). Aborted after {bytes_written_this_file + chunk_len} bytes."
                            )
                        if total_bytes_written + bytes_written_this_file + chunk_len > max_total_bytes:
                            raise ValueError(
                                f"Job total attachments size exceeds limit ({max_total_bytes} bytes). Aborted after {total_bytes_written + bytes_written_this_file + chunk_len} bytes."
                            )
                        f.write(chunk)
                        bytes_written_this_file += chunk_len

                os.rename(temp_path, target_path)
                return os.path.basename(target_path), bytes_written_this_file
            except Exception:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise


def materialize_base64_attachment(
    filename: str,
    content_b64: str,
    attachments_dir: str,
    max_attachment_bytes: int,
    total_bytes_written: int,
    max_total_bytes: int,
) -> Tuple[str, int]:
    clean_filename = sanitize_filename(filename)
    target_path = get_unique_filepath(attachments_dir, clean_filename)

    try:
        data = base64.b64decode(content_b64)
    except Exception as e:
        raise ValueError(f"Invalid base64 payload: {e}")

    data_len = len(data)
    if data_len > max_attachment_bytes:
        raise ValueError(f"Attachment exceeds max single file size limit ({max_attachment_bytes} bytes)")
    if total_bytes_written + data_len > max_total_bytes:
        raise ValueError(f"Job total attachments size exceeds limit ({max_total_bytes} bytes)")

    with open(target_path, "wb") as f:
        f.write(data)

    return os.path.basename(target_path), data_len


def materialize_bytes_attachment(
    filename: str,
    content_bytes: bytes,
    attachments_dir: str,
    max_attachment_bytes: int,
    total_bytes_written: int,
    max_total_bytes: int,
) -> Tuple[str, int]:
    clean_filename = sanitize_filename(filename)
    target_path = get_unique_filepath(attachments_dir, clean_filename)

    data_len = len(content_bytes)
    if data_len > max_attachment_bytes:
        raise ValueError(f"Attachment exceeds max single file size limit ({max_attachment_bytes} bytes)")
    if total_bytes_written + data_len > max_total_bytes:
        raise ValueError(f"Job total attachments size exceeds limit ({max_total_bytes} bytes)")

    with open(target_path, "wb") as f:
        f.write(content_bytes)

    return os.path.basename(target_path), data_len


async def materialize_attachment(
    spec: Dict[str, Any],
    workspace_path: str,
    total_bytes_so_far: int = 0,
    allowlist: Optional[List[str]] = None,
) -> Tuple[str, int]:
    settings = get_settings()
    attachments_dir = os.path.join(workspace_path, "attachments")
    os.makedirs(attachments_dir, exist_ok=True)

    max_attachment_bytes = settings.max_attachment_bytes
    max_total_bytes = settings.max_total_bytes

    if "url" in spec:
        filename, size = await fetch_url_attachment(
            url=spec["url"],
            attachments_dir=attachments_dir,
            max_attachment_bytes=max_attachment_bytes,
            total_bytes_written=total_bytes_so_far,
            max_total_bytes=max_total_bytes,
            allowlist=allowlist,
        )
        return filename, size
    elif "content_b64" in spec:
        filename = spec.get("filename", "attachment.bin")
        filename, size = materialize_base64_attachment(
            filename=filename,
            content_b64=spec["content_b64"],
            attachments_dir=attachments_dir,
            max_attachment_bytes=max_attachment_bytes,
            total_bytes_written=total_bytes_so_far,
            max_total_bytes=max_total_bytes,
        )
        return filename, size
    elif "bytes" in spec:
        filename = spec.get("filename", "attachment.bin")
        filename, size = materialize_bytes_attachment(
            filename=filename,
            content_bytes=spec["bytes"],
            attachments_dir=attachments_dir,
            max_attachment_bytes=max_attachment_bytes,
            total_bytes_written=total_bytes_so_far,
            max_total_bytes=max_total_bytes,
        )
        return filename, size
    else:
        raise ValueError("Attachment spec must contain 'url', 'content_b64', or 'bytes'")


def compose_prompt(user_prompt: str, saved_files: List[str], workspace_path: Optional[str] = None) -> str:
    if not saved_files:
        return user_prompt

    lines = [user_prompt, "", "Attached files (read them from disk as needed):"]
    for name in saved_files:
        if workspace_path:
            abs_path = os.path.abspath(os.path.join(workspace_path, "attachments", name))
            lines.append(f"- {abs_path}")
        else:
            lines.append(f"- ./attachments/{name}")

    return "\n".join(lines)

