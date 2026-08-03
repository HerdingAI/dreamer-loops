#!/usr/bin/env python3
"""Build the qmd-indexable wisdom corpus (§6.4).

The spec assumed a corpus of "book transcripts" already in text form. On disk
it is actually three heterogeneous piles:

  * 328 .txt audiobook/lecture transcripts   -> copy + header (the bulk)
  * 42 ebooks (epub/mobi/azw3/doc/pdf)        -> ebook-convert / pdftotext
  * 89 academic PDFs                          -> pdftotext -layout

qmd indexes markdown, so everything lands as .md under wisdom_md/, preserving
the source's directory structure because those folder names ("Wisdom Books",
"The Great Courses", "Economics at Google") are real thematic signal that
feeds qmd's context tree.

Source files are READ ONLY. DoD 6.4 asserts the books directory is untouched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG, atomic_write, atomic_write_json, log, read_json  # noqa: E402

TEXT_EXT = {".txt", ".md", ".text"}
EBOOK_EXT = {".epub", ".mobi", ".azw3", ".azw", ".doc", ".docx", ".rtf", ".fb2"}
PDF_EXT = {".pdf"}
SKIP_EXT = {".torrent", ".part", ".jpg", ".png", ".opf", ".db", ".json"}

MIN_CHARS = 400  # below this an extraction is a failure, not a short book
MIN_QUALITY = 0.90  # printable-prose ratio; below this the "text" is mojibake

# Prose-plausible characters. Ciphertext read as UTF-8 scores ~0.33 here, real
# prose (including accented text) scores >0.95.
_PROSE_OK = set(" \n\r\t.,;:'\"()[]{}-—–?!/%$&*+=<>@#0123456789"
                "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ")


def prose_quality(text: str) -> float:
    """Fraction of characters that look like prose.

    Length alone is not a quality gate: a DRM-encrypted EPUB read as bytes
    yields hundreds of KB of high-entropy mojibake that sails past any
    minimum-length check and then silently poisons the search index.
    """
    sample = text[:200_000]
    if not sample:
        return 0.0
    good = sum(1 for c in sample if c in _PROSE_OK or (c.isalpha() and ord(c) < 0x2000))
    return good / len(sample)


def is_drm_protected(src: Path) -> bool:
    """Adobe ADEPT and friends leave META-INF/encryption.xml behind."""
    try:
        with zipfile.ZipFile(src) as zf:
            names = set(zf.namelist())
            if "META-INF/encryption.xml" not in names:
                return False
            try:
                blob = zf.read("META-INF/encryption.xml").decode("utf-8", "replace")
            except (KeyError, OSError):
                return True
            # Font-obfuscation-only encryption still leaves the text readable.
            return "EncryptedData" in blob and "font" not in blob.lower()
    except (zipfile.BadZipFile, OSError):
        return False


def run(cmd: list[str], timeout: int = 300) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stderr[-400:]
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError as exc:
        return 127, str(exc)


def extract(src: Path) -> tuple[str | None, str]:
    """Return (text, method). text is None on failure."""
    ext = src.suffix.lower()
    if ext in TEXT_EXT:
        try:
            return src.read_text(encoding="utf-8", errors="replace"), "copy"
        except OSError as exc:
            return None, f"read-error: {exc}"

    if ext in PDF_EXT:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            out = Path(tf.name)
        try:
            code, err = run(["pdftotext", "-layout", "-q", str(src), str(out)])
            if code == 0 and out.exists():
                text = out.read_text(encoding="utf-8", errors="replace")
                if len(text.strip()) >= MIN_CHARS:
                    return text, "pdftotext"
                # Scanned PDF with no text layer — fall through to calibre.
            code, err = run(["ebook-convert", str(src), str(out)], timeout=600)
            if code == 0 and out.exists():
                text = out.read_text(encoding="utf-8", errors="replace")
                if len(text.strip()) >= MIN_CHARS:
                    return text, "ebook-convert(pdf)"
            return None, f"pdf-extract-failed: {err[:120]}"
        finally:
            out.unlink(missing_ok=True)

    if ext in EBOOK_EXT:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tf:
            out = Path(tf.name)
        try:
            code, err = run(["ebook-convert", str(src), str(out)], timeout=600)
            if code == 0 and out.exists():
                text = out.read_text(encoding="utf-8", errors="replace")
                if len(text.strip()) >= MIN_CHARS:
                    return text, "ebook-convert"
        finally:
            out.unlink(missing_ok=True)
        # Calibre fails on a minority of EPUBs. Two very different causes:
        #   * malformed OPF / odd manifest  -> a stdlib zip read recovers it
        #   * DRM (Adobe ADEPT)             -> nothing legitimate recovers it
        # Distinguish them, because the fallback cannot: it happily returns
        # ciphertext, which is long enough to pass a length check.
        if ext == ".epub":
            if is_drm_protected(src):
                return None, "drm-protected: encrypted EPUB, not extractable"
            text = epub_fallback(src)
            if text and len(text.strip()) >= MIN_CHARS:
                q = prose_quality(text)
                if q >= MIN_QUALITY:
                    return text, "epub-stdlib"
                return None, f"epub-stdlib-garbage: prose quality {q:.2f}"
        return None, f"ebook-convert-failed: {err[:120]}"

    return None, "unsupported"


class _Stripper(HTMLParser):
    """Minimal XHTML -> text. Drops script/style, keeps block boundaries."""

    SKIP = {"script", "style", "head", "title", "meta", "link"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
             "tr", "section", "blockquote"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", raw)).strip()


def epub_fallback(src: Path) -> str | None:
    try:
        with zipfile.ZipFile(src) as zf:
            names = [n for n in zf.namelist()
                     if n.lower().endswith((".xhtml", ".html", ".htm"))]
            if not names:
                return None
            # Spine order is usually lexical for well-formed books; good enough
            # for search indexing, where chunk order matters more than chapter order.
            chunks: list[str] = []
            for name in sorted(names):
                try:
                    raw = zf.read(name).decode("utf-8", errors="replace")
                except (KeyError, OSError):
                    continue
                sp = _Stripper()
                try:
                    sp.feed(raw)
                except Exception:  # noqa: BLE001 — malformed markup is expected here
                    continue
                t = sp.text()
                if t:
                    chunks.append(t)
            return "\n\n".join(chunks) or None
    except (zipfile.BadZipFile, OSError):
        return None


def collection_for(src: Path, roots: list[Path]) -> tuple[str, Path]:
    """(theme, relative path) — theme comes from the containing folder, which
    is genuine curation the owner already did."""
    for root in roots:
        try:
            rel = src.relative_to(root)
        except ValueError:
            continue
        theme = rel.parts[0] if len(rel.parts) > 1 else root.name
        return theme, Path(root.name) / rel
    return "misc", Path(src.name)


def build(roots: list[Path], out_dir: Path, force: bool, workers: int) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "_manifest.json"
    manifest: dict = read_json(manifest_path, default={}) or {}

    sources: list[Path] = []
    for root in roots:
        if not root.exists():
            log(f"WARN missing source root: {root}", job="wisdom")
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() not in SKIP_EXT:
                sources.append(f)

    log(f"{len(sources)} candidate source files across {len(roots)} roots",
        job="wisdom")
    stats = {"converted": 0, "cached": 0, "failed": 0, "skipped": 0,
             "by_method": {}, "failures": [], "themes": {}}

    def work(src: Path) -> tuple[Path, str, str | None, str]:
        theme, rel = collection_for(src, roots)
        dest = out_dir / rel.with_suffix(".md")
        try:
            st = src.stat()
            key = f"{st.st_size}:{int(st.st_mtime)}"
        except OSError:
            key = "0:0"
        prior = manifest.get(str(rel))
        if not force and prior and prior.get("key") == key and dest.exists():
            return dest, theme, None, "cached"
        text, method = extract(src)
        if text is None:
            return dest, theme, None, method
        # Universal quality gate. Applies to every method, not just the EPUB
        # fallback: a scanned PDF with a broken text layer fails the same way.
        # Never let unreadable bytes reach the index — a poisoned corpus is
        # worse than a smaller one, because search surfaces the noise silently.
        q = prose_quality(text)
        if q < MIN_QUALITY:
            dest.unlink(missing_ok=True)
            manifest.pop(str(rel), None)
            return dest, theme, None, f"low-quality-extraction: prose ratio {q:.2f}"
        title = src.stem
        header = (
            f"---\ntype: wisdom\ntitle: \"{title.replace(chr(34), chr(39))}\"\n"
            f"theme: \"{theme}\"\nsource_file: \"{src.name}\"\n"
            f"extraction: {method}\n---\n\n# {title}\n\n"
        )
        atomic_write(dest, header + text)
        manifest[str(rel)] = {"key": key, "method": method, "theme": theme,
                              "chars": len(text)}
        return dest, theme, method, "ok"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, s): s for s in sources}
        for i, fut in enumerate(as_completed(futures), 1):
            src = futures[fut]
            try:
                dest, theme, method, status = fut.result()
            except Exception as exc:  # noqa: BLE001
                stats["failed"] += 1
                stats["failures"].append(f"{src.name}: {exc}")
                continue
            if status == "cached":
                stats["cached"] += 1
            elif status == "ok":
                stats["converted"] += 1
                stats["by_method"][method] = stats["by_method"].get(method, 0) + 1
                stats["themes"][theme] = stats["themes"].get(theme, 0) + 1
            elif status == "unsupported":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1
                stats["failures"].append(f"{src.name}: {status}")
            if i % 50 == 0:
                log(f"{i}/{len(sources)} processed", job="wisdom")

    atomic_write_json(manifest_path, manifest)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=CFG["corpora"]["wisdom_build"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    roots = [Path(r) for r in CFG["corpora"]["wisdom_sources"]]
    # Checksum the source tree so DoD 6.4's "books untouched" is verifiable.
    before = source_fingerprint(roots)
    stats = build(roots, Path(args.out), args.force, args.workers)
    after = source_fingerprint(roots)

    log(f"converted={stats['converted']} cached={stats['cached']} "
        f"failed={stats['failed']} skipped={stats['skipped']}", job="wisdom")
    log(f"methods={stats['by_method']}", job="wisdom")
    log(f"themes={dict(sorted(stats['themes'].items(), key=lambda x: -x[1])[:12])}",
        job="wisdom")
    for f in stats["failures"][:15]:
        log(f"FAIL {f}", job="wisdom")
    if before != after:
        log("ERROR: source corpus was modified — DoD 6.4 violation", job="wisdom")
        return 2
    log("source corpus fingerprint unchanged (DoD 6.4)", job="wisdom")
    print(json.dumps({k: v for k, v in stats.items() if k != "failures"}, indent=2))
    return 0


def source_fingerprint(roots: list[Path]) -> str:
    h = hashlib.sha256()
    for root in roots:
        if not root.exists():
            continue
        for f in sorted(root.rglob("*")):
            if f.is_file():
                try:
                    st = f.stat()
                    h.update(f"{f}:{st.st_size}:{int(st.st_mtime)}\n".encode())
                except OSError:
                    continue
    return h.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
