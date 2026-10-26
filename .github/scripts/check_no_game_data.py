#!/usr/bin/env python3
"""No game data in the repository (CONTRIBUTING.md, rule 1).

Fails when any commit reachable from HEAD, or HEAD's tree, holds:
  - a Switch game file: .nca .nsp .nsz .xci .nso .nro .arc, or the compressed .ncz .xcz;
  - a key file: a *.keys file (prod.keys, title.keys), keys.txt, a ticket (*.tik), or a text file with
    console key lines in it (header_key = ..., master_key_00 = ...) under any name;
  - a file over 10 MB (10,000,000 bytes).

The whole history counts, not only the last tree: a file added in one commit and deleted in the next is still
in every clone. Run it from anywhere in the repository (CI checks out the full history):

    python .github/scripts/check_no_game_data.py

Standard library only. Exit status 0 when clean, 1 with a list of what was found.
"""
import os
import re
import subprocess
import sys

MAX_BYTES = 10_000_000
SWITCH_TYPES = {'.nca', '.ncz', '.nsp', '.nsz', '.xci', '.xcz', '.nso', '.nro', '.arc'}
KEY_NAME = re.compile(r'(^|/)([^/]*\.keys|keys\.txt|[^/]*\.tik)$', re.IGNORECASE)
# a line of a hactool-style key file: a console key's name, '=', then at least 128 bits of hex
KEY_LINE = re.compile(rb'^[ \t]*(header_key|(master|package1|package2|titlekek|aes_kek_generation|aes_key_generation'
                      rb'|key_area_key_(application|ocean|system)|eticket_rsa_kek|save_mac_key|sd_card_kek)'
                      rb'(_[0-9a-f]{2})?)[ \t]*=[ \t]*[0-9a-f]{32}', re.IGNORECASE | re.MULTILINE)
SCAN_CONTENT_UP_TO = 1_000_000   # key files are small; bigger blobs are checked by name and size only


def git(*args, data=None):
    return subprocess.run(['git', '-c', 'core.quotePath=false', *args], input=data, capture_output=True,
                          check=True).stdout


def all_paths():
    """Every path any commit reachable from HEAD added or changed (merges against each parent), and HEAD's."""
    out = git('log', 'HEAD', '-m', '--no-renames', '--format=', '--name-only', '--diff-filter=ACMRT')
    paths = {p for p in out.decode('utf-8', 'replace').splitlines() if p}
    paths |= {p for p in git('ls-tree', '-r', '--name-only', 'HEAD').decode('utf-8', 'replace').splitlines() if p}
    return paths


def all_blobs():
    """(blob id, size, a path it was seen at) for every blob reachable from HEAD."""
    listing = git('rev-list', '--objects', 'HEAD')
    checked = git('cat-file', '--batch-check=%(objecttype) %(objectname) %(objectsize) %(rest)', data=listing)
    for line in checked.decode('utf-8', 'replace').splitlines():
        kind, oid, size, *rest = line.split(' ', 3)
        if kind == 'blob':
            yield oid, int(size), rest[0] if rest else ''


def blob_contents(oids):
    """{blob id: bytes} through one git cat-file --batch."""
    out = git('cat-file', '--batch', data=''.join(o + '\n' for o in oids).encode())
    found, i = {}, 0
    while i < len(out):
        eol = out.index(b'\n', i)
        oid, _kind, size = out[i:eol].decode().split(' ')
        start = eol + 1
        found[oid] = out[start:start + int(size)]
        i = start + int(size) + 1
    return found


def main():
    os.chdir(git('rev-parse', '--show-toplevel').decode().strip())
    problems = []
    for path in sorted(all_paths()):
        ext = os.path.splitext(path)[1].lower()
        if ext in SWITCH_TYPES:
            problems.append(f'{path}: a Switch game file ({ext})')
        if KEY_NAME.search(path):
            problems.append(f'{path}: a key or ticket file')
    small = {}
    for oid, size, path in all_blobs():
        if size > MAX_BYTES:
            problems.append(f'{path or oid}: {size:,} bytes, over the 10 MB limit (blob {oid})')
        elif size <= SCAN_CONTENT_UP_TO:
            small[oid] = path
    for oid, data in blob_contents(sorted(small)).items():
        if KEY_LINE.search(data):
            problems.append(f'{small[oid] or oid}: console key lines (blob {oid})')
    commits = int(git('rev-list', '--count', 'HEAD').decode())
    if problems:
        print(f'Game data or keys in the repository ({commits} commits checked):')
        for p in problems:
            print('  ' + p)
        print('Remove them from every commit, not only the last one (CONTRIBUTING.md, rule 1). '
              'git log --all -- <path> shows the commits that hold a path.')
        return 1
    print(f'No game files, key files or files over 10 MB: {len(small):,} small blobs read, {commits} commits checked.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
