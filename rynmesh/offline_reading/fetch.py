"""Bounded public-web fetches without cookies, scripts, or node credentials."""
from __future__ import annotations

import http.client
import ipaddress
import math
import multiprocessing
import socket
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit


class OfflineError(ValueError):
    pass


def fetch_resource(url, *, max_bytes, timeout=15, allow_loopback=False, check=lambda: None):
    """A cancellable child bounds DNS, TLS and slow headers as well as the body.

    Socket timeouts alone reset on every received byte and cannot bound DNS.
    The one download worker waits for (and reaps) this child before proceeding.
    """
    if (not isinstance(url, str) or len(url) > 4096 or type(max_bytes) is not int
            or not 0 < max_bytes <= 8 * 1024 * 1024 or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or not 0 < timeout <= 60):
        raise OfflineError('offline_request_invalid')
    check()
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_fetch_child, args=(child, url, max_bytes, timeout, allow_loopback), daemon=True)
    deadline = time.monotonic() + timeout
    try:
        process.start()
        child.close()
        while True:
            check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfflineError('offline_source_timeout')
            if parent.poll(min(0.1, remaining)):
                result = parent.recv()
                if 'error_code' in result:
                    raise OfflineError(result['error_code'])
                check()
                return result
            if not process.is_alive():
                raise OfflineError('offline_source_unreachable')
    except (OSError, EOFError):
        raise OfflineError('offline_source_unreachable') from None
    finally:
        child.close()
        parent.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(2)
            if process.is_alive():
                process.kill()
                process.join(2)
            if not process.is_alive():
                process.close()


def _fetch_child(pipe, url, max_bytes, timeout, allow_loopback):
    try:
        pipe.send(_fetch(url, max_bytes=max_bytes, timeout=timeout, allow_loopback=allow_loopback))
    except OfflineError as exc:
        pipe.send({'error_code': str(exc)})
    except Exception:
        pipe.send({'error_code': 'offline_source_unreachable'})
    finally:
        pipe.close()


def _fetch(url, *, max_bytes, timeout=15, allow_loopback=False, check=lambda: None):
    deadline = time.monotonic() + timeout
    for _ in range(4):
        check()
        parsed = urlsplit(url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password
                or len(url) > 4096 or any(ord(char) < 32 for char in url)):
            raise OfflineError('offline_source_address_invalid')
        try:
            port = parsed.port or (443 if parsed.scheme == 'https' else 80)
            addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)}
            if not addresses or any(not ipaddress.ip_address(address).is_global and
                not (allow_loopback and ipaddress.ip_address(address).is_loopback) for address in addresses):
                raise OfflineError('offline_source_address_blocked')
            address = sorted(addresses)[0]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            if parsed.scheme == 'https':
                import certifi
                connection = http.client.HTTPSConnection(parsed.hostname, port, timeout=remaining,
                    context=ssl.create_default_context(cafile=certifi.where()))
            else:
                connection = http.client.HTTPConnection(parsed.hostname, port, timeout=remaining)
            # Pin the validated address while retaining the original host for
            # Host, certificate validation and TLS SNI. Redirects are revalidated.
            connection._create_connection = lambda target, timeout, source_address=None, address=address, port=port: socket.create_connection(
                (address, port), timeout, source_address)
            try:
                path = urlunsplit(('', '', parsed.path or '/', parsed.query, ''))
                connection.request('GET', path, headers={'User-Agent': 'Ryn-Offline-Reader/1', 'Accept-Encoding': 'identity'})
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader('Location')
                    if not location:
                        raise OfflineError('offline_source_unavailable')
                    url = urljoin(url, location)
                    continue
                if response.status in {401, 403}:
                    raise OfflineError('offline_source_access_required')
                if response.status != 200:
                    raise OfflineError('offline_source_unavailable')
                length = response.getheader('Content-Length')
                if length and int(length) > max_bytes:
                    raise OfflineError('offline_resource_too_large')
                if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                    raise OfflineError('offline_source_encoding_unsupported')
                data = bytearray()
                while True:
                    check()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if connection.sock:
                        connection.sock.settimeout(remaining)
                    chunk = response.read1(min(65536, max_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise OfflineError('offline_resource_too_large')
                if length and len(data) != int(length):
                    raise OfflineError('offline_transfer_incomplete')
                return {'data': bytes(data), 'url': url, 'mime': response.getheader('Content-Type', '').split(';')[0].lower()}
            finally:
                connection.close()
        except OfflineError:
            raise
        except (OSError, ValueError, http.client.HTTPException):
            raise OfflineError('offline_source_unreachable') from None
    raise OfflineError('offline_redirect_limit')


def image_mime(data):
    """Only passive raster formats; no SVG/HTML or data/remote resource wrappers."""
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33 and b'IEND' in data[-12:]:
        width, height = int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')
        mime = 'image/png'
    elif data.startswith((b'GIF87a', b'GIF89a')) and len(data) >= 14 and data[-1:] == b';':
        width, height = int.from_bytes(data[6:8], 'little'), int.from_bytes(data[8:10], 'little')
        mime = 'image/gif'
    elif data.startswith(b'\xff\xd8') and data.endswith(b'\xff\xd9'):
        width = height = 0
        offset = 2
        while offset + 8 < len(data):
            if data[offset] != 0xff:
                break
            marker = data[offset + 1]
            if marker == 0xff:
                offset += 1
                continue
            size = int.from_bytes(data[offset + 2:offset + 4], 'big')
            if size < 2:
                break
            if marker in {0xc0, 0xc1, 0xc2}:
                height = int.from_bytes(data[offset + 5:offset + 7], 'big')
                width = int.from_bytes(data[offset + 7:offset + 9], 'big')
                break
            offset += size + 2
        mime = 'image/jpeg'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP' and len(data) >= 30:
        if int.from_bytes(data[4:8], 'little') + 8 != len(data):
            raise OfflineError('offline_image_invalid')
        kind = data[12:16]
        if kind == b'VP8X':
            width, height = 1 + int.from_bytes(data[24:27], 'little'), 1 + int.from_bytes(data[27:30], 'little')
        elif kind == b'VP8L' and data[20] == 0x2f:
            bits = int.from_bytes(data[21:25], 'little')
            width, height = (bits & 0x3fff) + 1, ((bits >> 14) & 0x3fff) + 1
        elif kind == b'VP8 ' and data[23:26] == b'\x9d\x01\x2a':
            width, height = int.from_bytes(data[26:28], 'little') & 0x3fff, int.from_bytes(data[28:30], 'little') & 0x3fff
        else:
            raise OfflineError('offline_image_invalid')
        mime = 'image/webp'
    else:
        raise OfflineError('offline_image_unsupported')
    if not 0 < width <= 8192 or not 0 < height <= 8192 or width * height > 16_000_000:
        raise OfflineError('offline_image_dimensions_exceeded')
    return mime
