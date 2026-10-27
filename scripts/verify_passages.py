#!/usr/bin/env python3
"""Source fetching and passage verification for lessons.

Lessons are built only from text that code found. This module is the one place
that opens a connection for a lesson, and the one place that decides whether a
model's quoted passage is really in a source.

Fetcher (security is the point of this file). A URL is fetched only when:
  - the scheme is https, the port is the default (443), there is no userinfo and
    no control character, and the host is a dotted NAME (not an IP literal, not a
    numeric spelling of one);
  - Gate.check_url (egress_check) finds no blocked name, private phrase or email in it;
  - EVERY address the name resolves to is globally routable after unwrapping the
    IPv4 forms IPv6 can carry (mapped, 6to4, Teredo, NAT64). That also refuses
    carrier-grade NAT space (100.64.0.0/10), link-local, loopback and private networks.
The connection goes to the one address that was checked (SNI and certificate
verification still use the name), so a second DNS answer cannot move it. Redirects
are followed by hand and every hop passes all of the above. Bytes, time, DNS time,
redirects and the number of fetches are capped from config, every attempt lands in
the egress record, and any doubt is a refusal.

Verification. A claim's quoted passage counts when at least
`frontier.passage_min_run_tokens` of its normalised tokens appear as ONE contiguous
run in the source. What is kept is the source's own text for that run (original
casing), never the model's wording. Claims are then judged by the rules in the claims contract.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote, urljoin, urlsplit

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dreamer_common import CFG  # noqa: E402


def _cfg(key: str):
    return CFG["frontier"][key]


HTTPS_PORT = 443
MIN_READ_SECONDS = 0.1  # the least a socket read is given, so a nearly spent budget does not mean "no timeout"
_TARGET_SAFE = "/%:@!$&'()*+,;=~-._?"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
ACCEPTED_TYPES = ("text/html", "text/plain", "application/xhtml+xml")
# Second-level labels that sit under a country code (example.co.uk): the registrable
# domain then keeps three labels. A structural rule of the public suffix list, not a tunable.
_CC_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_SITE_LOCAL = ipaddress.ip_network("fec0::/10")  # deprecated, yet is_global says True
_TOKEN = re.compile(r"\w+", re.UNICODE)
# A digit run right after a letter (or letter + hyphen) is part of a name (XJ-9, TiO2, Cone-6), not a quantity.
_NUMBER = re.compile(r"(?<![A-Za-z])(?<![A-Za-z]-)(?<!\d)\d+(?:[.,]\d+)*")
# Punctuation that only closes a clause or quote: a span does not pick it up after its last token.
_CLAUSE_PUNCT = ".,;:!?)]}\"'\u2019\u201d"
# Spelled-out quantities (a language constant, not a tunable). Each maps to its digit form, which
# a verified span may carry instead of the word; "half" has none. "one" is left out on purpose:
# "no one", "one source" and "one of" are not quantities.
_NUMBER_WORDS = {
    "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10", "eleven": "11", "twelve": "12", "thirteen": "13", "fourteen": "14",
    "fifteen": "15", "sixteen": "16", "seventeen": "17", "eighteen": "18", "nineteen": "19",
    "twenty": "20", "thirty": "30", "forty": "40", "fifty": "50", "sixty": "60", "seventy": "70",
    "eighty": "80", "ninety": "90", "hundred": "100", "thousand": "1000", "million": "1000000",
    "billion": "1000000000", "trillion": "1000000000000", "dozen": "12", "half": "",
}
# A name token: letters, digits and hyphens/dashes holding two capitals, or a capital and a digit
# (XJ-9, QRS, B12); ordinary capitalised words and camel-case product words do not qualify.
_NAME_RUN = re.compile("[^\\W_](?:[^\\W_]|[-\u2010-\u2015\u2212])*")
_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"), "-")
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b", re.IGNORECASE)
# Modal action language: a claim that says what to do is load-bearing whatever kind the model gave it.
_MODAL = re.compile(r"\b(?:should|must|ought\s+to|needs?\s+to|have\s+to|has\s+to)\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# Domains and addresses
# --------------------------------------------------------------------------

def registrable_domain(host: str) -> str:
    """The domain a site is owned under: the last two labels, three under a
    country-code second level (co.uk), and the subdomain itself under a shared
    host listed in frontier.shared_host_suffixes (foo.github.io)."""
    labels = host.lower().rstrip(".").split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    tail = ".".join(labels[-2:])
    if tail in {s.lower() for s in (CFG["frontier"].get("shared_host_suffixes") or [])}:
        return ".".join(labels[-3:])
    if len(labels[-1]) == 2 and labels[-2] in _CC_SECOND_LEVEL:
        return ".".join(labels[-3:])
    return tail


def _embedded_v4(ip: ipaddress.IPv6Address) -> Iterator[ipaddress.IPv4Address]:
    if ip.ipv4_mapped:
        yield ip.ipv4_mapped
    if ip.sixtofour:
        yield ip.sixtofour
    if ip.teredo:
        yield from ip.teredo
    if ip in _NAT64:
        yield ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)


def address_ok(address: str) -> tuple[bool, str]:
    """True when `address` is globally routable, also after unwrapping every IPv4
    address it embeds."""
    if "%" in address:
        return False, f"scoped address {address}"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False, f"not an address: {address!r}"
    candidates = [ip]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip in _SITE_LOCAL:
            return False, f"site-local address {address}"
        candidates += list(_embedded_v4(ip))
    for c in candidates:
        if not c.is_global:
            return False, f"non-public address {address}"
    return True, ""


def resolve(name: str, timeout: float) -> list[str]:
    """Every address `name` resolves to. Gives up after `timeout` seconds
    (TimeoutError); an unresolvable name raises OSError."""
    box: dict = {}

    def work() -> None:
        try:
            box["found"] = socket.getaddrinfo(name, HTTPS_PORT, type=socket.SOCK_STREAM)
        except OSError as exc:
            box["error"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"dns lookup for {name} took longer than {timeout:g}s")
    if "error" in box:
        raise box["error"]
    return list(dict.fromkeys(info[4][0] for info in box["found"]))


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ONE already-checked address. `host` stays the name, so the Host
    header, SNI and certificate verification use it; no lookup happens here."""

    def __init__(self, host: str, ip: str, port: int, timeout: float, context=None):
        super().__init__(host, port=port, timeout=timeout,
                         context=context or ssl.create_default_context())
        self._pinned_ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


# --------------------------------------------------------------------------
# Readable text
# --------------------------------------------------------------------------

class _Text(HTMLParser):
    SKIP = {"script", "style", "nav", "noscript", "svg", "template", "footer", "aside", "form", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self._skip = 0
        self._foreign = 0  # svg/math nesting; `_skip` is also raised by head, so it can't tell
        self._title_done = False
        self._title_mode = ""  # "take" feeds title_parts, "drop" swallows an ignored title

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            # Only the first title outside svg/math is the page title; icon titles
            # ("Lock") and any later title are dropped, never read as body text.
            if self._foreign or self._title_done:
                self._title_mode = "drop"
            else:
                self._title_mode = "take"
                self._title_done = True
        else:
            if tag in ("svg", "math"):
                self._foreign += 1
            if tag in self.SKIP:
                self._skip += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self._title_mode = ""
        else:
            if tag in ("svg", "math") and self._foreign:
                self._foreign -= 1
            if tag in self.SKIP and self._skip:
                self._skip -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if self._title_mode == "take":
            self.title_parts.append(data)
        elif self._title_mode == "drop":
            return
        elif not self._skip:
            self.parts.append(data)


def _squash(s: str) -> str:
    return " ".join(s.split())


def extract_text(html: str) -> tuple[str, str]:
    """(title, readable text) of a page: scripts, styles, navigation, forms and
    page chrome dropped, whitespace normalised."""
    parser = _Text()
    parser.feed(html)
    parser.close()
    return _squash("".join(parser.title_parts)), _squash("".join(parser.parts))


# --------------------------------------------------------------------------
# The fetcher
# --------------------------------------------------------------------------

@dataclass
class FetchResult:
    ok: bool
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    reason: str = ""


def _default_connect(ip: str, host: str, port: int, timeout: float):
    return PinnedHTTPSConnection(host, ip, port, timeout)


class Fetcher:
    """One lesson's fetches. `record` collects an entry for every attempt, allowed
    or refused; `resolver(name, timeout)` and `connect(ip, host, port, timeout)` are
    injectable so tests never touch the network."""

    def __init__(self, *, resolver: Callable | None = None, connect: Callable | None = None,
                 gate=None, private_texts: list[str] | None = None,
                 clock: Callable[[], float] | None = None, record: list | None = None,
                 on_record: Callable[[dict], None] | None = None):
        self.resolver = resolver or resolve
        self.connect = connect or _default_connect
        self.gate = gate
        self.private_texts = private_texts or []
        self.clock = clock or time.monotonic
        self.record = record if record is not None else []
        self.on_record = on_record
        self.count = 0
        self.timeout = float(_cfg("fetch_timeout_seconds"))
        self.max_bytes = int(_cfg("fetch_max_bytes"))
        self.max_redirects = int(_cfg("fetch_max_redirects"))
        self.max_fetches = int(_cfg("max_fetches_per_lesson"))
        self.chunk = int(_cfg("fetch_chunk_bytes"))
        self.dns_timeout = float(_cfg("egress")["dns_timeout_seconds"])
        self.user_agent = str(_cfg("fetch_user_agent"))

    # -- bookkeeping ---------------------------------------------------------
    def _note(self, url: str, decision: str, reason: str = "", **extra) -> None:
        entry = {"kind": "fetch", "url": url, "decision": decision, "reason": reason, **extra}
        self.record.append(entry)
        if self.on_record:
            self.on_record(entry)

    def _refuse(self, url: str, reason: str, orig: str, **extra) -> FetchResult:
        self._note(url, "refuse", reason, **extra)
        return FetchResult(False, orig, reason=reason)

    # -- the checks that need no network ------------------------------------
    def _parse(self, url: str) -> tuple[str, str] | str:
        """(ascii host, request target) for an acceptable URL, else the reason."""
        if any(ord(c) <= 0x20 or ord(c) == 0x7F for c in url):
            return "control or space character in the url"
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return "malformed url"
        if parts.scheme.lower() != "https":
            return "only https is fetched"
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return "userinfo in the url"
        if port not in (None, HTTPS_PORT):
            return f"non-default port {port}"
        host = (parts.hostname or "").rstrip(".")
        if not host:
            return "no host"
        try:
            ipaddress.ip_address(host)
            return "ip literal host"
        except ValueError:
            pass
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return "host is not a valid name"
        labels = host.split(".")
        # "2130706433", "0x7f.1" and the like are numeric spellings of an address.
        if len(labels) < 2 or labels[-1].isdigit() or labels[-1].startswith("0x"):
            return "host is not a public dotted name"
        # http.client writes the request line as ASCII: percent-encode anything else,
        # keeping existing %XX escapes (and the reserved characters) as they are
        target = quote(parts.path or "/", safe=_TARGET_SAFE) + (
            "?" + quote(parts.query, safe=_TARGET_SAFE) if parts.query else "")
        return host, target

    def _check(self, url: str) -> tuple[str, str, str] | str:
        """(host, target, pinned address) when every check passes, else the reason."""
        parsed = self._parse(url)
        if isinstance(parsed, str):
            return parsed
        host, target = parsed
        if self.gate is not None:
            res = self.gate.check_url(url, self.private_texts)
            if not res.ok:
                return "url check refused it: " + "; ".join(res.reasons)
        try:
            addresses = self.resolver(host, self.dns_timeout)
        except TimeoutError:
            return "dns lookup timeout"
        except OSError as exc:
            return f"dns lookup failed: {exc}"
        if not addresses:
            return "name did not resolve"
        for a in addresses:
            ok, why = address_ok(a)
            if not ok:
                return f"name resolves to a non-public address ({why})"
        return host, target, addresses[0]

    # -- fetch ----------------------------------------------------------------
    def fetch(self, url: str) -> FetchResult:
        if self.count >= self.max_fetches:
            return self._refuse(url, f"fetch cap reached ({self.max_fetches})", url)
        self.count += 1
        started = self.clock()
        current = url
        for _ in range(self.max_redirects + 1):
            checked = self._check(current)
            if isinstance(checked, str):
                return self._refuse(current, checked, url)
            host, target, ip = checked
            if self.clock() - started > self.timeout:
                return self._refuse(current, "fetch time cap", url, ip=ip)
            conn = self.connect(ip, host, HTTPS_PORT, self.timeout)
            try:
                conn.request("GET", target, headers={
                    "User-Agent": self.user_agent, "Accept": "text/html,text/plain;q=0.9",
                    "Accept-Encoding": "identity", "Connection": "close"})
                resp = conn.getresponse()
                status = resp.status
                if status in REDIRECT_STATUSES:
                    loc = resp.getheader("Location")
                    if not loc:
                        return self._refuse(current, f"redirect {status} without a location", url, ip=ip, status=status)
                    self._note(current, "allow", f"redirect {status}", ip=ip, status=status)
                    current = urljoin(current, loc.strip())
                    continue
                if status != 200:
                    return self._refuse(current, f"http status {status}", url, ip=ip, status=status)
                ctype = (resp.getheader("Content-Type") or "text/html").split(";")[0].strip().lower()
                if ctype not in ACCEPTED_TYPES:
                    return self._refuse(current, f"unsupported content type {ctype}", url, ip=ip, status=status)
                enc = (resp.getheader("Content-Encoding") or "identity").strip().lower()
                if enc != "identity":
                    return self._refuse(current, f"unsupported content encoding {enc}", url, ip=ip, status=status)
                declared = resp.getheader("Content-Length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    return self._refuse(current, f"body exceeds {self.max_bytes} bytes", url, ip=ip, status=status)
                chunks, total = [], 0
                # read1 returns what has arrived instead of waiting for a full chunk, and
                # each read gets only the time left, so a server trickling bytes cannot
                # hold the run past the cap
                read = getattr(resp, "read1", resp.read)
                while True:
                    left = self.timeout - (self.clock() - started)
                    if left <= 0:
                        return self._refuse(current, "fetch time cap", url, ip=ip, status=status)
                    if getattr(conn, "sock", None) is not None:
                        conn.sock.settimeout(max(left, MIN_READ_SECONDS))
                    try:
                        data = read(self.chunk)
                    except TimeoutError:
                        return self._refuse(current, "fetch time cap", url, ip=ip, status=status)
                    if not data:
                        break
                    total += len(data)
                    if total > self.max_bytes:
                        return self._refuse(current, f"body exceeds {self.max_bytes} bytes", url, ip=ip, status=status)
                    chunks.append(data)
                body = b"".join(chunks)
                charset = re.search(r"charset=([\w.-]+)", resp.getheader("Content-Type") or "", re.I)
                try:
                    html = body.decode(charset.group(1) if charset else "utf-8", errors="replace")
                except LookupError:
                    html = body.decode("utf-8", errors="replace")
                if ctype == "text/plain":
                    title, text = "", _squash(html)
                else:
                    title, text = extract_text(html)
                self._note(current, "allow", "", ip=ip, status=status, bytes=total)
                return FetchResult(True, url, final_url=current, title=title, text=text)
            except (OSError, http.client.HTTPException, ssl.SSLError, ValueError) as exc:
                # ValueError covers UnicodeError (a bogus charset) and a malformed Location
                return self._refuse(current, f"connection error: {exc}", url, ip=ip)
            finally:
                conn.close()
        return self._refuse(current, f"too many redirects (limit {self.max_redirects})", url)


# --------------------------------------------------------------------------
# Passage verification
# --------------------------------------------------------------------------

def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(m.group(0).casefold(), m.start(), m.end()) for m in _TOKEN.finditer(text)]


def find_span(passage: str, source: str, min_run: int, max_chars: int) -> str | None:
    """The source's own text for the longest contiguous run of the passage's
    normalised tokens, when that run has at least `min_run` tokens; else None.
    The span runs to the end of the source word holding its last token (so "14%"
    stays whole, unless all that follows is clause punctuation), then is cut to `max_chars`."""
    p = [t for t, _, _ in _tokens(passage)]
    s = _tokens(source)
    if not p or not s or min_run < 1:
        return None
    where: dict[str, list[int]] = {}
    for j, (tok, _, _) in enumerate(s):
        where.setdefault(tok, []).append(j)
    best_len, best_at = 0, 0
    for i, tok in enumerate(p):
        for j in where.get(tok, ()):
            if i and j and p[i - 1] == s[j - 1][0]:
                continue  # inside a run that started earlier
            k = 0
            while i + k < len(p) and j + k < len(s) and p[i + k] == s[j + k][0]:
                k += 1
            if k > best_len:
                best_len, best_at = k, j
    if best_len < min_run:
        return None
    end = word_end = s[best_at + best_len - 1][2]
    while word_end < len(source) and not source[word_end].isspace():
        word_end += 1
    if source[end:word_end].strip(_CLAUSE_PUNCT):  # "14%" or "14%." stays whole; a bare "," or "." does not
        end = word_end
    return source[s[best_at][1]:end][:max_chars].rstrip()


def find_span_in_source(passage: str, src: "Source", min_run: int, max_chars: int) -> str | None:
    """find_span over a source. An owner source cut to head and tail (owner_text.CUT_MARKER)
    is two texts: a span never bridges the cut, which would present the omitted middle as
    one continuous run of the owner's words."""
    if src.kind != "owner":
        return find_span(passage, src.text, min_run, max_chars)
    from owner_text import CUT_MARKER  # lazy: owner_text pulls in questions
    spans = [find_span(passage, piece, min_run, max_chars) for piece in src.text.split(CUT_MARKER)]
    return max((sp for sp in spans if sp), key=len, default=None)


def flag_instructions(text: str, patterns: list[str]) -> list[str]:
    """The fragments of `text` that look like instructions to a model."""
    out = []
    for pat in patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            out.append(m.group(0))
    return out


def number_tokens(text: str) -> set[str]:
    return {m.group(0).replace(",", "") for m in _NUMBER.finditer(text)}


def number_words(text: str) -> list[str]:
    """The spelled-out quantities in `text`, lower-cased, in order of appearance."""
    return [m.group(1).lower() for m in _NUMBER_WORD.finditer(text)]


def _unsupported_number_word(claim_text: str, span_text: str) -> str | None:
    """The first number word of the claim that the span carries neither as the word nor as its digits."""
    have_words, have_digits = set(number_words(span_text)), number_tokens(span_text)
    for w in number_words(claim_text):
        if w not in have_words and not (_NUMBER_WORDS[w] and _NUMBER_WORDS[w] in have_digits):
            return w
    return None


def _uncorroborated_number(text: str, spans: list[dict], by_id: dict[str, "Source"]) -> tuple[str, list[str]] | None:
    """The first figure of the claim that no library or owner span holds and that fewer than two
    registrable web domains hold, with the web domains that do hold it; None when each is corroborated.
    Each figure is counted on its own: two sites that hold different figures corroborate neither."""
    held = [(by_id[sp["source"]], number_tokens(sp["span"]), set(number_words(sp["span"]))) for sp in spans]
    wanted = [(n, None) for n in sorted(number_tokens(text))]
    wanted += [(w, _NUMBER_WORDS[w]) for w in dict.fromkeys(number_words(text))]
    for tok, digits in wanted:
        # a spelled-out figure also counts as held by its digits ("half" has none), as _unsupported_number_word does
        has = [s for s, nums, words in held if (tok in nums if digits is None
                                                 else tok in words or bool(digits and digits in nums))]
        if any(s.kind in ("wisdom", "owner") for s in has):
            continue
        domains = {s.domain for s in has if s.kind == "web"}
        if len(domains) < 2:
            return tok, sorted(domains)
    return None


def _dash_fold(text: str) -> str:
    return text.translate(_DASHES).casefold()


def name_tokens(text: str) -> set[str]:
    """The names and abbreviations in `text` (see _NAME_RUN), as written."""
    out = set()
    for m in _NAME_RUN.finditer(text):
        tok = m.group(0).translate(_DASHES).rstrip("-")
        upper = sum(ch.isupper() for ch in tok)
        if upper >= 2 or (upper and any(ch.isdigit() for ch in tok)):
            out.add(tok)
    return out


@dataclass
class Source:
    id: str
    kind: str            # web | wisdom | owner
    title: str
    text: str
    url: str = ""
    ref: str = ""
    anchor: str = ""     # owner sources: the evidence quote the turn was found by

    @property
    def domain(self) -> str:
        return registrable_domain(urlsplit(self.url).hostname or "") if self.url else ""


@dataclass
class Verified:
    claims: list[dict] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    flagged: list[dict] = field(default_factory=list)


def verify_claims(claims: list[dict], sources: list[Source], *, min_run: int | None = None,
                  max_chars: int | None = None, patterns: list[str] | None = None) -> Verified:
    """Check each claim {text, kind, sources: [{source, passage}]} against the
    sources. A verified claim is {id, text, kind, single_source,
    spans: [{source, span}]}; the spans are source text. Every other claim is in
    `dropped` with its reason, and instruction-like spans are in `flagged`."""
    min_run = int(_cfg("passage_min_run_tokens")) if min_run is None else min_run
    max_chars = int(_cfg("passage_max_chars")) if max_chars is None else max_chars
    patterns = list(_cfg("instruction_patterns")) if patterns is None else patterns
    by_id = {s.id: s for s in sources}
    out = Verified()
    for c in claims:
        text = c["text"]
        if not c["sources"]:
            out.dropped.append({"text": text, "reason": "no source cited"})
            continue
        spans: list[dict] = []
        for ref in c["sources"]:
            src = by_id.get(ref["source"])
            if src is None or any(sp["source"] == src.id for sp in spans):
                continue
            span = find_span_in_source(ref["passage"], src, min_run, max_chars)
            if span is None:
                continue
            hits = flag_instructions(span, patterns)
            if hits:
                out.flagged.append({"source": src.id, "fragment": hits[0], "span": span})
                continue
            spans.append({"source": src.id, "span": span})
        if not spans:
            out.dropped.append({"text": text, "reason": "no cited passage verified in its source"})
            continue
        unsupported = number_tokens(text) - number_tokens(" ".join(sp["span"] for sp in spans))
        if unsupported:
            out.dropped.append({"text": text, "reason": f"number {sorted(unsupported)[0]} is not in the verified span"})
            continue
        missing_word = _unsupported_number_word(text, " ".join(sp["span"] for sp in spans))
        if missing_word:
            out.dropped.append({"text": text, "reason": f"number {missing_word} is not in the verified span"})
            continue
        folded = _dash_fold(" ".join(sp["span"] for sp in spans))
        missing_name = next((n for n in sorted(name_tokens(text)) if _dash_fold(n) not in folded), None)
        if missing_name:
            out.dropped.append({"text": text, "reason": f"name {missing_name} is not in the verified span"})
            continue
        used = [by_id[sp["source"]] for sp in spans]
        independent = {s.id if s.kind != "web" else s.domain or s.id for s in used}
        reason = ("a number, dose or action step needs two registrable domains or one library or "
                  "owner source")
        figure = _uncorroborated_number(text, spans, by_id)
        if figure:
            out.dropped.append({"text": text, "reason": reason, "number": figure[0], "domains": figure[1]})
            continue
        if (c["kind"] == "action" or _MODAL.search(text)) and not (number_tokens(text) or number_words(text)):
            # no figure to count: the claim as a whole needs a library or owner span, or two web domains
            if not (any(s.kind in ("wisdom", "owner") for s in used)
                    or len({s.domain for s in used if s.kind == "web"}) >= 2):
                # name the domains it did verify on, so a reader can tell one blog from two papers on one host
                out.dropped.append({"text": text, "reason": reason,
                                    "domains": sorted({s.domain for s in used if s.kind == "web"})})
                continue
        out.claims.append({"id": f"c{len(out.claims) + 1}", "text": text, "kind": c["kind"],
                           "single_source": len(independent) == 1, "spans": spans})
    return out
