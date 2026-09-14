"""Temporarily block atomic replacement of one Windows acceptance data file.

Reads and ordinary writes remain shared. Holding an existing file without
FILE_SHARE_DELETE makes an atomic replace fail, without changing bytes or ACLs.
Stop this helper to release the fault. Never use it on a personal node home.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--target', choices=('consumption.json', 'recommendation-profile.json', 'ask-ryn/history.json', 'local-search/index.json', 'friend-feed/state.json'), required=True)
    args = parser.parse_args()
    home = args.home.resolve()
    assert os.name == 'nt'
    if args.target == 'friend-feed/state.json':
        assert home.name == 'rynmesh-feed-acceptance-author-20260911-v2'
        assert json.loads((home / args.target).read_text())['version'] in {'ryn.friend-feed.v1', 'ryn.friend-feed.v2'}
    elif args.target == 'local-search/index.json':
        assert home.name == 'rynmesh-first-sharing-acceptance-invites-20260914-C'
        marker = json.loads((home / '.first-sharing-fixture.json').read_text(encoding='utf-8'))
        assert marker['kind'] == 'ryn.first-sharing-acceptance.v1' and marker['port'] == 18972
    elif args.target == 'ask-ryn/history.json':
        assert home.name == 'rynmesh-ai-recovery-acceptance'
        marker = json.loads((home / '.ai-recovery-fixture.json').read_text(encoding='utf-8'))
        assert marker['kind'] == 'ryn.ai-recovery-acceptance.v1'
    else:
        assert home.name.startswith('rynmesh-first-reading-acceptance-')
        marker = json.loads((home / '.first-reading-fixture.json').read_text(encoding='utf-8'))
        assert marker['kind'] == 'ryn.first-reading-acceptance.v1'
    path = (home / args.target).resolve()
    assert path.is_relative_to(home) and path.is_file()
    api = ctypes.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = api.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    print('Atomic replacement fault active: ' + args.target, flush=True)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        api.CloseHandle(handle)


if __name__ == '__main__':
    main()
