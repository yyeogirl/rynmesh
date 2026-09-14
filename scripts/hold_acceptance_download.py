"""Windows-only: hold one synthetic acceptance copy to reproduce delete failure.

No mutation or deletion. Press Enter (or terminate this helper) to release it.
"""
from __future__ import annotations

import argparse
import ctypes
import os
import re
from ctypes import wintypes
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--job-id')
    target.add_argument('--reading-orphan', help='32-hex ID of an existing consumption.json atomic orphan')
    target.add_argument('--feed-orphan', help='32-hex ID of an existing friend-feed state.json atomic orphan')
    target.add_argument('--document-id', help='Managed imp_ ID of an existing synthetic original.txt')
    args = parser.parse_args()
    home = args.home.resolve()
    identifier = args.job_id or args.reading_orphan or args.feed_orphan or args.document_id
    path = (home / 'offline-reading' / 'downloads' / (identifier + '.json') if args.job_id
            else home / 'friend-feed' / ('.state.json.' + identifier + '.tmp') if args.feed_orphan
            else home / 'library-imports' / identifier / 'original.txt' if args.document_id
            else home / ('.consumption.json.' + identifier + '.tmp'))
    prefix = 'rynmesh-feed-acceptance-' if args.feed_orphan or args.document_id else 'rynmesh-offline-acceptance-'
    if (os.name != 'nt' or not home.name.startswith(prefix)
            or not re.fullmatch(r'imp_[a-f0-9]{32}(?:[a-f0-9]{32})?' if args.document_id else '[a-f0-9]{32}', identifier)
            or path.resolve() != path or not path.is_file()):
        raise SystemExit('Use one existing synthetic acceptance copy on Windows.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # Share read/write so review still works, but withhold FILE_SHARE_DELETE.
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        input('Synthetic copy held open without delete sharing. Press Enter to release.\n')
    finally:
        kernel.CloseHandle(handle)
        print('Synthetic copy handle released.')


if __name__ == '__main__':
    main()
