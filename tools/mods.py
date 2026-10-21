#!/usr/bin/env python3
"""Mods: ARCropolis-style file mods (GameBanana's kind) installed, turned on and off, and applied.

Usage:
  mods.py list                          the installed mods, as JSON
  mods.py install <archive> [--name N] [--gamebanana ID] [--title T] [--image URL]
  mods.py gamebanana <mod id> [--file FILE_ID]   download a mod's file from GameBanana and install it
  mods.py enable|disable|remove <name>
  mods.py order <name>...               the mods' order: a later one wins where two change a file
  mods.py apply                         make the game read the turned-on mods (none: the game as it is)
  mods.py vanilla                       the game as it is, without taking the mods off the list
  (every command takes --tree <dir>: the port's folder, the default)

A mod is a folder in <tree>/mods: files at the paths the game's data.arc has them at -
fighter/mario/model/body/c00/def_mario_001_col.nutexb, ui/replace/..., sound/..., and
"stream;/..." for stream: files (music) - as ARCropolis reads them on a Switch. Its state is in
<tree>/mods/mods.json.

How it is applied, with no change to the game or the runtime. The runtime reads the game's RomFS
through a layer map (romfs.cfg, runtime/hle_fs.c): byte ranges of the image from source files.
data.arc is the image's last file; the RomFS's own tables follow it. `apply` writes
<tree>/dump/mods/overlay.bin and a second layer map in which
  - data.arc grows past its old end, and its header points at a new copy of its file table there;
  - the game reads a folder's files as one chunk (folder offsets: offset, sizes), read as a
    stream: each file (a data entry an info-to-data entry of the folder names) where the one
    before it ends, 16-aligned. So each folder a mod changes is laid out again after data.arc's
    old end, its files in their order: the unchanged ones as they were - read from the game's own
    files through the layer map, not copied (the overlay has a hole there) - and a changed one's
    new bytes in its place, the files after it moved along; the folder's data entries and its
    folder entry point at the new place. No table grows: the game doesn't load a table with
    more entries than it had (tried: it crashes);
  - a stream: file (music) is stored whole, its stream data entries pointed at it;
  - the RomFS's tables come after data.arc's new end, with its new size;
and romfs.cfg is switched to it (the game as it is: romfs_vanilla.cfg, kept).
Limits, as against ARCropolis on a Switch: only files the game has are changed - a mod's new
files (more costume slots, a custom model's own textures) are skipped and listed; and a file two
paths share (one costume's texture another reuses) changes for both.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import ssl
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from arc import Arc, LOW40, hash40, romfs_entries  # noqa: E402
from nca import RomfsImage, read_layers, write_layers  # noqa: E402

try:
    from compression.zstd import CompressionParameter, compress as zstd_compress   # Python 3.14

    def game_compress(data):
        """zstd as the game's own files are: a 2 MB window, no content size, no checksum. The
        game's decompressor overruns its buffers on other frames (a single-segment one, whose
        window is the whole file: a crash in a copy, loading a big modded texture)."""
        return zstd_compress(data, options={CompressionParameter.window_log: 21, CompressionParameter.content_size_flag: 0,
                                            CompressionParameter.checksum_flag: 0})
except ImportError:                                           # 3.10 to 3.13: the zstandard package
    import zstandard

    def zstd_compress(data):
        return zstandard.ZstdCompressor().compress(data)

    def game_compress(data):
        params = zstandard.ZstdCompressionParameters.from_level(3, window_log=21, write_content_size=False, write_checksum=False)
        return zstandard.ZstdCompressor(compression_params=params).compress(data)

LAYER_ZERO = 0xFFFFFFFF
MODDED_MAP = "romfs_mods.layers"
# A mod's own folder holds these at its top (the game's data.arc's top folders, and ARCropolis'
# names for the stream: and prebuilt: ones)
TOP = {"fighter", "ui", "sound", "stream;", "prebuilt;", "effect", "stage", "camera", "item", "param", "assist",
       "boss", "enemy", "finding", "pokemon", "spirits", "standard", "common", "menu", "render", "shader", "snapshot",
       "fighter_pass", "miihat", "replay", "sp_enemy"}
SKIP_FILES = {"info.toml", "config.json", "preview.webp", "preview.png", "preview.jpg", "readme.txt", "readme.md"}
GAMEBANANA = "https://gamebanana.com/apiv11"


class Error(Exception):
    """Something the person can act on; the message says what."""


# ---- where things are ----------------------------------------------------------------------
class Tree:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        self.mods = os.path.join(self.root, "mods")
        self.state_path = os.path.join(self.mods, "mods.json")
        self.modules = os.path.join(self.root, "dump", "modules")
        self.cfg = os.path.join(self.modules, "romfs.cfg")
        self.vanilla_cfg = os.path.join(self.modules, "romfs_vanilla.cfg")
        self.work = os.path.join(self.root, "dump", "mods")

    def load(self):
        try:
            with open(self.state_path, encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, ValueError):
            state = {}
        state.setdefault("order", [])
        state.setdefault("mods", {})
        present = [n for n in sorted(os.listdir(self.mods)) if os.path.isdir(os.path.join(self.mods, n))] if os.path.isdir(self.mods) else []
        state["order"] = [n for n in state["order"] if n in present] + [n for n in present if n not in state["order"]]
        for n in present:
            state["mods"].setdefault(n, {"enabled": True})
        state["mods"] = {n: m for n, m in state["mods"].items() if n in present}
        return state

    def save(self, state):
        os.makedirs(self.mods, exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1)
        os.replace(tmp, self.state_path)

    def applied(self):
        """The mods the game reads now (romfs.cfg's), or None for the game as it is."""
        try:
            with open(os.path.join(self.work, "applied.json"), encoding="utf-8") as f:
                applied = json.load(f)
        except (OSError, ValueError):
            return None
        return applied if self.is_modded() else None

    def is_modded(self):
        try:
            with open(self.cfg, encoding="utf-8") as f:
                return MODDED_MAP in f.read()
        except OSError:
            return False


# ---- a mod's files ---------------------------------------------------------------------------
def arc_path(rel):
    """A file's path in data.arc from its path in a mod's folder."""
    rel = rel.replace(os.sep, "/")
    if rel.startswith("stream;/"):
        return "stream:/" + rel[len("stream;/"):]
    if rel.startswith("prebuilt;/"):
        return "prebuilt:/" + rel[len("prebuilt;/"):]
    return rel


def mod_files(folder):
    """(arc path, file) of every file a mod's folder holds."""
    out = []
    for dp, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for n in sorted(files):
            if n.startswith(".") or n.lower() in SKIP_FILES:
                continue
            full = os.path.join(dp, n)
            out.append((arc_path(os.path.relpath(full, folder)), full))
    return out


def find_roots(top):
    """The folders under `top` laid out as data.arc is (each a mod's own folder)."""
    roots = []
    for dp, dirs, _files in os.walk(top):
        if any(d.lower() in TOP for d in dirs):
            roots.append(dp)
            dirs[:] = []   # (a mod's own folders are not other mods)
    return roots


# ---- installing ------------------------------------------------------------------------------
def extract(archive, into):
    """A .zip with Python's own reader; .7z and .rar (and the rest) with macOS's bsdtar."""
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            for m in z.infolist():
                target = os.path.realpath(os.path.join(into, m.filename))
                if not target.startswith(os.path.realpath(into) + os.sep) and target != os.path.realpath(into):
                    raise Error(f"{os.path.basename(archive)} has a file outside its folder ({m.filename}): not installed")
            z.extractall(into)
        return
    r = subprocess.run(["tar", "-xf", archive, "-C", into], capture_output=True, text=True)
    if r.returncode:
        raise Error(f"cannot unpack {os.path.basename(archive)}: {r.stderr.strip()[-400:]}")


def safe_name(name):
    name = re.sub(r"[^\w .()+&'-]+", "_", name).strip(" ._") or "mod"
    return name[:80]


def install(tree, archive, name=None, meta=None):
    """Unpack `archive` and add each mod folder in it; returns the names added."""
    os.makedirs(tree.mods, exist_ok=True)
    state = tree.load()
    added = []
    with tempfile.TemporaryDirectory(prefix="windowsmash-mod-") as tmp:
        if os.path.isdir(archive):
            shutil.copytree(archive, os.path.join(tmp, "x"))
        else:
            os.makedirs(os.path.join(tmp, "x"))
            extract(archive, os.path.join(tmp, "x"))
        roots = find_roots(os.path.join(tmp, "x"))
        if not roots:
            raise Error(f"{os.path.basename(archive)} has no files laid out as the game's (fighter/, ui/, sound/, stream;/ ...): "
                        "it may be a Skyline plugin (code for a Switch), which WindowSmash can't run")
        base = safe_name(name or os.path.splitext(os.path.basename(archive))[0])
        for root in roots:
            leaf = os.path.basename(root)
            label = base if len(roots) == 1 else f"{base} - {safe_name(leaf)}"
            final, k = label, 2
            while os.path.exists(os.path.join(tree.mods, final)):
                final, k = f"{label} ({k})", k + 1
            shutil.copytree(root, os.path.join(tree.mods, final))
            entry = {"enabled": True}
            entry.update(meta or {})
            state = tree.load()
            state["mods"][final] = entry
            tree.save(state)
            added.append(final)
    return added


def gamebanana_get(path):
    ctx = ssl.create_default_context()
    try:
        import certifi   # (python.org's Python has no certificates of its own until it is told)
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    req = urllib.request.Request(GAMEBANANA + path, headers={"User-Agent": "WindowSmash mod manager"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
        return json.load(r), ctx


def gamebanana_install(tree, mod_id, file_id=None, progress=None):
    data, ctx = gamebanana_get(f"/Mod/{int(mod_id)}?_csvProperties=_sName,_aFiles,_aPreviewMedia,_aSubmitter,_aCategory")
    files = data.get("_aFiles") or []
    if not files:
        raise Error("this mod has no files to download on GameBanana")
    f = next((x for x in files if str(x.get("_idRow")) == str(file_id)), None) if file_id else files[0]
    if not f:
        raise Error(f"the mod has no file {file_id}")
    if f.get("_sAvResult") not in (None, "", "clean"):
        raise Error(f"GameBanana's virus scan of {f.get('_sFile')} says: {f.get('_sAvResult')}. Not installed.")
    images = (data.get("_aPreviewMedia") or {}).get("_aImages") or []
    image = f"{images[0]['_sBaseUrl']}/{images[0].get('_sFile530') or images[0]['_sFile']}" if images else ""
    with tempfile.TemporaryDirectory(prefix="windowsmash-dl-") as tmp:
        target = os.path.join(tmp, os.path.basename(f["_sFile"]))
        req = urllib.request.Request(f["_sDownloadUrl"], headers={"User-Agent": "WindowSmash mod manager"})
        md5 = hashlib.md5()
        with urllib.request.urlopen(req, timeout=60, context=ctx) as r, open(target, "wb") as out:
            total, done = int(r.headers.get("Content-Length") or f.get("_nFilesize") or 0), 0
            while chunk := r.read(1 << 20):
                out.write(chunk)
                md5.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        if f.get("_sMd5Checksum") and md5.hexdigest() != f["_sMd5Checksum"].lower():
            raise Error(f"{f['_sFile']} did not download whole (its checksum differs): try again")
        meta = {"gamebanana": int(mod_id), "title": data.get("_sName", ""), "image": image,
                "author": (data.get("_aSubmitter") or {}).get("_sName", ""),
                "category": (data.get("_aCategory") or {}).get("_sName", ""), "file": f["_sFile"]}
        return install(tree, target, data.get("_sName") or f["_sFile"], meta)


# ---- applying --------------------------------------------------------------------------------
def vanilla_cfg(tree):
    """romfs_vanilla.cfg: the game as it is (romfs.cfg, kept the first time mods are applied)."""
    if not tree.is_modded() and os.path.exists(tree.cfg):
        shutil.copyfile(tree.cfg, tree.vanilla_cfg)
    if not os.path.exists(tree.vanilla_cfg):
        raise Error("the game isn't set up here yet (no dump/modules/romfs.cfg): build it first")
    with open(tree.vanilla_cfg, encoding="utf-8") as f:
        lines = [l.rstrip("\r\n") for l in f if l.strip()]
    return lines


class Table:
    """data.arc's file table, changed in place (no entry added, no table's size changed)."""

    def __init__(self, arc):
        self.arc = arc
        self.t = bytearray(arc.t)
        self.streams = None
        self.owned = None
        self.replaced = {}   # folder -> {data index: (stored bytes, size, flags)}

    def entry(self, table, index, fmt):
        o, size, _ = self.arc.tab[table]
        return struct.unpack_from(fmt, self.t, o + index * size)

    def put(self, table, index, fmt, *values):
        o, size, _ = self.arc.tab[table]
        struct.pack_into(fmt, self.t, o + index * size, *values)

    def path_index(self, path):
        h = hash40(path)
        start, count = struct.unpack_from("<2I", self.t, self.arc.bucket_table + (h % self.arc.buckets) * 8)
        lo, hi = start, start + count
        while lo < hi:
            mid = (lo + hi) // 2
            v = self.entry("hash_to_path", mid, "<Q")[0]
            if v & LOW40 == h:
                return v >> 40
            lo, hi = (mid + 1, hi) if v & LOW40 < h else (lo, mid)
        return None

    def file_targets(self, path):
        """The (folder, data) entries of a file (one per region for a regional file), or None."""
        p = self.path_index(path)
        if p is None:
            return None
        full = self.entry("paths", p, "<Q")[0]
        info = self.entry("info_indices", full >> 40, "<2I")[1]
        _, _, to_data, flags = self.entry("infos", info, "<4I")
        out = []
        for r in range(self.arc.regions if flags & 0x8000 else 1):
            folder, data, _ = self.entry("info_to_datas", to_data + r, "<3I")
            out.append((folder, data))
        return sorted(set(out))

    def replace(self, folder, data, raw):
        """A file's new bytes (stored as the original is: zstd-compressed or not)."""
        flags = self.entry("datas", data, "<4I")[3]
        stored = game_compress(raw) if flags & 3 else raw
        self.replaced.setdefault(folder, {})[data] = (stored, len(raw), flags)

    def folder_files(self):
        """folder -> the file data entries in its chunk: the ones its info-to-data entries name
        (the folder's own range of data entries counts other folders' too)."""
        if self.owned is None:
            self.owned = {}
            ndatas = self.arc.tab["datas"][2]
            o, size, count = self.arc.tab["info_to_datas"]
            for folder, data, _ in struct.iter_unpack("<3I", self.t[o:o + size * count]):
                if data < ndatas:   # (unused entries name no data)
                    self.owned.setdefault(folder, set()).add(data)
        return self.owned

    def rebuild_folders(self, alias, place):
        """Each folder a mod changes, laid out again: its files in their order, a changed file's
        new bytes in its place, the unchanged ones' original bytes (`alias`: read from the game's
        files, not copied); the folder and its files' entries pointed there.
        The game reads a folder's chunk as one stream, each file where the one before ends
        (16-aligned): its loader (f_7103544a70) steps through the chunk by each file's stored
        size, not by the file's offset. So a changed file has to stay in its place, the files
        after it moved along (a file put after the chunk: the files read after it are read from
        the wrong place - a hang, or a crash in the loader's copy)."""
        owned = self.folder_files()
        for folder in sorted(self.replaced):
            off, decomp, comp, start, count, res = self.entry("folder_offsets", folder, "<QIIIII")
            files = [(o, d) for o, _, d in sorted((self.entry("datas", d, "<4I")[0] * 4, self.entry("datas", d, "<4I")[1] > 0, d)
                                                  for d in owned.get(folder, ()))]   # (an empty file before a file at its offset)
            changes = self.replaced[folder]
            # the chunk in pieces: runs of unchanged files (old offsets [a, b), the files) and changed files
            # (an empty file, stored size 0, is in the run it is at the end of)
            pieces, pos = [], 0
            for in_folder, d in files:
                if in_folder != pos:
                    raise Error(f"folder {folder}'s files aren't laid out one after another (file data {d})")
                pos = align(pos + self.entry("datas", d, "<4I")[1], 0x10)
                if d in changes:
                    pieces.append(("new", d))
                elif pieces and pieces[-1][0] == "old":
                    pieces[-1][2] = pos
                    pieces[-1][3].append((in_folder, d))
                else:
                    pieces.append(["old", in_folder, pos, [(in_folder, d)]])
            if pos != comp or not any(d in changes for _, d in files):
                raise Error(f"folder {folder}'s files don't make up its data")
            base, end, grow = None, None, 0
            for piece in pieces:
                if piece[0] == "old":
                    _, a, b, members = piece
                    where = alias(self.arc.file_section + off + a, b - a, 0x10)
                    base = where if base is None else base
                    for in_folder, d in members:
                        o, stored, size, flags = self.entry("datas", d, "<4I")
                        self.put("datas", d, "<4I", (where + in_folder - a - base) // 4, stored, size, flags)
                    where_end = where + b - a
                else:
                    d = piece[1]
                    stored, size, flags = changes[d]
                    where = place(stored, 0x10)
                    base = where if base is None else base
                    grow += size - self.entry("datas", d, "<4I")[2]
                    self.put("datas", d, "<4I", (where - base) // 4, len(stored), size, flags)
                    where_end = align(where + len(stored), 0x10)
                if end is not None and where != end:   # (alias and place pad to 0x10, as the chunk is)
                    raise Error(f"folder {folder}: a file isn't where the one before it ends")
                end = where_end
            self.put("folder_offsets", folder, "<QIIIII", base - self.arc.file_section, max(0, decomp + grow),
                     end - base, start, count, res)

    def stream_targets(self, path):
        """The stream-data entries of a stream: file (each of its variants), or None."""
        if self.streams is None:
            self.streams = {}
            for i in range(self.arc.tab["stream_entries"][2]):
                full, flags = self.entry("stream_entries", i, "<QI")
                self.streams[full & LOW40] = (full >> 40, flags)
        found = self.streams.get(hash40(path))
        if not found:
            return None
        index, flags = found
        nidx = self.arc.tab["stream_indices"][2]
        out = []
        for r in range((14 if flags & 1 else 5) if flags & 3 else 1):
            if index + r < nidx:
                out.append(self.entry("stream_indices", index + r, "<I")[0])
        return sorted(set(out))


def align(n, a):
    return (n + a - 1) // a * a


def split_layers(entries, size, overrides):
    """Layer entries (at, src, off) over [0, size) with `overrides` (at, end, src, off) - apart from
    each other - laid on top: one sweep over both, in order."""
    spans = []
    for k, (at, src, off) in enumerate(entries):
        end = entries[k + 1][0] if k + 1 < len(entries) else size
        if at < end:
            spans.append((at, end, src, off))
    overrides = sorted(o for o in overrides if o[0] < o[1])
    out, i = [], 0
    for at, end, src, off in spans:
        pos = at
        while i < len(overrides) and overrides[i][1] <= pos:
            i += 1
        j = i
        while pos < end:
            if j < len(overrides) and overrides[j][0] < end:
                oat, oend = overrides[j][0], overrides[j][1]
                if oat > pos:
                    out.append((pos, src, off if src == LAYER_ZERO else off + (pos - at)))
                pos = max(pos, oend)
                j += 1
            else:
                out.append((pos, src, off if src == LAYER_ZERO else off + (pos - at)))
                pos = end
    out += [(oat, osrc, ooff) for oat, _oend, osrc, ooff in overrides]
    out.sort()
    return out


def pieces(entries, size, a, b, to):
    """Layer overrides that put image bytes [a, b) (as `entries` lay them) at `to` on."""
    out = []
    for k, (at, src, off) in enumerate(entries):
        end = entries[k + 1][0] if k + 1 < len(entries) else size
        lo, hi = max(at, a), min(end, b)
        if lo < hi:
            out.append((to + lo - a, to + hi - a, src, off if src == LAYER_ZERO else off + (lo - at)))
    return out


def apply(tree, log=print):
    """Make romfs.cfg read the turned-on mods (or the game as it is, when none is on)."""
    lines = vanilla_cfg(tree)
    state = tree.load()
    on = [n for n in state["order"] if state["mods"].get(n, {}).get("enabled")]
    if not on:
        shutil.copyfile(tree.vanilla_cfg, tree.cfg)
        os.makedirs(tree.work, exist_ok=True)
        with open(os.path.join(tree.work, "applied.json"), "w", encoding="utf-8") as f:
            json.dump({"mods": [], "files": 0, "skipped": {}}, f)
        log("no mod is turned on: the game as it is")
        return {"mods": [], "files": 0, "skipped": {}}
    if lines[0] != "layered":
        raise Error("the game's data isn't read through a layer map here (romfs.cfg): mods need it (run the installer again)")
    image = RomfsImage(tree.vanilla_cfg)
    try:
        return _apply(tree, image, lines, state, on, log)
    finally:
        image.close()


def _apply(tree, image, lines, state, on, log):
    t0 = time.time()
    size, entries = read_layers(lines[1])
    sources = lines[2:]
    if len(sources) >= 8:
        raise Error("romfs.cfg already has 8 sources")
    rhdr = list(struct.unpack("<10Q", image.read(0, 0x50)))
    arc_at, arc_size = next((o, s) for p, o, s in romfs_entries(image) if p == "data.arc")
    meta_at = rhdr[1]
    if arc_at + arc_size > meta_at or rhdr[3] < meta_at or rhdr[5] < meta_at or rhdr[7] < meta_at or rhdr[7] + rhdr[8] > size:
        raise Error("the game's RomFS isn't laid out as expected (data.arc last, its tables after it)")
    arc = Arc(lambda off, n: image.read(arc_at + off, n))
    table = Table(arc)

    # every file the turned-on mods hold (a later mod wins), sorted into kinds
    chosen = {}
    for name in on:
        for path, src in mod_files(os.path.join(tree.mods, name)):
            chosen[path.lower()] = (path, src, name)
    os.makedirs(tree.work, exist_ok=True)
    overlay_path = os.path.join(tree.work, "overlay.bin")
    tmp_path = overlay_path + ".tmp"
    skipped = {}
    changed = 0
    HEAD = 0x100   # overlay: [0, 0x50) the RomFS header, [0x50, 0x88) data.arc's, from HEAD data.arc's new bytes
    grow_at = align(arc_size, 0x10)   # data.arc's new bytes start here (data.arc offsets)
    with open(tmp_path, "wb") as out:
        out.write(b"\0" * HEAD)
        out.write(b"\0" * (grow_at - arc_size))
        at = grow_at

        def place(data, a):
            nonlocal at
            pad = align(at, a) - at
            out.write(b"\0" * pad)
            at += pad
            where = at
            out.write(data)
            at += len(data)
            return where

        aliases = []   # (data.arc offset, the original's data.arc offset, size): read from the game's own files

        def alias(original, size, a):
            nonlocal at
            pad = align(at, a) - at
            out.write(b"\0" * pad)
            at += pad
            where = at
            out.seek(size, os.SEEK_CUR)   # (a hole: no disk space, the layer map reads the game's bytes there)
            at += size
            aliases.append((where, original, size))
            return where

        for key in sorted(chosen):
            path, src, name = chosen[key]
            if path.startswith("stream:/"):
                targets = table.stream_targets(path)
                if not targets:
                    skipped.setdefault(name, []).append(path)
                    continue
                with open(src, "rb") as f:
                    data = f.read()
                where = place(data, 0x10)
                for d in targets:
                    table.put("stream_datas", d, "<QQ", len(data), where)
                changed += 1
                continue
            targets = table.file_targets(path)
            # (a data entry of size 0 is a costume slot reusing another's file: the game sizes the
            # file's buffer from that other one, so new bytes here overrun it - left as it is)
            targets = [(f, d) for f, d in targets or [] if table.entry("datas", d, "<4I")[2]]
            if not targets:
                skipped.setdefault(name, []).append(path)
                continue
            with open(src, "rb") as f:
                data = f.read()
            for folder, d in targets:
                table.replace(folder, d, data)
            changed += 1
        table.rebuild_folders(alias, place)

        # the table, then data.arc's new end
        z = zstd_compress(bytes(table.t))
        orig_head = struct.unpack("<4I", arc.read(arc.header[4], 16))
        table_at = place(struct.pack("<4I", 0x10, len(table.t), len(z), align(len(z), 4)) + z, 0x10)
        _ = orig_head
        new_arc_size = at
        hdr = list(arc.header)
        hdr[4] = table_at

        # the RomFS's tables after data.arc's new end, data.arc's size in them
        new_meta_at = align(arc_at + new_arc_size, 0x10)
        out.write(b"\0" * (new_meta_at - (arc_at + new_arc_size)))
        meta = bytearray(image.read(meta_at, size - meta_at))
        fmo, fms = rhdr[7] - meta_at, rhdr[8]
        o, found = fmo, False
        while o < fmo + fms:
            _p, _s, foff, fsize, _h, nlen = struct.unpack_from("<IIQQII", meta, o)
            if meta[o + 0x20:o + 0x20 + nlen] == b"data.arc":
                struct.pack_into("<Q", meta, o + 0x10, new_arc_size)
                found = True
                break
            o += 0x20 + align(nlen, 4)
        if not found:
            raise Error("data.arc's entry isn't in the RomFS's file table")
        meta_in_overlay = HEAD + (new_meta_at - arc_at - arc_size)
        out.write(meta)
        delta = new_meta_at - meta_at
        for k in (1, 3, 5, 7):
            rhdr[k] += delta
        out.seek(0)
        out.write(struct.pack("<10Q", *rhdr))
        out.write(struct.pack("<7Q", *hdr))
    os.replace(tmp_path, overlay_path)

    # the layer map: the game's, with the two headers, the new bytes and the tables from the overlay
    src = len(sources)
    new_size = new_meta_at + len(meta)
    base = [(a, s, o) for a, s, o in entries if a < meta_at]
    overrides = [(0, 0x50, src, 0), (arc_at, arc_at + 0x38, src, 0x50),
                 (arc_at + arc_size, new_meta_at, src, HEAD), (new_meta_at, new_size, src, meta_in_overlay)]
    if aliases:   # the new bytes, but where the game's own original bytes are read again (aliases)
        ext = overrides[2]
        overrides[2:3] = []
        pos = ext[0]
        for where, original, n in sorted(aliases):
            if arc_at + where > pos:
                overrides.append((pos, arc_at + where, src, HEAD + (pos - (arc_at + arc_size))))
            overrides += pieces(base, meta_at, arc_at + original, arc_at + original + n, arc_at + where)
            pos = arc_at + where + n
        if pos < ext[1]:
            overrides.append((pos, ext[1], src, HEAD + (pos - (arc_at + arc_size))))
    layers = split_layers(base, meta_at, overrides)
    map_path = os.path.join(tree.modules, MODDED_MAP)
    write_layers(map_path, new_size, layers)
    cfg_text = "layered\n" + map_path + "\n" + "".join(s + "\n" for s in sources) + overlay_path + "\n"
    check(cfg_text, chosen, skipped, tree)
    with open(tree.cfg + ".tmp", "w", encoding="utf-8") as f:
        f.write(cfg_text)
    os.replace(tree.cfg + ".tmp", tree.cfg)
    written = os.stat(overlay_path).st_blocks * 512
    summary = {"mods": on, "files": changed, "skipped": skipped, "bytes": written}
    with open(os.path.join(tree.work, "applied.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=1)
    log(f"{len(on)} mod(s), {changed} files changed, {sum(len(v) for v in skipped.values())} skipped "
        f"(new files the game doesn't have), {written / 1048576:.1f} MB written, in {time.time() - t0:.0f} s")
    return summary


def check(cfg_text, chosen, skipped, tree):
    """Read the modded RomFS back as the game will: a few changed files must be the mods' bytes."""
    probe = os.path.join(tree.work, "check.cfg")
    with open(probe, "w", encoding="utf-8") as f:
        f.write(cfg_text)
    image = RomfsImage(probe)
    try:
        arc_at = next(o for p, o, _s in romfs_entries(image) if p == "data.arc")
        arc = Arc(lambda off, n: image.read(arc_at + off, n))
        bad = set(p for v in skipped.values() for p in v)
        sample = [v for v in chosen.values() if v[0] not in bad and not v[0].startswith("stream:/")][:8]
        for path, src, _name in sample:
            with open(src, "rb") as f:
                if arc.get(path) != f.read():
                    raise Error(f"check failed: {path} doesn't read back as the mod has it (nothing was changed)")
    finally:
        image.close()
        os.remove(probe)


# ---- the command line ------------------------------------------------------------------------
def listing(tree):
    state = tree.load()
    applied = tree.applied()
    out = []
    for n in state["order"]:
        m = state["mods"][n]
        files = mod_files(os.path.join(tree.mods, n))
        out.append({"name": n, "enabled": bool(m.get("enabled")), "files": len(files),
                    "skipped": len((applied or {}).get("skipped", {}).get(n, [])),
                    **{k: v for k, v in m.items() if k != "enabled"}})
    return {"mods": out, "applied": (applied or {}).get("mods"), "modded": tree.is_modded()}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--tree", default=os.path.dirname(HERE))
    ap.add_argument("--name")
    ap.add_argument("--file")
    ap.add_argument("--gamebanana")
    ap.add_argument("--title")
    ap.add_argument("--image")
    ap.add_argument("--json", action="store_true", help="results as one line of JSON (the Mods app reads it)")
    a = ap.parse_args()
    tree = Tree(a.tree)

    def say(text):
        print(json.dumps({"log": text}) if a.json else text, flush=True)

    try:
        if a.command == "list":
            print(json.dumps(listing(tree), indent=None if a.json else 1))
            return 0
        if a.command == "install":
            meta = {k: v for k, v in (("gamebanana", a.gamebanana), ("title", a.title), ("image", a.image)) if v}
            for path in a.args:
                say(f"installed: {', '.join(install(tree, path, a.name, meta))}")
        elif a.command == "gamebanana":
            last = [0.0]

            def progress(done, total):
                if a.json and time.time() - last[0] > 0.25:
                    last[0] = time.time()
                    print(json.dumps({"progress": done / total if total else 0}), flush=True)
            say(f"installed: {', '.join(gamebanana_install(tree, a.args[0], a.file, progress))}")
        elif a.command in ("enable", "disable"):
            state = tree.load()
            for n in a.args:
                if n not in state["mods"]:
                    raise Error(f"no mod called {n}")
                state["mods"][n]["enabled"] = a.command == "enable"
            tree.save(state)
        elif a.command == "remove":
            state = tree.load()
            for n in a.args:
                if n in state["mods"]:
                    shutil.rmtree(os.path.join(tree.mods, n))
            tree.save(tree.load())
        elif a.command == "order":
            state = tree.load()
            state["order"] = [n for n in a.args if n in state["mods"]] + [n for n in state["order"] if n not in a.args]
            tree.save(state)
        elif a.command == "apply":
            result = apply(tree, say)
            if a.json:
                print(json.dumps({"result": result}), flush=True)
        elif a.command == "vanilla":
            vanilla_cfg(tree)
            shutil.copyfile(tree.vanilla_cfg, tree.cfg)
            say("the game as it is (the mods stay on the list)")
        else:
            raise Error(f"unknown command {a.command}")
    except Error as e:
        print(json.dumps({"error": str(e)}) if a.json else f"error: {e}", flush=True)
        return 1
    except (OSError, urllib.error.URLError) as e:
        print(json.dumps({"error": str(e)}) if a.json else f"error: {e}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
