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
import json
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
    passages INTEGER NOT NULL DEFAULT 0  -- not "chunks": that name is the search table's
);
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
    """The usual places your documents live on this PC, the ones that exist, without one inside another."""
    env = dict(os.environ) if env is None else env
    home = home or Path.home()
    shell = _shell_folders() if env is os.environ or env == dict(os.environ) else {}
    found: list[Path] = [shell.get("Personal") or home / "Documents", shell.get("Desktop") or home / "Desktop"]
    for var in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        if env.get(var):
            found.append(Path(env[var]))
    found += [home / "Dropbox", home / "Google Drive", home / "My Drive"]
    found += [Path(f"{d}:/My Drive") for d in _local_drives()]  # Google Drive for desktop: G:\My Drive
    return _outermost([p for p in found if _is_dir(p)])


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


def _local_drives() -> list[str]:
    """Drive letters on this PC that aren't network drives (a disconnected one can hang for seconds)."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        mask = k32.GetLogicalDrives()
        letters = [c for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1 and c not in "AB"]
        return [c for c in letters if k32.GetDriveTypeW(f"{c}:\\") in (2, 3)]  # removable, fixed
    except (AttributeError, OSError):
        return []


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
                 pause: float = 0.002):
        self.path = str(path)
        self._folders = folders if callable(folders) else (lambda f=list(folders or []): f)
        self.exclude = [e.lower() for e in exclude or []]
        self.max_bytes = int(max_file_mb * 1024 * 1024)
        self.max_files = max_files
        self.pause = pause  # between files: indexing is background work, never in the way of the voice loop
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(SCHEMA)
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

    def _excluded(self, path: Path) -> bool:
        s = str(path).lower().replace("\\", "/")
        return any(fnmatch.fnmatch(s, pat.replace("\\", "/")) for pat in self.exclude)

    def walk(self, folder: Path) -> Iterator[tuple[Path, os.stat_result]]:
        """Every document under ``folder``, with its stat; never follows junctions or links (loops)."""
        stack = [folder]
        while stack:
            d = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
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
                        if name.lower() not in SKIP_DIRS and not self._excluded(Path(e.path)):
                            stack.append(Path(e.path))
                        continue
                except OSError:
                    continue
                if Path(name).suffix.lower() in DOCUMENTS and not name.startswith(SKIP_NAMES) \
                        and not self._excluded(Path(e.path)):
                    yield Path(e.path), st

    def index(self, on_progress: Callable[[dict], None] | None = None) -> dict:
        """One pass over every folder: read what's new or changed, drop what's gone. Safe to call again."""
        with self._lock:
            if self.running:
                return {"ok": True, "running": True}
            self.running = True
        self._stop.clear()
        t0 = time.time()
        counts = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0, "online": 0, "failed": 0}
        seen: set[str] = set()
        folders = self.folders()
        known = {r["path"]: (r["size"], r["mtime"]) for r in self._query("SELECT path, size, mtime FROM files")}
        capped = False
        try:
            for folder in folders:
                for path, st in self.walk(folder):
                    if self._stop.is_set():
                        break
                    if len(seen) >= self.max_files:
                        capped = True
                        break
                    key = str(path)
                    seen.add(key)
                    old = known.get(key)
                    if old and old[0] == st.st_size and abs(old[1] - st.st_mtime) < 1:
                        counts["unchanged"] += 1
                        continue
                    status = self._index_file(path, st, folder)
                    counts["online" if status == "online" else "failed" if status == "error" else
                           "updated" if old else "added"] += 1
                    self.progress = {"running": True, "seen": len(seen), "current": path.name, **counts}
                    if on_progress and (counts["added"] + counts["updated"]) % 25 == 1:
                        on_progress(dict(self.progress))
                    time.sleep(self.pause)
            if not self._stop.is_set():
                gone = [p for p in known if p not in seen and (capped is False or not any(
                    p.startswith(str(f)) for f in folders))]
                for p in gone:
                    self._remove(p)
                counts["removed"] = len(gone)
        finally:
            with self._lock:
                self._db.commit()
                self.running = False
            self.progress = {}
        self.last = {"at": time.time(), "seconds": round(time.time() - t0, 1), "capped": capped, **counts}
        log.info("library: %s", self.last)
        return {"ok": True, **self.last}

    def stop(self) -> None:
        self._stop.set()

    def _index_file(self, path: Path, st: os.stat_result, folder: Path) -> str:
        ext = path.suffix.lower()
        attrs = getattr(st, "st_file_attributes", 0)
        url, sections, status = None, [], "ok"
        if attrs & (OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS):
            status = "online"
        elif ext in LINKS:
            status = "link"
            try:
                url = json.loads(path.read_text(encoding="utf-8", errors="replace")).get("url")
            except (OSError, ValueError, AttributeError):
                url = None
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
                                 "url=?, passages=? WHERE id=?", (str(folder), title, ext, st.st_size, st.st_mtime,
                                                                time.time(), status, url, len(passages), row["id"]))
                fid = row["id"]
            else:
                fid = self._db.execute(
                    "INSERT INTO files(path, folder, title, ext, size, mtime, indexed, status, url, passages) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)", (str(path), str(folder), title, ext, st.st_size, st.st_mtime,
                                                     time.time(), status, url, len(passages))).lastrowid
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
                "bm25(chunks, 4.0, 1.0) AS score FROM chunks c JOIN files f ON f.id = c.file_id "
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
            out["note"] = "an online Google document: only its name is known"
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

    def count(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM files")[0]["n"]

    def status(self) -> dict:
        counts = {r["status"]: r["n"] for r in self._query("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}
        by_folder = {r["folder"]: r["n"] for r in self._query("SELECT folder, COUNT(*) AS n FROM files GROUP BY folder")}
        return {"files": sum(counts.values()), "by_status": counts, "running": self.running,
                "progress": self.progress, "last": self.last,
                "folders": [{"path": str(f), "files": by_folder.get(str(f), 0)} for f in self.folders()]}

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
