#!/usr/bin/env python3
"""Deterministic daily sync for the Claude Blog Condenser.

Fetch, strip, diff, validate, write, and commit are deterministic operations and
live here, in code, where the limits are enforced rather than advised. The only
step that needs a model is writing a TL;DR, and that is one API call per new
post with a validated result.

Every limit below is a hard stop that ends the run cleanly. Nothing in this
script retries beyond the stated attempt counts, invents an alternative write
path, or splits a payload into parts. If the whole file cannot be written, the
previous file is left untouched.

Usage:  python3 sync.py            # from the repo root

Test hooks (not used in normal operation):
  SYNC_INDEX_URL=<url>   override config source.index_url
  --no-commit            do everything except git add/commit/push
  --no-push              commit but do not push
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser

# ── Hard limits ───────────────────────────────────────────────────────────────
WALL_CLOCK_BUDGET_S = 8 * 60   # total run budget, checked between every stage
MAX_ATTEMPTS = 2               # per operation; a third attempt aborts the run
HTTP_TIMEOUT_S = 45

# Summarization. Changing SUMMARY_MODEL is the whole experiment: the runner does
# not care which model this is, and no other stage calls the API.
SUMMARY_MODEL = "claude-haiku-4-5"
SUMMARY_MAX_TOKENS = 300
# Bounds the body text sent per summary, and with it the worst case for a whole
# run. Measured over the stored set: median post 6.6k chars, p75 10.3k, p90 21k,
# longest 69k. At 16k the full 6-post cap is ~24k input tokens, which keeps a run
# inside the ~30k/run budget and under a 20k/min workspace ITPM ceiling; ~87% of
# posts still go to the model whole. Truncation only ever drops the tail of a very
# long post, and a summary leads with what the post announces, which is at the
# front. Raise this if summaries of long posts start missing the point.
SUMMARY_INPUT_CHAR_CAP = 16000
TLDR_MAX_CHARS = 600            # posts.schema.json tldr maxLength

# Prompt caching. The only repeated content in this workload is a retry: attempt
# 2 resends the identical system + rules + body and appends the validator's
# reason. A cache breakpoint after the body lets that second call read the whole
# prefix instead of paying for it again.
#
# Measured 2026-09-07 and left OFF, because it cannot pay here:
#
# 1. Haiku 4.5 has a 4096-token minimum cacheable prefix. The longest stored post
#    truncated to SUMMARY_INPUT_CHAR_CAP measured 3707 prompt tokens - under the
#    threshold, so the marker silently no-ops (cache_creation_input_tokens: 0,
#    confirmed against the live API). Median posts are far smaller again.
# 2. Clearing 4096 means sending MORE body text to enable the cache, which costs
#    more input on every call than the cache returns on a minority of retries.
# 3. A write costs 1.25x and a read 0.1x, so with retry probability p the expected
#    input cost is 1.25 + 0.1p cached against 1.0 + p uncached: caching only wins
#    when p > ~0.28, and the calibrated prompt now lands attempt 1 most of the time.
# 4. Nothing else is cacheable. The shared system + rules prefix is ~300 tokens,
#    an order of magnitude under the threshold, and padding it to qualify costs
#    more than caching saves.
#
# Flip to True if the model, its threshold, or the retry rate changes; the run
# log reports the retry rate against the break-even every run.
ENABLE_PROMPT_CACHE = False
CACHE_BREAKEVEN_RETRY_RATE = 0.28
# The prompt is given stricter limits than the validator enforces. Models drift
# long on long posts, and a summary aimed at the exact ceiling lands just over it
# about half the time; the headroom absorbs that drift without loosening the
# contract, which stays at config's word bounds and the schema's 600 characters.
TLDR_PROMPT_HEADROOM_WORDS = 8
TLDR_PROMPT_MAX_CHARS = 480
ANTHROPIC_VERSION = "2023-06-01"
API_URL = "https://api.anthropic.com/v1/messages"

USER_AGENT = "claude-blog-condenser/2.0 (+https://github.com/sw00t/Claude-Blog-Condenser)"

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# Chrome strings that must never survive extraction (runbook step 4 self-check).
# Matched as whole standalone lines: the metadata block renders each of these on
# a line of its own, while prose only ever mentions them mid-sentence ("Category
# Management", "Share the results"). Substring matching here discards good posts.
CHROME_MARKERS = ["Reading time", "Copy link", "Share", "Category",
                  "Related posts", "Subscribe", "min", "Author(s)"]


class _Usage:
    """Per-run API token totals, so cost is observable rather than assumed."""

    def __init__(self):
        self.calls = self.retries = 0
        self.uncached_in = self.cache_write = self.cache_read = self.out = 0

    def add(self, usage, retry=False):
        self.calls += 1
        self.retries += 1 if retry else 0
        self.uncached_in += usage.get("input_tokens", 0)
        self.cache_write += usage.get("cache_creation_input_tokens", 0)
        self.cache_read += usage.get("cache_read_input_tokens", 0)
        self.out += usage.get("output_tokens", 0)

    def summary(self):
        if not self.calls:
            return "no API calls"
        # Haiku 4.5: $1/MTok in, $5/MTok out; writes bill 1.25x, reads 0.1x.
        cost = ((self.uncached_in + 1.25 * self.cache_write
                 + 0.10 * self.cache_read) / 1e6) + (self.out * 5 / 1e6)
        return ("%d call(s), %d retry(ies) | in %d uncached / %d cache-write / "
                "%d cache-read | out %d | ~$%.4f"
                % (self.calls, self.retries, self.uncached_in, self.cache_write,
                   self.cache_read, self.out, cost))


RUN_USAGE = _Usage()


class AbortRun(Exception):
    """Run cannot continue. Commit nothing, leave posts.json untouched."""


class PushAuthError(Exception):
    """Push authentication failed. Hard stop, reported plainly."""


class SkipPost(Exception):
    """This one post cannot be processed. Record it in `skipped` and go on."""


def log(msg):
    print("[sync] %s" % msg, flush=True)


def utcnow():
    return datetime.now(timezone.utc)


def rfc3339(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Budget ────────────────────────────────────────────────────────────────────
class Budget:
    """Wall clock and fetch ceilings. Both are measured, not estimated."""

    def __init__(self, seconds, max_fetches):
        self.start = time.monotonic()
        self.seconds = seconds
        self.max_fetches = max_fetches
        self.fetches = 0
        self.expired = False

    def elapsed(self):
        return time.monotonic() - self.start

    def remaining(self):
        return self.seconds - self.elapsed()

    def check(self, stage):
        """Return True while there is budget left. Latches `expired` once spent."""
        if self.remaining() <= 0:
            if not self.expired:
                self.expired = True
                log("wall-clock budget spent at stage '%s' (%.1fs) - finalising"
                    % (stage, self.elapsed()))
            return False
        return True

    def take_fetch(self):
        if self.fetches >= self.max_fetches:
            return False
        self.fetches += 1
        return True


def with_attempts(label, fn):
    """Run fn with at most MAX_ATTEMPTS tries. A third attempt aborts the run.

    Varying the request would be the same approach, not a new one, so there is
    no variation here: the same call is made, at most twice.
    """
    last = None
    for i in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - deliberately broad, then abort
            last = exc
            log("%s: attempt %d/%d failed: %s" % (label, i, MAX_ATTEMPTS, exc))
    raise AbortRun("%s failed after %d attempts: %s" % (label, MAX_ATTEMPTS, last))


# ── HTTP ──────────────────────────────────────────────────────────────────────
def http_get(url, budget, label):
    if not budget.take_fetch():
        raise AbortRun("HTTP fetch cap of %d reached before fetching %s"
                       % (budget.max_fetches, url))

    def once():
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            if resp.status != 200:
                raise IOError("HTTP %s for %s" % (resp.status, url))
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")

    return with_attempts(label, once)


# ── HTML helpers ──────────────────────────────────────────────────────────────
BLOCK_TAGS = {"p", "div", "section", "article", "h1", "h2", "h3", "h4", "h5",
              "h6", "li", "ul", "ol", "br", "tr", "table", "blockquote", "pre",
              "figure", "figcaption", "hr"}
DROP_TAGS = {"script", "style", "svg", "noscript", "head", "nav", "footer",
             "form", "button"}
VOID_TAGS = {"br", "img", "hr", "meta", "link", "input", "source", "area",
             "base", "col", "embed", "param", "track", "wbr"}


class _Slice(HTMLParser):
    """Capture the outer HTML of the first element matching tag + class predicate."""

    def __init__(self, tag, pred):
        HTMLParser.__init__(self, convert_charrefs=False)
        self.tag, self.pred = tag, pred
        self.depth = 0
        self.on = False
        self.buf = []
        self.out = None

    def handle_starttag(self, tag, attrs):
        if self.out is not None:
            return
        if not self.on and tag == self.tag and self.pred(dict(attrs).get("class") or ""):
            self.on = True
            self.depth = 0
        if self.on:
            self.buf.append(self.get_starttag_text() or "")
            if tag not in VOID_TAGS:
                self.depth += 1

    def handle_startendtag(self, tag, attrs):
        if self.on and self.out is None:
            self.buf.append(self.get_starttag_text() or "")

    def handle_endtag(self, tag):
        if self.on and self.out is None:
            if tag in VOID_TAGS:
                return
            self.buf.append("</%s>" % tag)
            self.depth -= 1
            if self.depth <= 0:
                self.out = "".join(self.buf)
                self.on = False

    def handle_data(self, data):
        if self.on and self.out is None:
            self.buf.append(data)

    def handle_entityref(self, name):
        if self.on and self.out is None:
            self.buf.append("&%s;" % name)

    def handle_charref(self, name):
        if self.on and self.out is None:
            self.buf.append("&#%s;" % name)


def slice_element(doc, tag, pred):
    parser = _Slice(tag, pred)
    parser.feed(doc)
    return parser.out


class _BodyText(HTMLParser):
    """Rich-text container -> plain text, with [[figure:N]] markers.

    `[[figure:N]]` markers are the only markup emitted into body_text. Each
    marker sits at the image's exact position in document order and has a
    matching entry in `figures`.
    """

    def __init__(self):
        HTMLParser.__init__(self, convert_charrefs=True)
        self.parts = []
        self.skip = 0
        self.figures = []
        self.n = 0
        self.fig_depth = 0
        self.caption = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag in DROP_TAGS:
            self.skip += 1
            return
        if self.skip:
            return
        if tag == "figure":
            self.fig_depth = 1
        elif self.fig_depth:
            self.fig_depth += 1
        # Only images inside a <figure> in the article body count. Hero art,
        # avatars, logos and tracking pixels live outside this container.
        if tag == "img" and self.fig_depth:
            src = (attr.get("src") or "").strip()
            if src.startswith("//"):
                src = "https:" + src
            if src and not src.startswith("data:"):
                self.n += 1
                fig = {"n": self.n, "src": src}
                alt = (attr.get("alt") or "").strip()
                if alt:
                    fig["alt"] = alt
                self.figures.append(fig)
                self.parts.append("\n[[figure:%d]]\n" % self.n)
        if tag == "figcaption":
            self.caption = []
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in DROP_TAGS:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "figcaption" and self.caption is not None:
            text = " ".join("".join(self.caption).split())
            if text and self.figures:
                self.figures[-1]["caption"] = text
            self.caption = None
        if self.fig_depth:
            self.fig_depth -= 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.caption is not None:
            self.caption.append(data)
        else:
            self.parts.append(data)


def html_to_text(fragment):
    parser = _BodyText()
    parser.feed(fragment)
    lines = [" ".join(l.split()) for l in "".join(parser.parts).split("\n")]
    out, blank = [], False
    for line in lines:
        if line:
            out.append(line)
            blank = False
        elif not blank and out:
            out.append("")
            blank = True
    while out and not out[-1]:
        out.pop()
    return "\n".join(out), parser.figures


def strip_tags(fragment):
    fragment = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", fragment,
                      flags=re.S | re.I)
    parser = _BodyText()
    parser.feed(fragment)
    return [" ".join(l.split()) for l in "".join(parser.parts).split("\n")]


# ── Discovery: first page only ────────────────────────────────────────────────
# Page 1 renders posts in two lists: the main grid (carries the category label
# printed on the card) and the hero marquee above it (title and date only, and
# it repeats each item for the scrolling animation, hence the dedupe by slug).
GRID_CARD_RE = re.compile(
    r'<div role="listitem" class="blog_cms_item w-dyn-item">'
    r'(.*?)(?=<div role="listitem" class="blog_cms_item w-dyn-item">|\Z)', re.S)
MARQUEE_CARD_RE = re.compile(
    r'<div role="listitem" class="marquee_cms_blog_list_item w-dyn-item">'
    r'(.*?)(?=<div role="listitem" class="marquee_cms_blog_list_item w-dyn-item">|\Z)',
    re.S)
ITEM_LINK_RE = re.compile(r'fs-list-element="item-link"[^>]*href="(/blog/[^"#?]+)"')
FIELD_RE = r'fs-list-field="%s"[^>]*>(.*?)</div>'
TITLE_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.S)
DATE_RE = re.compile(r'u-foreground-tertiary">([^<]{4,40})<')
HREF_RE = re.compile(r'href="(/blog/[^"#?]+)"')


def parse_long_date(text):
    """'August 26, 2026' -> '2026-08-26'. Never guesses; returns None if unsure."""
    m = re.match(r"\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\s*$", text or "")
    if not m or m.group(1) not in MONTHS:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2))).isoformat()
    except ValueError:
        return None


def slugify(path):
    slug = path.rstrip("/").rsplit("/", 1)[-1].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    return slug


def discover(index_html, index_url):
    """Parse page 1 of the index. No pagination, no feeds, no sitemaps, no archives.

    Seeing far fewer posts than window_days would suggest is expected: posts that
    scroll off page 1 are carried forward from storage, not re-crawled.
    """
    base = re.match(r"(https?://[^/]+)", index_url)
    origin = base.group(1) if base else "https://claude.com"
    found, seen = [], set()

    def add(slug, path, title, card_date, category):
        if not slug or not title or slug in seen:
            return
        seen.add(slug)
        found.append({
            "id": slug,
            "url": origin + path,
            "title": title,
            "index_date": card_date,
            "category": category,
        })

    def field(card, name):
        m = re.search(FIELD_RE % name, card, re.S)
        return " ".join(strip_tags(m.group(1))).strip() if m else ""

    # Main grid first: it is the list that prints a category on each card.
    for card in GRID_CARD_RE.findall(index_html):
        link = ITEM_LINK_RE.search(card) or HREF_RE.search(card)
        if not link:
            continue
        path = link.group(1)
        add(slugify(path), path, field(card, "heading"),
            parse_long_date(field(card, "date")), field(card, "category") or None)

    # Then the hero marquee, for anything the grid did not already cover.
    for card in MARQUEE_CARD_RE.findall(index_html):
        link = HREF_RE.search(card)
        title = TITLE_RE.search(card)
        if not link or not title:
            continue
        card_date = None
        for cand in DATE_RE.findall(card):
            card_date = parse_long_date(cand)
            if card_date:
                break
        path = link.group(1)
        add(slugify(path), path,
            " ".join(strip_tags(title.group(1))).strip(), card_date, None)

    return found


# ── Post extraction ───────────────────────────────────────────────────────────
def extract_details(doc):
    """Read the post's metadata block: Category, Product, Date, Author(s)."""
    details = {}
    rest = doc
    while True:
        frag = slice_element(rest, "div",
                             lambda c: "hero_blog_post_details_content" in c)
        if not frag:
            break
        idx = rest.find(frag)
        rest = rest[idx + len(frag):] if idx >= 0 else ""
        lines = [l for l in strip_tags(frag) if l]
        if len(lines) >= 2:
            details.setdefault(lines[0], []).extend(lines[1:])
        if not rest:
            break
    return details


def extract_post(doc, url, index_title, index_date, index_category=None):
    """Return a post record, or raise SkipPost. Never invents a field."""
    frag = slice_element(
        doc, "div",
        lambda c: "u-rich-text-blog" in c and "w-richtext" in c
        and "w-condition-invisible" not in c)
    if not frag:
        raise SkipPost("article body container not found")

    body, figures = html_to_text(frag)
    if not body.strip():
        raise SkipPost("article body extracted empty")

    details = extract_details(doc)

    title = index_title or ""
    if not title:
        m = re.search(r"<title[^>]*>(.*?)</title>", doc, re.S | re.I)
        if m:
            title = " ".join(strip_tags(m.group(1))).split("|")[0].strip()
    if not title:
        raise SkipPost("no title found")

    published = None
    for label in ("Date", "Published", "Published on"):
        for value in details.get(label, []):
            published = parse_long_date(value)
            if published:
                break
        if published:
            break
    if not published:
        published = index_date
    if not published:
        # Never guess a date. Skip this post only.
        raise SkipPost("no publication date on the page")

    # body_text begins at the first sentence of prose and ends at the last
    # sentence of the article. The container excludes the metadata block and the
    # trailing "Related posts" / subscribe chrome; this is the self-check.
    body_lines = {l.strip() for l in body.split("\n")}
    for marker in CHROME_MARKERS:
        if marker in body_lines:
            raise SkipPost("page chrome %r survived extraction" % marker)
    if body.lstrip().lower().startswith(title.strip().lower()) and title.strip():
        raise SkipPost("body_text begins by repeating the title")

    record = {
        "id": slugify(url),
        "url": url,
        "title": title,
        "published_at": published,
        "body_text": body,
        "word_count": len(body.split()),
        "fetched_at": rfc3339(utcnow()),
        "content_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
    }

    authors = [a for a in details.get("Author(s)", []) if a.strip()]
    if authors:
        record["authors"] = authors
    tags = [t for t in details.get("Product", []) if t.strip()]
    if tags:
        record["tags"] = tags
    # Category as printed on the index card, verbatim; the post page's own
    # "Category" label is the fallback. Never inferred from title or body.
    category = index_category or next(
        (c for c in details.get("Category", []) if c.strip()), None)
    if category:
        record["category"] = category.strip()[:60]
    if figures:
        record["figures"] = figures

    check_figure_markers(record)
    return record


def check_figure_markers(post):
    """Markers and figures entries must correspond one-to-one."""
    markers = sorted(int(n) for n in re.findall(r"\[\[figure:(\d+)\]\]",
                                                post.get("body_text", "")))
    entries = sorted(f["n"] for f in post.get("figures", []))
    if markers != entries:
        raise SkipPost("figure markers %s do not match figures %s"
                       % (markers, entries))


# ── Summarization: the one step that needs a model ────────────────────────────
SUMMARY_SYSTEM = (
    "You write short, tight one-paragraph summaries of blog posts. You are given "
    "the full body text of a single post. Reply with the summary only: no "
    "preamble, no quotes, no bullets, no headings, no markdown. Brevity is a hard "
    "requirement, not a preference - a summary that runs over the stated word "
    "limit is rejected outright."
)

SUMMARY_TEMPLATE = """Summarize the blog post below in ONE paragraph of about \
{target_words} words.

Hard limits, both enforced by a validator that rejects anything outside them:
- No fewer than {min_words} words and no more than {max_words} words.
- No more than {max_chars} characters in total.

Aim for {target_words} words, in at most two sentences. Overshooting is by far
the most common failure here, especially on long posts: cover only the single
main point and stop. Do not try to mention every section. Count the words before
you answer, and if you are near the limit, cut a clause rather than trusting it.

Other rules:
- Ground it only in the body text given. No outside knowledge, no speculation
  about what a release "means".
- Lead with what the post announces or argues. Do not start with "This post".
- Plain prose in your own words. No bullets, no headings, no marketing tone.
- Do not copy any run of more than eight consecutive words from the body.

Title: {title}

Body text:
{body}"""


def anti_slice_ok(tldr, title, body):
    """Strip any title prefix, then the first 40 chars must not appear in body."""
    text = tldr.strip()
    head = title.strip()
    if head and text.lower().startswith(head.lower()):
        text = text[len(head):].lstrip(" :–—-·").strip()
    probe = text[:40]
    if not probe:
        return False
    return probe not in body


def validate_summary(tldr, title, body, min_words, max_words):
    text = (tldr or "").strip()
    if not text:
        return "empty summary"
    if len(text) > TLDR_MAX_CHARS:
        return ("summary is %d characters, over the schema maxLength of %d"
                % (len(text), TLDR_MAX_CHARS))
    words = len(text.split())
    if not (min_words <= words <= max_words):
        return "summary is %d words, outside %d-%d" % (words, min_words, max_words)
    if text[0] in "-*#>" or "\n-" in text or "\n*" in text:
        return "summary contains bullets or headings"
    if not anti_slice_ok(text, title, body):
        return "summary is a mechanical slice of body_text"
    return None


def resolve_auth():
    """Return auth headers for the Messages API, or None if there is no credential.

    Preferred is ANTHROPIC_API_KEY, which is what the scheduled sandbox should
    carry. Falling back to the `ant` CLI's stored OAuth profile lets the script
    run locally after `ant auth login` without minting a key; OAuth tokens go on
    Authorization: Bearer with the oauth beta header, not on x-api-key. The token
    is never logged and never passed on a command line.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return {"x-api-key": key}
    token = os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not token:
        try:
            proc = subprocess.run(
                ["ant", "auth", "print-credentials", "--access-token"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=30)
            if proc.returncode == 0:
                token = proc.stdout.strip()
        except Exception:  # noqa: BLE001 - no credential is a clean abort, not a crash
            token = None
    if token:
        return {"authorization": "Bearer " + token,
                "anthropic-beta": "oauth-2025-04-20"}
    return None


def call_model(auth_headers, title, body, min_words, max_words, problem=None):
    aim_max = max(min_words + 5, max_words - TLDR_PROMPT_HEADROOM_WORDS)
    prompt = SUMMARY_TEMPLATE.format(
        min_words=min_words, max_words=aim_max, title=title,
        target_words=(min_words + aim_max) // 2,
        max_chars=TLDR_PROMPT_MAX_CHARS,
        body=body[:SUMMARY_INPUT_CHAR_CAP])
    # The correction goes in its own block AFTER the cache breakpoint, so the
    # retry's prefix is byte-identical to the first attempt's and reads from cache.
    block = {"type": "text", "text": prompt}
    if ENABLE_PROMPT_CACHE:
        block["cache_control"] = {"type": "ephemeral"}
    content = [block]
    if problem:
        content.append({"type": "text", "text":
                        "Your previous attempt was rejected: %s. Write a shorter "
                        "one that satisfies every limit above." % problem})
    payload = {
        "model": SUMMARY_MODEL,
        "max_tokens": SUMMARY_MAX_TOKENS,
        "system": SUMMARY_SYSTEM,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {"content-type": "application/json",
               "anthropic-version": ANTHROPIC_VERSION}
    headers.update(auth_headers)
    req = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text").strip()
    return text, data.get("usage") or {}


def summarize(auth_headers, post, min_words, max_words):
    """One API call per post. At most MAX_ATTEMPTS; then skip the post.

    A response that arrives but fails validation consumes an attempt rather than
    triggering an open-ended rewrite loop -- summaries never spin here.
    """
    last = None
    for i in range(1, MAX_ATTEMPTS + 1):
        try:
            text, usage = call_model(auth_headers, post["title"],
                                     post["body_text"], min_words, max_words,
                                     problem=last)
            RUN_USAGE.add(usage, retry=(i > 1))
        except Exception as exc:  # noqa: BLE001
            last = "API call failed: %s" % exc
            log("  summary attempt %d/%d: %s" % (i, MAX_ATTEMPTS, last))
            continue
        problem = validate_summary(text, post["title"], post["body_text"],
                                   min_words, max_words)
        if problem is None:
            return text
        last = problem
        log("  summary attempt %d/%d rejected: %s" % (i, MAX_ATTEMPTS, problem))
    raise SkipPost("summary failed validation %d times: %s" % (MAX_ATTEMPTS, last))


# ── JSON Schema validation (the gate) ─────────────────────────────────────────
DATE_RE_S = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE_S = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?"
                           r"(Z|[+-]\d{2}:\d{2})$")
URI_RE_S = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s]+$")


def _check_format(value, fmt, path, errors):
    if fmt == "date":
        if not DATE_RE_S.match(value):
            errors.append("%s: not a date: %r" % (path, value))
            return
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            errors.append("%s: not a real date: %r" % (path, value))
    elif fmt == "date-time":
        if not DATETIME_RE_S.match(value):
            errors.append("%s: not an RFC 3339 date-time: %r" % (path, value))
    elif fmt == "uri":
        if not URI_RE_S.match(value):
            errors.append("%s: not a URI: %r" % (path, value))


def validate_schema(instance, schema, root=None, path="$", errors=None):
    """Validate against the subset of JSON Schema used by posts.schema.json."""
    if errors is None:
        errors = []
    if root is None:
        root = schema

    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            errors.append("%s: unsupported $ref %r" % (path, ref))
            return errors
        target = root
        for part in ref[2:].split("/"):
            target = target.get(part, {})
        return validate_schema(instance, target, root, path, errors)

    if "const" in schema and instance != schema["const"]:
        errors.append("%s: expected const %r, got %r" % (path, schema["const"], instance))

    expected = schema.get("type")
    if expected:
        types = expected if isinstance(expected, list) else [expected]
        ok = False
        for t in types:
            if t == "object" and isinstance(instance, dict):
                ok = True
            elif t == "array" and isinstance(instance, list):
                ok = True
            elif t == "string" and isinstance(instance, str):
                ok = True
            elif t == "integer" and isinstance(instance, int) and not isinstance(instance, bool):
                ok = True
            elif t == "number" and isinstance(instance, (int, float)) and not isinstance(instance, bool):
                ok = True
            elif t == "boolean" and isinstance(instance, bool):
                ok = True
            elif t == "null" and instance is None:
                ok = True
        if not ok:
            errors.append("%s: expected type %r, got %s" % (path, expected, type(instance).__name__))
            return errors

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append("%s: shorter than minLength %d" % (path, schema["minLength"]))
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append("%s: longer than maxLength %d" % (path, schema["maxLength"]))
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append("%s: does not match pattern %r" % (path, schema["pattern"]))
        if "format" in schema:
            _check_format(instance, schema["format"], path, errors)

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append("%s: below minimum %r" % (path, schema["minimum"]))
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append("%s: above maximum %r" % (path, schema["maximum"]))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append("%s: missing required property %r" % (path, key))
        props = schema.get("properties", {})
        for key, value in instance.items():
            if key in props:
                validate_schema(value, props[key], root, "%s.%s" % (path, key), errors)
            elif schema.get("additionalProperties") is False:
                errors.append("%s: unknown property %r" % (path, key))

    if isinstance(instance, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(instance):
                validate_schema(item, item_schema, root, "%s[%d]" % (path, i), errors)
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append("%s: fewer than minItems %d" % (path, schema["minItems"]))

    return errors


def extra_checks(doc, cutoff, min_words, max_words):
    """The checks the schema cannot express. Scripted over the whole file."""
    problems, notes = [], []
    seen = set()
    for post in doc["posts"]:
        pid = post["id"]
        if pid in seen:
            problems.append("duplicate id %r" % pid)
        seen.add(pid)
        if post["published_at"] < cutoff:
            problems.append("%s: published_at %s is before cutoff %s"
                            % (pid, post["published_at"], cutoff))
        if not anti_slice_ok(post["tldr"], post["title"], post["body_text"]):
            problems.append("%s: tldr is a mechanical slice of body_text" % pid)
        markers = sorted(int(n) for n in re.findall(r"\[\[figure:(\d+)\]\]",
                                                    post["body_text"]))
        entries = sorted(f["n"] for f in post.get("figures", []))
        if markers != entries:
            problems.append("%s: figure markers %s != figures %s"
                            % (pid, markers, entries))
        if post["word_count"] != len(post["body_text"].split()):
            problems.append("%s: word_count does not match body_text" % pid)
        if post["content_hash"] != hashlib.sha256(
                post["body_text"].encode("utf-8")).hexdigest():
            problems.append("%s: content_hash does not match body_text" % pid)
        # TL;DR word bounds are a quality rule, not a validation failure.
        words = len(post["tldr"].split())
        if not (min_words <= words <= max_words):
            notes.append("%s: stored tldr is %d words, outside %d-%d"
                         % (pid, words, min_words, max_words))
    return problems, notes


# ── Atomic write ──────────────────────────────────────────────────────────────
def atomic_write_json(path, obj):
    """Write the complete file or nothing at all. Never a partial file."""
    body = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        # Re-read the temp file and parse it before it can replace real data.
        with open(tmp, encoding="utf-8") as fh:
            json.load(fh)
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── Git ───────────────────────────────────────────────────────────────────────
AUTH_MARKERS = ["authentication failed", "could not read username",
                "could not read password", "permission denied",
                "access denied", "invalid username or password",
                "terminal prompts disabled", "403 forbidden",
                "support for password authentication was removed"]


def git(*args, **kwargs):
    proc = subprocess.run(["git"] + list(args), cwd=REPO_ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=kwargs.get("timeout", 120))
    return proc


def git_commit_and_push(message, paths, do_push=True):
    """Exactly one commit per run. Push retries once, then aborts."""
    add = git("add", "--", *paths)
    if add.returncode != 0:
        raise AbortRun("git add failed: %s" % add.stderr.strip())

    staged = git("diff", "--cached", "--quiet")
    if staged.returncode == 0:
        log("nothing staged; no commit")
        return None

    commit = git("commit", "-m", message)
    if commit.returncode != 0:
        raise AbortRun("git commit failed: %s" % commit.stderr.strip())
    sha = git("rev-parse", "HEAD").stdout.strip()[:10]
    log("committed %s %s" % (sha, message))

    if not do_push:
        log("--no-push: leaving the commit local")
        return sha

    last = ""
    for i in range(1, MAX_ATTEMPTS + 1):
        push = git("push", "origin", "HEAD:main", timeout=180)
        if push.returncode == 0:
            log("pushed to origin/main")
            return sha
        last = (push.stderr or push.stdout or "").strip()
        low = last.lower()
        if any(m in low for m in AUTH_MARKERS):
            raise PushAuthError(last)
        log("push attempt %d/%d failed: %s" % (i, MAX_ATTEMPTS, last))
    raise AbortRun("git push failed after %d attempts: %s" % (MAX_ATTEMPTS, last))


# ── Main ──────────────────────────────────────────────────────────────────────
def load_json(path, label):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except ValueError as exc:
        raise AbortRun("%s exists but does not parse: %s" % (label, exc))


def run(argv):
    no_commit = "--no-commit" in argv
    no_push = "--no-push" in argv

    config = load_json(os.path.join(REPO_ROOT, "config.json"), "config.json")
    if config is None:
        raise AbortRun("config.json not found")

    index_url = os.environ.get("SYNC_INDEX_URL") or config["source"]["index_url"]
    window_days = config["window_days"]
    max_new = config["max_new_fetches_per_run"]
    max_fetches = config["max_page_fetches_per_run"]
    min_words = config["tldr"]["min_words"]
    max_words = config["tldr"]["max_words"]
    posts_path = os.path.join(REPO_ROOT, config["output"]["posts"])
    schema_path = os.path.join(REPO_ROOT, config["output"]["schema"])
    sync_path = os.path.join(REPO_ROOT, config["output"]["last_sync"])

    budget = Budget(WALL_CLOCK_BUDGET_S, max_fetches)
    started = utcnow()
    cutoff = (started.date() - timedelta(days=window_days)).isoformat()
    log("start %s | budget %ds | fetch cap %d | new cap %d | cutoff %s"
        % (rfc3339(started), WALL_CLOCK_BUDGET_S, max_fetches, max_new, cutoff))

    # Stage 1: load state.
    schema = load_json(schema_path, "posts.schema.json")
    if schema is None:
        raise AbortRun("posts.schema.json not found")
    stored_doc = load_json(posts_path, "data/posts.json")
    stored = {p["id"]: p for p in (stored_doc or {}).get("posts", [])}
    log("stage 1: %d stored posts" % len(stored))

    notes, skipped = [], []

    # Stage 2: discover page 1 only.
    if not budget.check("discover"):
        raise AbortRun("budget spent before discovery; nothing was fetched")
    index_html = http_get(index_url, budget, "fetch index")
    discovered = discover(index_html, index_url)
    if not discovered:
        raise AbortRun("discovery yielded zero posts from %s" % index_url)
    log("stage 2: discovered %d posts on page 1" % len(discovered))

    # Stage 3: diff.
    # Drop anything already outside the retention window before diffing. Page 1
    # routinely carries posts older than a short window_days, and those were just
    # pruned from storage - without this they look "new" every run, get fetched
    # and summarized, and then fail the cutoff check, aborting the run and burning
    # the API budget daily. A post out of window is never worth fetching: it would
    # be pruned again the moment it was added.
    aged_out = [i for i in discovered
                if i["index_date"] and i["index_date"] < cutoff]
    if aged_out:
        log("stage 3: %d discovered post(s) are older than the cutoff; ignoring"
            % len(aged_out))
    candidates = [i for i in discovered
                  if not (i["index_date"] and i["index_date"] < cutoff)]

    new_ids, changed_ids = [], []
    for item in candidates:
        prior = stored.get(item["id"])
        if prior is None:
            new_ids.append(item)
        elif (prior.get("title") != item["title"]
              or (item["index_date"] and prior.get("published_at") != item["index_date"])):
            changed_ids.append(item)
    pruned = [pid for pid, p in stored.items() if p["published_at"] < cutoff]
    todo = new_ids + changed_ids
    todo.sort(key=lambda i: (i["index_date"] or "", i["id"]), reverse=True)
    if len(todo) > max_new:
        notes.append("%d posts qualified for fetch; capped at %d, remainder next run"
                     % (len(todo), max_new))
        todo = todo[:max_new]
    log("stage 3: %d new, %d changed, %d to prune, %d to fetch this run"
        % (len(new_ids), len(changed_ids), len(pruned), len(todo)))

    # Stage 4-5: fetch, extract, summarize. Budget re-checked before every post.
    auth_headers = resolve_auth() if todo else None
    if todo and not auth_headers:
        raise AbortRun(
            "no API credential found, and %d post(s) need a summary. Set "
            "ANTHROPIC_API_KEY in the environment (or run `ant auth login`)."
            % len(todo))

    fetched, changed_count = {}, 0
    for item in todo:
        if not budget.check("fetch %s" % item["id"]):
            notes.append("wall-clock budget ended the run with %d post(s) unfetched"
                         % (len(todo) - len(fetched) - len(skipped)))
            break
        if budget.fetches >= max_fetches:
            notes.append("HTTP fetch cap of %d reached; remainder next run" % max_fetches)
            break
        log("stage 4: %s" % item["id"])
        try:
            page = http_get(item["url"], budget, "fetch %s" % item["id"])
            record = extract_post(page, item["url"], item["title"],
                                  item["index_date"], item.get("category"))
        except SkipPost as exc:
            log("  skipped: %s" % exc)
            skipped.append({"id": item["id"], "reason": str(exc)})
            continue

        if record["published_at"] < cutoff:
            log("  out of window (%s); not stored" % record["published_at"])
            continue

        prior = stored.get(item["id"])
        if prior and prior.get("content_hash") == record["content_hash"]:
            # Unchanged after all: keep the stored record, including fetched_at.
            log("  content unchanged; keeping stored record")
            fetched[item["id"]] = prior
            continue

        try:
            record["tldr"] = summarize(auth_headers, record, min_words, max_words)
        except SkipPost as exc:
            log("  skipped: %s" % exc)
            skipped.append({"id": item["id"], "reason": str(exc)})
            continue
        log("  summarized (%d words)" % len(record["tldr"].split()))
        fetched[item["id"]] = record
        if prior:
            changed_count += 1

    # Stage 6: assemble the complete document in memory.
    merged = []
    for pid, post in stored.items():
        if pid in pruned:
            continue
        merged.append(fetched.get(pid, post))
    for pid, post in fetched.items():
        if pid not in stored:
            merged.append(post)
    merged.sort(key=lambda p: (p["published_at"], p["id"]), reverse=True)

    new_doc = {
        "version": 1,
        "generated_at": rfc3339(utcnow()),
        "window_days": window_days,
        "source_url": index_url,
        "posts": merged,
    }

    # Stage 7: validate before touching disk. This is the gate.
    errors = validate_schema(new_doc, schema)
    if errors:
        raise AbortRun("schema validation failed (%d error(s)); nothing written:\n  %s"
                       % (len(errors), "\n  ".join(errors[:20])))
    problems, bound_notes = extra_checks(new_doc, cutoff, min_words, max_words)
    if problems:
        raise AbortRun("post-schema checks failed (%d); nothing written:\n  %s"
                       % (len(problems), "\n  ".join(problems[:20])))
    notes.extend(bound_notes)
    log("stage 7: validated %d posts" % len(merged))

    # Stage 8: write atomically, then commit exactly once.
    new_count = sum(1 for pid in fetched if pid not in stored)
    counts = {
        "discovered": len(discovered),
        "new": new_count,
        "changed": changed_count,
        "unchanged": len(merged) - new_count - changed_count,
        "pruned": len(pruned),
        "skipped": len(skipped),
    }
    # Compare records by id, not the serialised list: sort order is presentation,
    # not content, and a pure reordering must never produce a commit. No new,
    # changed or pruned post means no commit at all.
    content_changed = ({p["id"]: p for p in (stored_doc or {}).get("posts", [])}
                       != {p["id"]: p for p in merged})

    last_sync = {
        "ran_at": rfc3339(started),
        "source_url": index_url,
        "window_days": window_days,
        "counts": counts,
        "skipped": skipped,
        "notes": notes,
    }

    if content_changed:
        atomic_write_json(posts_path, new_doc)
    atomic_write_json(sync_path, last_sync)
    log("stage 8: counts %s" % json.dumps(counts))

    sha = None
    if not content_changed:
        log("no new, changed, or pruned posts; no commit")
    elif no_commit:
        log("--no-commit: files written, nothing committed")
    else:
        message = "%s %s (+%d new, %d changed, %d pruned)" % (
            config["commit_prefix"], started.date().isoformat(),
            counts["new"], counts["changed"], counts["pruned"])
        sha = git_commit_and_push(
            message,
            [os.path.relpath(posts_path, REPO_ROOT),
             os.path.relpath(sync_path, REPO_ROOT)],
            do_push=not no_push)

    log("api usage: %s" % RUN_USAGE.summary())
    if RUN_USAGE.calls:
        rate = RUN_USAGE.retries / float(RUN_USAGE.calls)
        log("retry rate %.0f%% vs %.0f%% caching break-even (cache %s)"
            % (rate * 100, CACHE_BREAKEVEN_RETRY_RATE * 100,
               "on" if ENABLE_PROMPT_CACHE else "off"))
    log("done in %.1fs | commits: %d | exit 0" % (budget.elapsed(), 1 if sha else 0))
    return 0


def main(argv):
    try:
        return run(argv)
    except PushAuthError as exc:
        print("[sync] HARD STOP: push authentication failed. The commit is local "
              "and unpushed; no alternative write path was attempted.\n%s" % exc,
              file=sys.stderr)
        return 2
    except AbortRun as exc:
        print("[sync] ABORT: %s" % exc, file=sys.stderr)
        print("[sync] data/posts.json was not modified.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
