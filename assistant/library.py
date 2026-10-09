"""The second brain's library: your documents, searchable by Vesper.

Vesper reads the folders you choose (by default the usual places on this PC: Documents, Desktop, OneDrive, Google
Drive, Dropbox) and keeps a word index of what's in them in data/library.db, so it can answer "what did the vendor
proposal say about support hours?" from your own files, say which file it came from, and open it.

- Word, PowerPoint, Excel (text only), PDF, Markdown and text files. A native Google Doc in Google Drive for
  desktop (.gdoc) is a link, not a file: it's found by name and opens in the browser.
- Files OneDrive keeps online only (not downloaded to this PC) are indexed by name only: reading them would
  download the whole drive.
- Only what changed since the last pass is read again, and nothing leaves this PC: the index is a file in data/.

Reading a document is reading text someone may have written for a model to obey (a downloaded PDF, a pasted
email), so the tools that return it are marked untrusted (see brain/tools.py): actions after them need a yes.
"""

from __future__ import annotations

import fnmatch
import html
import logging
import os
import re
import sqlite3
import string
import sys
import threading
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from .storage import STOPWORDS, _spellings, _words

log = logging.getLogger(__name__)

TEXT = {".md", ".markdown", ".txt", ".text", ".org", ".rst"}
OFFICE = {".docx", ".pptx", ".xlsx"}
PDF = {".pdf"}
LINKS = {".gdoc", ".gsheet", ".gslides"}  # Google Drive for desktop: a pointer to a doc that lives online
DOCUMENTS = TEXT | OFFICE | PDF | LINKS

# Folders that hold programs, caches or copies of other things, never your writing.
SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "appdata", "$recycle.bin", "system volume information",
             "windowsapps", "my games", "venv", "bower_components"}
SKIP_NAMES = ("~$", ".~lock", "~wrl")  # Office lock and temp files
# Reading a whole drive ("this whole PC"): Windows, programs and games are someone else's files, not your writing.
SYSTEM_DIRS = {"windows", "windows.old", "program files", "program files (x86)", "programdata", "recovery",
               "perflogs", "msocache", "$windows.~bt", "$windows.~ws", "$sysreset", "intel", "amd", "nvidia",
               "drivers", "steamlibrary", "steamapps", "steam", "epic games", "riot games", "battle.net",
               "gog games", "xboxgames", "origin games", "ea games", "ubisoft", "ubisoft game launcher", "msys64",
               "cygwin64", "python27", "anaconda3", "miniconda3", "android", "sdk"}
WHOLE_PC_TYPES = OFFICE | PDF | LINKS | {".md", ".markdown"}  # not .txt: outside your folders that's logs and readmes
FULL_EVERY_S = 24 * 3600  # the whole PC once a day; your own folders every pass

# Windows file attributes (os.stat().st_file_attributes)
HIDDEN, SYSTEM, OFFLINE = 0x2, 0x4, 0x1000
RECALL_ON_OPEN, RECALL_ON_DATA_ACCESS = 0x40000, 0x400000  # OneDrive "online only": reading downloads it
MOUNT_POINT, SYMLINK = 0xA0000003, 0xA000000C  # reparse tags: junctions ("My Music" in Documents) and links

CHUNK = 1000      # characters per indexed passage: big enough to hold a thought, small enough to quote
MAX_CHARS = 400_000  # per document: past that it's a data dump, not notes
MAX_PDF_PAGES = 300
MAX_XML_BYTES = 40 * 1024 * 1024  # an Office part bigger than this when unzipped is a zip bomb, not a document

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    folder TEXT NOT NULL,
    title TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    indexed REAL NOT NULL,
    status TEXT NOT NULL,      -- ok | online (name only) | empty (no text, e.g. a scan) | too_big | link | error
    url TEXT,
    passages INTEGER NOT NULL DEFAULT 0, -- not "chunks": that name is the search table's
    priority INTEGER NOT NULL DEFAULT 1  -- 1: one of your folders; 0: elsewhere on the PC (ranked after yours)
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS files_mtime ON files(mtime);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks USING fts5(title, text, file_id UNINDEXED, loc UNINDEXED,
                                                     tokenize='porter unicode61');
"""


@dataclass
class Section:
    loc: str   # where in the document: "p. 4", "slide 2", "Budget" (a sheet); "" for plain text
    text: str


# ---------------------------------------------------------------------------
# Which folders
# ---------------------------------------------------------------------------
def default_folders(env: dict | None = None, home: Path | None = None) -> list[Path]:
    """The usual places your documents live on this PC, the ones that exist, without one inside another:
    Documents and Desktop (wherever Windows has moved them), every OneDrive you're signed in to, Google Drive for
    desktop's My Drive for each account (its own drive letter: J:\\My Drive, K:\\My Drive), Dropbox."""
    real = env is None and home is None
    if real and _cache.get("folders") and time.time() - _cache["folders"][0] < CACHE_S:
        return list(_cache["folders"][1])
    env = dict(os.environ) if env is None else env
    home = home or Path.home()
    shell = _shell_folders() if real else {}
    found: list[Path] = [shell.get("Personal"), shell.get("Desktop"), home / "Documents", home / "Desktop"]
    found += [Path(env[v]) for v in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer") if env.get(v)]
    found += _onedrive_roots() if real else []
    found += [home / "Dropbox", home / "Google Drive", home / "My Drive"]
    out = _outermost([p for p in found if p is not None and _is_dir(p)] + (google_drives() if real else []))
    if real:
        _cache["folders"] = (time.time(), out)
    return out


CACHE_S = 600  # where the folders are doesn't change often; finding them can take a second or two
_cache: dict[str, tuple[float, list]] = {}


def _shell_folders() -> dict[str, Path]:
    """Documents and Desktop as Windows has them, which may be moved (into OneDrive, say)."""
    if sys.platform != "win32":
        return {}
    try:
        import winreg

        out = {}
        key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            for name in ("Personal", "Desktop"):
                try:
                    out[name] = Path(os.path.expandvars(winreg.QueryValueEx(k, name)[0]))
                except OSError:
                    pass
        return out
    except OSError:
        return {}


def _onedrive_roots() -> list[Path]:
    """Every OneDrive signed in on this PC (personal and work), from OneDrive's own settings."""
    if sys.platform != "win32":
        return []
    try:
        import winreg

        out = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\OneDrive\Accounts") as accounts:
            for i in range(64):
                try:
                    name = winreg.EnumKey(accounts, i)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(accounts, name) as account:
                        out.append(Path(winreg.QueryValueEx(account, "UserFolder")[0]))
                except OSError:
                    continue
        return out
    except OSError:
        return []


def drive_letters() -> list[str]:
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        mask = ctypes.windll.kernel32.GetLogicalDrives()
        return [c for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1 and c not in "AB"]
    except (AttributeError, OSError):
        return []


def local_drives() -> list[Path]:
    """The drives in this PC (fixed and removable), for reading the whole PC. Not network drives, and not Google
    Drive's letters: those are cloud drives, read through their My Drive."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        roots = [Path(f"{c}:/") for c in drive_letters() if k32.GetDriveTypeW(f"{c}:\\") in (2, 3)]
    except (AttributeError, OSError):
        return []
    google = {str(p)[:2].upper() for p in google_drives()}
    return [r for r in roots if str(r)[:2].upper() not in google]


def google_drives() -> list[Path]:
    """Google Drive for desktop gives each account a drive letter with My Drive in it. A disconnected network
    drive can take many seconds to answer, so every letter is asked at once and a slow one is left out."""
    return [p for p in _dirs_within([Path(f"{d}:/My Drive") for d in drive_letters()])]


def _dirs_within(paths: list[Path], timeout: float = 2.0) -> list[Path]:
    """The paths that are folders, asking in parallel and giving up on any that hangs (a dead network drive)."""
    answers: dict[int, bool] = {}

    def ask(i: int, p: Path) -> None:
        answers[i] = _is_dir(p)
    threads = [threading.Thread(target=ask, args=(i, p), daemon=True) for i, p in enumerate(paths)]
    for t in threads:
        t.start()
    deadline = time.time() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    return [p for i, p in enumerate(paths) if answers.get(i)]


MEDIA = {"music", "videos", "pictures", "photos", "saved games", "3d objects", "camera roll", "screenshots",
         "obs recordings", "recordings"}


def quick_access() -> list[Path]:
    """The folders pinned in File Explorer's Quick access (Home): where you actually keep things."""
    if sys.platform != "win32":
        return []
    import subprocess

    ps = ("(New-Object -ComObject Shell.Application).Namespace('shell:::{679f85cb-0220-4080-b29b-5540cc05aab6}')"
          ".Items() | ForEach-Object { $_.Path }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True,
                             text=True, timeout=20, creationflags=0x08000000).stdout  # CREATE_NO_WINDOW
    except (OSError, subprocess.SubprocessError):
        return []
    return [Path(line.strip()) for line in out.splitlines() if re.match(r"^[A-Za-z]:\\", line.strip())]


def suggestions(current: list[Path], home: Path | None = None, pinned: list[Path] | None = None) -> list[dict]:
    """Folders worth adding that the library doesn't read yet: your pinned folders, Downloads, and the Shared
    drives of a Google account. Never a whole drive, your user folder, or a folder of photos or video."""
    real = pinned is None
    key = "suggest:" + "|".join(map(str, current))
    if real and _cache.get(key) and time.time() - _cache[key][0] < CACHE_S:
        return list(_cache[key][1])
    home = home or Path.home()
    pinned = quick_access() if real else pinned
    candidates = [(p, "pinned in File Explorer") for p in pinned]
    candidates += [(home / "Downloads", "Downloads")]
    candidates += [(Path(str(c)[:2] + "/Shared drives"), "Google shared drives") for c in current
                   if c.name == "My Drive"]
    cur = [_norm(c) for c in current]
    out, seen = [], set()
    for path, why in candidates:  # no disk access until the timed check below: L:\\ may be a dead network drive
        r = _norm(path)
        if r in seen or r.parent == r or r == _norm(home) or r.name.lower() in MEDIA:
            continue
        if any(r == c or c in r.parents for c in cur):  # already read
            continue
        seen.add(r)
        out.append((path, why))
    found = set(_dirs_within([p for p, _ in out]))
    result = [{"path": str(p), "why": why} for p, why in out if p in found][:12]
    if real:
        _cache[key] = (time.time(), result)
    return result


def _under(path: str, roots: list[Path]) -> bool:
    p = _norm(Path(path))
    return any(p == r or r in p.parents for r in roots)


def _norm(p: Path) -> Path:
    """An absolute, case-folded path for comparing, without touching the disk."""
    return Path(os.path.normcase(os.path.abspath(str(p))))


def _is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _outermost(paths: list[Path]) -> list[Path]:
    """Drop folders that sit inside another one in the list (Documents moved into OneDrive), and duplicates."""
    resolved = []
    for p in paths:
        try:
            r = p.resolve()
        except OSError:
            r = p
        if r not in resolved:
            resolved.append(r)
    return [p for p in resolved if not any(o != p and o in p.parents for o in resolved)]


def title_of(path: Path) -> str:
    """"Q3_vendor-review (final).docx" -> "Q3 vendor review (final)": what you'd say out loud."""
    return re.sub(r"\s+", " ", re.sub(r"[_\-.]+", " ", path.stem)).strip() or path.name


# ---------------------------------------------------------------------------
# Reading documents
# ---------------------------------------------------------------------------
def extract(path: Path) -> list[Section]:
    """The text of one document, in sections that say where each part is. Raises ValueError when unreadable."""
    ext = path.suffix.lower()
    if ext in TEXT:
        return [Section("", _read_text(path))]
    if ext == ".docx":
        return [Section("", _docx(path))]
    if ext == ".pptx":
        return _pptx(path)
    if ext == ".xlsx":
        return _xlsx(path)
    if ext in PDF:
        return _pdf(path)
    raise ValueError(f"not a document: {ext}")


def _read_text(path: Path) -> str:
    raw = path.read_bytes()[: MAX_CHARS * 4]
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _xml_part(z: zipfile.ZipFile, name: str) -> str:
    info = z.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"{name} is {info.file_size // 1_000_000} MB unzipped")
    return z.read(name).decode("utf-8", errors="replace")


def _xml_text(xml: str, para: str) -> str:
    """Word/PowerPoint XML to plain text: a line per paragraph, tabs kept, every tag dropped."""
    xml = re.sub(rf"</{para}>", "\n", xml)
    xml = re.sub(r"<(?:w:tab|w:br|a:br)\b[^>]*/>", lambda m: "\t" if "tab" in m.group(0) else "\n", xml)
    text = html.unescape(re.sub(r"<[^>]+>", "", xml))
    return re.sub(r"[ \t]*\n\s*\n+", "\n\n", text).strip()


def _docx(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        parts = ["word/document.xml"] + sorted(n for n in z.namelist() if re.fullmatch(r"word/(?:foot|end)notes\.xml", n))
        return "\n\n".join(_xml_text(_xml_part(z, n), "w:p") for n in parts if n in z.namelist())


def _pptx(path: Path) -> list[Section]:
    with zipfile.ZipFile(path) as z:
        names = z.namelist()

        def num(n: str) -> int:
            return int(re.search(r"(\d+)\.xml$", n).group(1))
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=num)
        out = []
        for n in slides:
            text = _xml_text(_xml_part(z, n), "a:p")
            notes = f"ppt/notesSlides/notesSlide{num(n)}.xml"
            if notes in names:
                extra = _xml_text(_xml_part(z, notes), "a:p")
                text += f"\n\nSpeaker notes: {extra}" if extra.strip() else ""
            if text.strip():
                out.append(Section(f"slide {num(n)}", text))
        return out


def _xlsx(path: Path) -> list[Section]:
    """Sheet names and the words in cells (numbers alone aren't worth searching)."""
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        sheets = re.findall(r'<sheet\b[^>]*\bname="([^"]+)"', _xml_part(z, "xl/workbook.xml")) if "xl/workbook.xml" in names else []
        words = []
        if "xl/sharedStrings.xml" in names:
            words = [html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
                     for s in re.findall(r"<si>(.*?)</si>", _xml_part(z, "xl/sharedStrings.xml"), re.S)]
        text = "\n".join(w for w in words if w and not re.fullmatch(r"[\d\s.,%$€£:/-]+", w))
        head = f"Sheets: {', '.join(html.unescape(s) for s in sheets)}" if sheets else ""
        return [Section("", f"{head}\n{text}".strip())]


def _pdf(path: Path) -> list[Section]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # an install from before pypdf was a requirement: run Update Vesper
        raise ValueError("PDF support isn't installed (run Update Vesper)") from exc
    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:
            raise ValueError("password-protected") from exc
    out = []
    for i, page in enumerate(reader.pages[:MAX_PDF_PAGES], 1):
        try:
            text = page.extract_text() or ""
        except Exception:  # one broken page shouldn't lose the rest
            continue
        if text.strip():
            out.append(Section(f"p. {i}", text))
    return out


def chunks_of(sections: list[Section], size: int = CHUNK) -> Iterator[tuple[str, str]]:
    """(loc, text) passages of about ``size`` characters, split at paragraph then sentence boundaries."""
    total = 0
    for sec in sections:
        buf = ""
        for para in re.split(r"\n\s*\n", sec.text):
            para = re.sub(r"[ \t]+", " ", para).strip()
            if not para:
                continue
            while len(para) > size:  # a wall of text: cut at the last sentence end that fits
                cut = max(para.rfind(". ", 0, size), para.rfind("\n", 0, size))
                cut = cut + 1 if cut > size // 3 else size
                if buf:
                    yield sec.loc, buf
                    buf = ""
                yield sec.loc, para[:cut].strip()
                total += cut
                para = para[cut:].strip()
            if len(buf) + len(para) + 2 > size and buf:
                yield sec.loc, buf
                buf = ""
            buf = f"{buf}\n\n{para}" if buf else para
            total += len(para)
            if total > MAX_CHARS:
                break
        if buf:
            yield sec.loc, buf
        if total > MAX_CHARS:
            return


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------
class Library:
    def __init__(self, path: Path | str, folders: Callable[[], list[Path]] | list[Path] | None = None,
                 exclude: list[str] | None = None, max_file_mb: float = 25, max_files: int = 20_000,
                 pause: float = 0.002, whole_pc: Callable[[], bool] | bool = False,
                 drives: Callable[[], list[Path]] = local_drives, skip_paths: list[Path] | None = None,
                 cloud: Callable[[dict, str], str | None] | None = None):
        self.path = str(path)
        self._folders = folders if callable(folders) else (lambda f=list(folders or []): f)
        self._whole_pc = whole_pc if callable(whole_pc) else (lambda w=bool(whole_pc): w)
        self._drives = drives
        self.skip_paths = [_norm(p) for p in skip_paths or []]  # Vesper's own folder: its docs aren't yours
        # The text behind a Google Docs/Sheets/Slides link (integrations/google.py), or None when no Google
        # account is connected: then the link is known by name only.
        self.cloud = cloud
        self.exclude = [e.lower() for e in exclude or []]
        self.max_bytes = int(max_file_mb * 1024 * 1024)
        self.max_files = max_files
        self.pause = pause  # between files: indexing is background work, never in the way of the voice loop
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(files)")}
            if "priority" not in cols:  # an index made before "this whole PC" existed: all of it is your folders
                self._db.execute("ALTER TABLE files ADD COLUMN priority INTEGER NOT NULL DEFAULT 1")
            self._db.commit()
        self.running = False
        self.progress: dict = {}
        self.last: dict = {}  # the last pass: when, how many, how long
        self._stop = threading.Event()
        self._dirty = 0

    # ---- folders -------------------------------------------------------
    def folders(self) -> list[Path]:
        try:
            return _outermost([Path(os.path.expandvars(str(f))).expanduser() for f in self._folders()])
        except Exception:  # a bad setting must not stop search
            log.exception("library: reading the folder list failed")
            return []

    def whole_pc(self) -> bool:
        try:
            return bool(self._whole_pc())
        except Exception:
            return False

    def drives(self) -> list[Path]:
        """The drive roots read in "this whole PC" mode; none otherwise."""
        if not self.whole_pc():
            return []
        try:
            return [d for d in self._drives() if _norm(d) not in self.skip_paths]
        except Exception:
            log.exception("library: listing drives failed")
            return []

    def _meta(self, key: str, value: str | None = None) -> str | None:
        with self._lock:
            if value is not None:
                self._db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
                self._db.commit()
                return value
            row = self._db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def full_due(self) -> bool:
        """The whole PC is read once a day; the passes between only look at your own folders."""
        return self.whole_pc() and time.time() - float(self._meta("last_full") or 0) > FULL_EVERY_S

    def _excluded(self, path: Path) -> bool:
        s = str(path).lower().replace("\\", "/")
        return any(fnmatch.fnmatch(s, pat.replace("\\", "/")) for pat in self.exclude)

    def walk(self, folder: Path, strict: bool = False, avoid: list[Path] | None = None
             ) -> Iterator[tuple[Path, os.stat_result]]:
        """Every document under ``folder``, with its stat; never follows junctions or links (loops).

        ``strict`` (a whole drive, outside your folders): leaves out Windows, programs and games (a folder with a
        .dll in it is a program), code repositories (a .git), Vesper's own folder, and plain text files (logs and
        readmes); ``avoid`` are your folders, walked on their own."""
        avoid_n = [_norm(a) for a in avoid or []]
        stack = [folder]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            if strict and d != folder and any(
                    e.name.lower().endswith(".dll") or e.name == ".git" for e in entries):
                continue  # a program, a game or a repo
            for e in entries:
                name = e.name
                try:
                    st = e.stat(follow_symlinks=False)
                    attrs = getattr(st, "st_file_attributes", 0)
                    if e.is_symlink() or getattr(st, "st_reparse_tag", 0) in (MOUNT_POINT, SYMLINK):
                        continue
                    if attrs & (HIDDEN | SYSTEM) or name.startswith("."):
                        continue
                    if e.is_dir(follow_symlinks=False):
                        low = name.lower()
                        if low in SKIP_DIRS or self._excluded(Path(e.path)):
                            continue
                        if strict and (low in SYSTEM_DIRS or _norm(Path(e.path)) in avoid_n
                                       or _norm(Path(e.path)) in self.skip_paths):
                            continue
                        stack.append(Path(e.path))
                        continue
                except OSError:
                    continue
                types = WHOLE_PC_TYPES if strict else DOCUMENTS
                if Path(name).suffix.lower() in types and not name.startswith(SKIP_NAMES) \
                        and not self._excluded(Path(e.path)):
                    yield Path(e.path), st

    def index(self, on_progress: Callable[[dict], None] | None = None, scope: str | None = None) -> dict:
        """One pass: read what's new or changed, drop what's gone. Safe to call again.

        ``scope``: "all" (your folders, and every drive in "this whole PC" mode), "yours" (your folders only), or
        None: the whole PC once a day, your folders the rest of the time."""
        with self._lock:
            if self.running:
                return {"ok": True, "running": True}
            self.running = True
        self._stop.clear()
        t0 = time.time()
        scope = scope or ("all" if self.full_due() or not self.whole_pc() else "yours")
        counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "online": 0, "failed": 0}
        seen: set[str] = set()
        folders, drives = self.folders(), self.drives()
        roots = [(f, False) for f in folders] + ([(d, True) for d in drives] if scope == "all" else [])
        known = {r["path"]: r for r in self._query("SELECT path, size, mtime, folder, priority, ext, indexed FROM files")}
        capped = False
        try:
            for root, strict in roots:
                for path, st in self.walk(root, strict=strict, avoid=folders if strict else None):
                    if self._stop.is_set():
                        break
                    if len(seen) >= self.max_files:
                        capped = True
                        break
                    key = str(path)
                    seen.add(key)
                    old = known.get(key)
                    stale_link = old and old["ext"] in LINKS and time.time() - old["indexed"] > FULL_EVERY_S
                    if old and old["size"] == st.st_size and abs(old["mtime"] - st.st_mtime) < 1 and not stale_link:
                        counts["unchanged"] += 1
                        if old["folder"] != str(root) or old["priority"] != int(not strict):  # a folder you added
                            with self._lock:
                                self._db.execute("UPDATE files SET folder=?, priority=? WHERE path=?",
                                                 (str(root), int(not strict), key))
                        continue
                    status = self._index_file(path, st, root, priority=not strict)
                    counts["online" if status == "online" else "failed" if status == "error" else
                           "updated" if old else "added"] += 1
                    self.progress = {"running": True, "seen": len(seen), "current": path.name, **counts}
                    if on_progress and (counts["added"] + counts["updated"]) % 25 == 1:
                        on_progress(dict(self.progress))
                    time.sleep(self.pause)
            if not self._stop.is_set():
                walked = [_norm(r) for r, _ in roots]
                current = [_norm(r) for r in folders + drives]
                # gone: under a place this pass walked fully (not stopped at the file limit) and not seen, or no
                # longer under anything that's read (a folder taken off the list, "this whole PC" turned off)
                gone = [p for p in known if p not in seen and (
                    (not capped and _under(p, walked)) or not _under(p, current))]
                for p in gone:
                    self._remove(p)
                counts["removed"] = len(gone)
                if scope == "all" and drives and not capped:
                    self._meta("last_full", str(time.time()))
        finally:
            with self._lock:
                self._db.commit()
                self.running = False
            self.progress = {}
        self.last = {"at": time.time(), "seconds": round(time.time() - t0, 1), "capped": capped, "scope": scope,
                     **counts}
        log.info("library: %s", self.last)
        return {"ok": True, **self.last}

    def stop(self) -> None:
        self._stop.set()

    def _index_file(self, path: Path, st: os.stat_result, folder: Path, priority: bool = True) -> str:
        ext = path.suffix.lower()
        attrs = getattr(st, "st_file_attributes", 0)
        url, sections, status = None, [], "ok"
        if attrs & (OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS):
            status = "online"
        elif ext in LINKS:
            from .integrations.google import read_link

            status, link = "link", read_link(path)
            url = link.get("url") or None
            if self.cloud and link.get("doc_id"):
                try:
                    text = self.cloud(link, ext)
                    if text and text.strip():
                        sections, status = [Section("", text)], "ok"
                except Exception as exc:  # not shared with a connected account, blocked by an admin, offline
                    log.debug("library: no text for %s: %s", path.name, exc)
        elif st.st_size > self.max_bytes:
            status = "too_big"
        else:
            try:
                sections = extract(path)
                if not any(s.text.strip() for s in sections):
                    status = "empty"
            except Exception as exc:  # zip errors, broken PDFs, permissions: note it and carry on
                log.debug("library: can't read %s: %s", path, exc)
                status = "error"
        title = title_of(path)
        passages = list(chunks_of(sections)) or [("", "")]  # a name-only row still finds the file by name
        with self._lock:
            row = self._db.execute("SELECT id FROM files WHERE path=?", (str(path),)).fetchone()
            if row:
                self._db.execute("DELETE FROM chunks WHERE file_id=?", (row["id"],))
                self._db.execute("UPDATE files SET folder=?, title=?, ext=?, size=?, mtime=?, indexed=?, status=?, "
                                 "url=?, passages=?, priority=? WHERE id=?",
                                 (str(folder), title, ext, st.st_size, st.st_mtime, time.time(), status, url,
                                  len(passages), int(priority), row["id"]))
                fid = row["id"]
            else:
                fid = self._db.execute(
                    "INSERT INTO files(path, folder, title, ext, size, mtime, indexed, status, url, passages, priority) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (str(path), str(folder), title, ext, st.st_size, st.st_mtime,
                                                       time.time(), status, url, len(passages), int(priority))
                ).lastrowid
            self._db.executemany("INSERT INTO chunks(title, text, file_id, loc) VALUES (?,?,?,?)",
                                 [(title, text, fid, loc) for loc, text in passages])
            self._dirty += 1
            if self._dirty >= 50:  # a first pass can take a while: keep what's done if Vesper closes mid-way
                self._db.commit()
                self._dirty = 0
        return status

    def _remove(self, path: str) -> None:
        with self._lock:
            row = self._db.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
            if row:
                self._db.execute("DELETE FROM chunks WHERE file_id=?", (row["id"],))
                self._db.execute("DELETE FROM files WHERE id=?", (row["id"],))

    def _query(self, sql: str, args: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---- asking it ---------------------------------------------------------
    def search(self, query: str, limit: int = 5) -> list[dict]:
        """The best passage from each of the best-matching documents; an empty query = the latest documents."""
        limit = max(1, min(int(limit or 5), 20))
        terms = [w for w in _words(query) if w not in STOPWORDS]
        terms += [v for t in terms for v in _spellings(t) if v not in terms]
        if not terms:
            return [self._hit(r) for r in self._query(
                "SELECT *, '' AS snippet, '' AS loc FROM files ORDER BY mtime DESC LIMIT ?", (limit,))]
        match = " OR ".join(f'"{t}"*' if len(t) > 3 else f'"{t}"' for t in terms)
        try:
            rows = self._query(
                "SELECT f.*, c.loc, snippet(chunks, 1, '[', ']', ' … ', 28) AS snippet, "
                "bm25(chunks, 4.0, 1.0) * CASE f.priority WHEN 1 THEN 1.6 ELSE 1.0 END AS score "  # yours first
                "FROM chunks c JOIN files f ON f.id = c.file_id "
                "WHERE chunks MATCH ? ORDER BY score LIMIT ?", (match, limit * 8))
        except sqlite3.OperationalError:
            return []
        best: dict[int, dict] = {}
        for r in rows:  # one passage per document: the best one
            best.setdefault(r["id"], r)
        return [self._hit(r) for r in list(best.values())[:limit]]

    def _hit(self, r: dict) -> dict:
        when = time.strftime("%b %d, %Y", time.localtime(r["mtime"])).replace(" 0", " ")
        out = {"title": r["title"], "file": Path(r["path"]).name, "path": r["path"],
               "folder": Path(r["folder"]).name or r["folder"], "modified": when, "where": r.get("loc") or "",
               "passage": _flat(r.get("snippet") or "")}
        if r["status"] == "online":
            out["note"] = "online only: not downloaded to this PC, so only its name is known"
        elif r["status"] == "link":
            out["note"] = "a Google document only known by name: connect its Google account (Setup) to read it"
        elif r["status"] in ("empty", "too_big", "error"):
            out["note"] = {"empty": "no text in it (a scan or images)", "too_big": "too big to read",
                           "error": "couldn't be read"}[r["status"]]
        return out

    def find(self, name: str) -> dict | None:
        """The document someone means by a name they said ("the Q3 plan"): best title match, newest first."""
        terms = [w for w in _words(name) if w not in STOPWORDS | {"doc", "document", "file", "pdf", "deck", "sheet"}]
        if not terms:
            return None
        like = " OR ".join(["lower(title) LIKE ?"] * len(terms))
        said = name.strip().strip('"').lower()
        tail = said.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in tail:  # a file name or path, from a search result
            for r in self._query("SELECT * FROM files WHERE instr(lower(path), ?) > 0 ORDER BY mtime DESC", (tail,)):
                if r["path"].lower() == said or Path(r["path"]).name.lower() == tail:
                    return r
        rows = self._query(f"SELECT * FROM files WHERE {like} ORDER BY mtime DESC LIMIT 200",
                           tuple(f"%{t}%" for t in terms))

        def score(r: dict) -> tuple:
            words = set(_words(r["title"]))
            return (sum(t in words for t in terms) + 0.5 * sum(any(w.startswith(t) for w in words) for t in terms),
                    -len(words), r["mtime"])
        best = max(rows, key=score) if rows else None
        return best if best and score(best)[0] > 0 else None  # "plan" is in "airplane", but that's not it

    def read(self, name: str, max_chars: int = 6000) -> dict:
        doc = self.find(name)
        if not doc:
            loose = Path(name.strip().strip('"'))
            if loose.is_absolute() and loose.suffix.lower() in DOCUMENTS - LINKS and loose.is_file():
                return self._read_loose(loose, max_chars)  # any document on the PC, by its path
            return {"ok": False, "error": f"No document called {name} in your library."}
        if doc["status"] in ("online", "link"):
            return {"ok": True, **self._hit({**doc, "loc": "", "snippet": ""}), "text": ""}
        parts, total = [], 0
        for r in self._query("SELECT loc, text FROM chunks WHERE file_id=? ORDER BY rowid", (doc["id"],)):
            piece = f"[{r['loc']}] {r['text']}" if r["loc"] else r["text"]
            parts.append(piece)
            total += len(piece)
            if total >= max_chars:
                break
        text = "\n\n".join(parts)
        return {"ok": True, **self._hit({**doc, "loc": "", "snippet": ""}), "text": text[:max_chars],
                "truncated": total > max_chars or len(text) > max_chars}

    def _read_loose(self, path: Path, max_chars: int) -> dict:
        """A document that isn't in the library (a program's folder, a path you gave): read straight from disk."""
        try:
            if path.stat().st_size > self.max_bytes:
                return {"ok": False, "error": f"{path.name} is too big to read."}
            sections = extract(path)
        except Exception as exc:
            return {"ok": False, "error": f"Can't read {path.name}: {exc}"[:200]}
        text = "\n\n".join(f"[{s.loc}] {s.text}" if s.loc else s.text for s in sections)
        return {"ok": True, "title": title_of(path), "file": path.name, "path": str(path), "folder": path.parent.name,
                "where": "", "passage": "", "text": text[:max_chars], "truncated": len(text) > max_chars,
                "note": "read from disk: it isn't in the library"}

    def reread_links(self) -> int:
        """A Google account was connected or removed: read every Google Docs/Sheets/Slides link again."""
        with self._lock:
            n = self._db.execute(f"UPDATE files SET mtime=0 WHERE ext IN ({','.join('?' * len(LINKS))})",
                                 tuple(sorted(LINKS))).rowcount
            self._db.commit()
        return n

    def google_counts(self) -> dict:
        """Google Docs/Sheets/Slides: how many are read, and how many only known by name."""
        rows = self._query(f"SELECT status, COUNT(*) AS n FROM files WHERE ext IN ({','.join('?' * len(LINKS))}) "
                           "GROUP BY status", tuple(sorted(LINKS)))
        by = {r["status"]: r["n"] for r in rows}
        return {"read": by.get("ok", 0), "names_only": by.get("link", 0)}

    def count(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM files")[0]["n"]

    def status(self) -> dict:
        counts = {r["status"]: r["n"] for r in self._query("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        by_folder = {r["folder"]: r["n"] for r in self._query("SELECT folder, COUNT(*) AS n FROM files GROUP BY folder")}
        return {"files": sum(counts.values()), "by_status": counts, "running": self.running,
                "progress": self.progress, "last": self.last, "whole_pc": self.whole_pc(),
                "last_full": float(self._meta("last_full") or 0) or None,
                "folders": [{"path": str(f), "files": by_folder.get(str(f), 0)} for f in self.folders()],
                "drives": [{"path": str(d), "files": by_folder.get(str(d), 0)} for d in self.drives()]}

    def close(self) -> None:
        with self._lock:
            self._db.close()


def _flat(text: str) -> str:
    """One line, read aloud well: a paragraph break becomes a full stop unless the line already ended one."""
    text = re.sub(r"(?<![.!?:;,\]])\s*\n\s*\n\s*", ". ", text.strip())
    return re.sub(r"\s+", " ", text).strip()


def open_path(doc: dict) -> dict:
    """Open a library document the way double-clicking it would (a Google doc: in the browser)."""
    import subprocess
    import webbrowser

    if doc.get("status") == "link" and doc.get("url"):
        webbrowser.open(doc["url"])
        return {"ok": True, "opened": doc["title"]}
    path = doc["path"]
    if Path(path).suffix.lower() not in DOCUMENTS or not Path(path).exists():
        return {"ok": False, "error": f"Can't open {Path(path).name}."}
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - only files the library indexed, and only document types
    else:
        subprocess.Popen(["xdg-open" if sys.platform != "darwin" else "open", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"ok": True, "opened": doc["title"]}
