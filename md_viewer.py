#!/usr/bin/env python3
"""md_viewer.py: a small Markdown viewer with no dependencies.

Converts Markdown (CommonMark plus the GitHub extras: tables,
strikethrough, task lists, bare-URL links, alerts and LaTeX formulas
in $...$ and $$...$$) to HTML styled like GitHub, and serves it to the
browser from a local web server.  Formulas are drawn by MathJax, which
the page loads from the internet.
The page reloads itself whenever the file is saved, so you can edit in
vim and watch the result.

Usage:
  python md_viewer.py FILE.md             view FILE.md in the browser
  python md_viewer.py DIRECTORY           list a directory's .md files
  python md_viewer.py FILE.md -o F.html   write a static HTML file

In the browser, the key "t" switches between light and dark themes.
"""

import argparse
import html
import http.server
import json
import os
import mimetypes
import re
import sys
import unicodedata
import urllib.parse
import webbrowser
from collections import namedtuple
from pathlib import Path

MD_SUFFIXES = {'.md', '.markdown', '.mdown', '.mkd'}

# ---------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------

ESCAPABLE = set('!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~')


def esc(text):
    """Escapes text for HTML element content."""
    return html.escape(text, quote=False)


def esc_attr(text):
    """Escapes text for an HTML attribute value."""
    return html.escape(text, quote=True)


def unescape_md(text):
    """Removes backslash escapes and decodes entities (URLs, titles)."""
    text = re.sub(r'\\([!-/:-@\[-`{-~])', r'\1', text)
    return html.unescape(text)


def norm_url(url):
    """Percent-encodes what a URL may not contain (spaces, ...)."""
    return urllib.parse.quote(url, safe="%:/?#[]@!$&'()*+,;=~-._")


def norm_label(label):
    """Normalizes a reference label: case and white space."""
    return ' '.join(label.split()).casefold()


def strip_tags(fragment):
    """Plain text of an HTML fragment."""
    return html.unescape(re.sub(r'<[^>]*>', '', fragment))


def is_space(c):
    return c == '' or c.isspace()


def is_punct(c):
    return c != '' and (c in ESCAPABLE
                        or unicodedata.category(c)[0] in 'PS')


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def is_blank(line):
    return not line.strip()


# ---------------------------------------------------------------------
# Inline parser: emphasis, code, links, images, HTML, line breaks
# ---------------------------------------------------------------------

ATTRIBUTE = (r'(?:\s+[A-Za-z_:][\w.:-]*'
             r'(?:\s*=\s*(?:[^\s"\'=<>`]+|\'[^\']*\'|"[^"]*"))?)')
OPEN_TAG = r'<[A-Za-z][A-Za-z0-9-]*' + ATTRIBUTE + r'*\s*/?>'
CLOSE_TAG = r'</[A-Za-z][A-Za-z0-9-]*\s*>'

RE_ENTITY = re.compile(r'&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}'
                       r'|[A-Za-z][A-Za-z0-9]{1,31});')
RE_TICKS = re.compile(r'`+')
RE_COMMENT = re.compile(r'<!--[\s\S]*?-->')
RE_OPEN_TAG = re.compile(OPEN_TAG)
RE_CLOSE_TAG = re.compile(CLOSE_TAG)
RE_AUTOLINK = re.compile(r'<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^\s<>]*)>')
RE_EMAIL = re.compile(r'<([\w.+-]+@[\w-]+(?:\.[\w-]+)+)>')
RE_BARE_URL = re.compile(r'(?:https?://|www\.)[^\s<]+')


def math_html(tex, display):
    """A formula for MathJax, which only looks inside the elements
    marked tex2jax_process (see the page template)."""
    if display:
        return ('<span class="tex2jax_process math-display">\\[%s\\]'
                '</span>' % esc(tex))
    return ('<span class="tex2jax_process math-inline">\\(%s\\)</span>'
            % esc(tex))


def trim_url(url):
    """Drops trailing punctuation that belongs to the sentence."""
    while url:
        if url[-1] in '?!.,:;*_~\'"':
            url = url[:-1]
        elif url[-1] == ')' and url.count(')') > url.count('('):
            url = url[:-1]
        else:
            break
    return url


class Delim:
    """A run of *, _ or ~ that may open or close emphasis."""
    __slots__ = ('char', 'count', 'orig', 'can_open', 'can_close',
                 'opens', 'closes')

    def __init__(self, char, count, can_open, can_close):
        self.char = char
        self.count = count
        self.orig = count
        self.can_open = can_open
        self.can_close = can_close
        self.opens = []
        self.closes = []

    def html(self):
        return (''.join(self.closes) + self.char * self.count
                + ''.join(self.opens))


class InlineParser:

    def __init__(self, refs):
        self.refs = refs

    def render(self, text, in_link=False):
        nodes = self.tokenize(text, in_link)
        self.emphasis([n for n in nodes if isinstance(n, Delim)])
        return ''.join(n if isinstance(n, str) else n.html()
                       for n in nodes)

    # The tokenizer turns the text into a list of HTML strings and
    # Delim objects; emphasis() later decides which delimiters pair up.
    def tokenize(self, s, in_link):
        out = []
        buf = []
        n = len(s)
        i = 0

        def flush():
            if buf:
                out.append(esc(''.join(buf)))
                buf.clear()

        while i < n:
            c = s[i]
            hit = None

            if c == '\\' and i + 1 < n:
                if s[i + 1] == '\n':
                    flush()
                    out.append('<br>\n')
                    i += 2
                elif s[i + 1] in ESCAPABLE:
                    buf.append(s[i + 1])
                    i += 2
                else:
                    buf.append(c)
                    i += 1
                continue

            if c == '`':
                hit = self.code_span(s, i)
                if not hit:
                    run = RE_TICKS.match(s, i).group()
                    buf.append(run)
                    i += len(run)
                    continue
            elif c == '<':
                hit = self.angle(s, i)
            elif c == '!' and s.startswith('[', i + 1):
                hit = self.link(s, i, image=True)
            elif c == '[' and not in_link:
                hit = self.link(s, i, image=False)
            elif c in '*_~':
                j = i
                while j < n and s[j] == c:
                    j += 1
                if c == '~' and j - i > 2:
                    buf.append(s[i:j])
                else:
                    flush()
                    out.append(self.delim(s, i, j))
                i = j
                continue
            elif c == '$':
                hit = self.math(s, i)
            elif c == '&':
                m = RE_ENTITY.match(s, i)
                if m:
                    hit = (m.group(), m.end())
            elif c == '\n':
                text = ''.join(buf)
                kept = text.rstrip(' ')
                buf[:] = [kept]
                flush()
                hard = len(text) - len(kept) >= 2
                out.append('<br>\n' if hard else '\n')
                i += 1
                continue
            elif (c in 'hw' and not in_link
                  and (i == 0 or s[i - 1] in ' \t\n*_~(')):
                hit = self.bare_url(s, i)

            if hit:
                flush()
                out.append(hit[0])
                i = hit[1]
            else:
                buf.append(c)
                i += 1
        flush()
        return out

    @staticmethod
    def delim(s, i, j):
        """Creates a Delim for s[i:j], with the CommonMark flanking
        rules deciding whether it can open and/or close."""
        c = s[i]
        before = s[i - 1] if i > 0 else ''
        after = s[j] if j < len(s) else ''
        left = not is_space(after) and (
            not is_punct(after) or is_space(before) or is_punct(before))
        right = not is_space(before) and (
            not is_punct(before) or is_space(after) or is_punct(after))
        if c == '_':
            can_open = left and (not right or is_punct(before))
            can_close = right and (not left or is_punct(after))
        else:
            can_open, can_close = left, right
        return Delim(c, j - i, can_open, can_close)

    @staticmethod
    def find_ticks(s, start, length):
        for m in RE_TICKS.finditer(s, start):
            if len(m.group()) == length:
                return m.start()
        return -1

    def code_span(self, s, i):
        run = RE_TICKS.match(s, i).group()
        start = i + len(run)
        end = self.find_ticks(s, start, len(run))
        if end < 0:
            return None
        code = s[start:end].replace('\n', ' ')
        if (len(code) > 1 and code[0] == ' ' and code[-1] == ' '
                and code.strip(' ')):
            code = code[1:-1]
        return '<code>%s</code>' % esc(code), end + len(run)

    @staticmethod
    def math(s, i):
        """$$...$$ (displayed) and $...$ (inline) formulas, kept
        verbatim for MathJax.  As in Pandoc, an inline formula's $
        must touch its content, and the closing $ must not be followed
        by a digit, so amounts like "$10 and $20" stay text."""
        n = len(s)
        if s.startswith('$$', i):
            end = s.find('$$', i + 2)
            if end < 0 or not s[i + 2:end].strip():
                return None
            return math_html(s[i + 2:end], True), end + 2
        if i + 1 >= n or s[i + 1].isspace() or s[i + 1] == '$':
            return None
        j = i + 1
        while j < n:
            c = s[j]
            if c == '\\':
                j += 2
                continue
            if (c == '$' and not s[j - 1].isspace()
                    and not (j + 1 < n and s[j + 1].isdigit())):
                return math_html(s[i + 1:j], False), j + 1
            j += 1
        return None

    @staticmethod
    def angle(s, i):
        """HTML comments and tags (passed through) and <autolinks>."""
        for rx in (RE_COMMENT, RE_OPEN_TAG, RE_CLOSE_TAG):
            m = rx.match(s, i)
            if m:
                return m.group(), m.end()
        m = RE_AUTOLINK.match(s, i)
        if m:
            url = m.group(1)
            return ('<a href="%s">%s</a>'
                    % (esc_attr(norm_url(url)), esc(url)), m.end())
        m = RE_EMAIL.match(s, i)
        if m:
            mail = m.group(1)
            return ('<a href="mailto:%s">%s</a>'
                    % (esc_attr(mail), esc(mail)), m.end())
        return None

    @staticmethod
    def bare_url(s, i):
        m = RE_BARE_URL.match(s, i)
        if not m:
            return None
        url = trim_url(m.group())
        if url.startswith('www.'):
            if '.' not in url[4:]:
                return None
            href = 'http://' + url
        else:
            if len(url) <= len('https://'):
                return None
            href = url
        return ('<a href="%s">%s</a>'
                % (esc_attr(norm_url(href)), esc(url)), i + len(url))

    def find_bracket(self, s, i):
        """Index of the ] that closes a [ just before position i."""
        depth = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == '\\':
                i += 2
                continue
            if c == '`':
                run = RE_TICKS.match(s, i).group()
                end = self.find_ticks(s, i + len(run), len(run))
                i = end + len(run) if end >= 0 else i + len(run)
                continue
            if c == '[':
                depth += 1
            elif c == ']':
                if depth == 0:
                    return i
                depth -= 1
            i += 1
        return -1

    @staticmethod
    def destination(s, k):
        """Parses '(url "title")' starting at s[k] == '('.
        Returns (url, title, end) or None."""
        n = len(s)
        i = k + 1
        while i < n and s[i] in ' \t\n':
            i += 1
        if i < n and s[i] == '<':
            j = i + 1
            while j < n and s[j] not in '>\n':
                j += 2 if s[j] == '\\' else 1
            if j >= n or s[j] != '>':
                return None
            url = s[i + 1:j]
            i = j + 1
        else:
            j = i
            depth = 0
            while j < n:
                c = s[j]
                if c == '\\' and j + 1 < n:
                    j += 2
                    continue
                if c.isspace():
                    break
                if c == '(':
                    depth += 1
                elif c == ')':
                    if depth == 0:
                        break
                    depth -= 1
                j += 1
            url = s[i:j]
            i = j
        title = None
        gap = i
        while i < n and s[i] in ' \t\n':
            i += 1
        if i < n and i > gap and s[i] in '"\'(':
            closer = ')' if s[i] == '(' else s[i]
            j = i + 1
            while j < n and s[j] != closer:
                j += 2 if s[j] == '\\' else 1
            if j >= n:
                return None
            title = unescape_md(s[i + 1:j])
            i = j + 1
            while i < n and s[i] in ' \t\n':
                i += 1
        if i < n and s[i] == ')':
            return unescape_md(url), title, i + 1
        return None

    def link(self, s, i, image):
        start = i + 2 if image else i + 1
        close = self.find_bracket(s, start)
        if close < 0:
            return None
        label = s[start:close]
        k = close + 1
        target = None
        if s.startswith('(', k):
            target = self.destination(s, k)
        if target:
            url, title, end = target
        else:
            if s.startswith('[', k):
                m = s.find(']', k + 1)
                if m < 0:
                    return None
                name, end = (s[k + 1:m] or label), m + 1
            else:
                name, end = label, k
            ref = self.refs.get(norm_label(name))
            if not ref:
                return None
            url, title = ref
        inner = self.render(label, in_link=True)
        title_attr = ' title="%s"' % esc_attr(title) if title else ''
        if image:
            return ('<img src="%s" alt="%s"%s>'
                    % (esc_attr(norm_url(url)),
                       esc_attr(strip_tags(inner)), title_attr), end)
        return ('<a href="%s"%s>%s</a>'
                % (esc_attr(norm_url(url)), title_attr, inner), end)

    # The CommonMark "process emphasis" procedure, slightly simplified.
    def emphasis(self, delims):
        ci = 0
        while ci < len(delims):
            closer = delims[ci]
            if not closer.can_close or closer.count == 0:
                ci += 1
                continue
            oi = ci - 1
            while oi >= 0 and not self.pairs(delims[oi], closer):
                oi -= 1
            if oi < 0:
                ci += 1
                continue
            opener = delims[oi]
            if closer.char == '~':
                use, tag = closer.count, 'del'
            elif opener.count >= 2 and closer.count >= 2:
                use, tag = 2, 'strong'
            else:
                use, tag = 1, 'em'
            opener.count -= use
            closer.count -= use
            opener.opens.insert(0, '<%s>' % tag)
            closer.closes.append('</%s>' % tag)
            for d in delims[oi + 1:ci]:
                d.can_open = d.can_close = False
            if closer.count == 0:
                ci += 1

    @staticmethod
    def pairs(opener, closer):
        if (opener.char != closer.char or not opener.can_open
                or opener.count == 0):
            return False
        if closer.char == '~':
            return opener.count == closer.count
        if ((opener.can_close or closer.can_open)
                and (opener.orig + closer.orig) % 3 == 0
                and not (opener.orig % 3 == 0
                         and closer.orig % 3 == 0)):
            return False
        return True


# ---------------------------------------------------------------------
# Block parser: paragraphs, headings, lists, quotes, code, tables, HTML
# ---------------------------------------------------------------------

RE_ATX = re.compile(r'^ {0,3}(#{1,6})(?=[ \t]|$)[ \t]*(.*?)'
                    r'(?:[ \t]+#+)?[ \t]*$')
RE_SETEXT = re.compile(r'^ {0,3}(=+|-+)[ \t]*$')
RE_FENCE = re.compile(r'^( {0,3})(`{3,}|~{3,})(.*)$')
RE_HR = re.compile(r'^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}'
                   r'|(?:_[ \t]*){3,})$')
RE_QUOTE = re.compile(r'^ {0,3}> ?')
RE_BULLET = re.compile(r'^( {0,3})([-+*])([ \t]+|$)')
RE_ORDERED = re.compile(r'^( {0,3})(\d{1,9})([.)])([ \t]+|$)')
RE_TABLE_DELIM = re.compile(r'^ {0,3}\|?[ \t]*:?-+:?[ \t]*'
                            r'(?:\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$')
RE_REFDEF = re.compile(r'^ {0,3}\[((?:[^\]\\]|\\.)+)\]:[ \t]*'
                       r'<?([^\s>]+)>?'
                       r'(?:[ \t]+("[^"]*"|\'[^\']*\'|\([^)]*\)))?'
                       r'[ \t]*$')
RE_TASK = re.compile(r'^\[([ xX])\][ \t]+(?=\S)')
RE_ALERT = re.compile(r'^[ \t]*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]'
                      r'[ \t]*$', re.I)

RE_HTML1 = re.compile(r'^ {0,3}<(script|pre|style|textarea)(?:\s|>|$)',
                      re.I)
RE_HTML1_END = re.compile(r'</(?:script|pre|style|textarea)>', re.I)
RE_HTML2 = re.compile(r'^ {0,3}<!--')
RE_HTML6 = re.compile(r'^ {0,3}</?([A-Za-z][A-Za-z0-9-]*)(?:\s|/?>|$)')
RE_HTML7 = re.compile(r'^ {0,3}(?:' + OPEN_TAG + '|' + CLOSE_TAG
                      + r')[ \t]*$')
BLOCK_TAGS = set('''
    address article aside base basefont blockquote body caption center
    col colgroup dd details dialog dir div dl dt fieldset figcaption
    figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr
    html iframe legend li link main menu menuitem nav noframes ol
    optgroup option p param search section summary table tbody td tfoot
    th thead title tr track ul'''.split())

Marker = namedtuple('Marker', 'ordered char start width first')


def html_kind(line):
    """CommonMark HTML block type of a line: 1, 2, 6, 7 or 0."""
    if RE_HTML1.match(line):
        return 1
    if RE_HTML2.match(line):
        return 2
    m = RE_HTML6.match(line)
    if m and m.group(1).lower() in BLOCK_TAGS:
        return 6
    if RE_HTML7.match(line):
        return 7
    return 0


def fence_of(line):
    """The match of an opening code fence, or None."""
    m = RE_FENCE.match(line)
    if m and m.group(2)[0] == '`' and '`' in m.group(3):
        return None
    return m


def list_marker(line):
    """Parses a list item's marker.  'width' is the column where the
    item's content starts; 'first' is the content of the first line."""
    m = RE_BULLET.match(line)
    if m:
        ordered, char, start = False, m.group(2), None
        width, gap = len(m.group(1)) + 1, m.group(3)
    else:
        m = RE_ORDERED.match(line)
        if not m:
            return None
        ordered, char, start = True, m.group(3), int(m.group(2))
        width = len(m.group(1)) + len(m.group(2)) + 1
        gap = m.group(4)
    rest = line[m.end():]
    if is_blank(rest):
        return Marker(ordered, char, start, width + 1, '')
    if len(gap) > 4:
        return Marker(ordered, char, start, width + 1,
                      ' ' * (len(gap) - 1) + rest)
    return Marker(ordered, char, start, width + len(gap), rest)


def interrupts(line):
    """True if the line starts a block that ends a paragraph."""
    if RE_ATX.match(line) or RE_HR.match(line) or RE_QUOTE.match(line):
        return True
    if fence_of(line):
        return True
    if html_kind(line) not in (0, 7):
        return True
    m = list_marker(line)
    if m and m.first.strip() and (not m.ordered or m.start == 1):
        return True
    return False


def lazy_ok(collected):
    """True if the collected lines of a quote or list item end in an
    open paragraph, which a 'lazy' line may continue."""
    if not collected or is_blank(collected[-1]):
        return False
    in_fence = False
    for line in collected:
        if fence_of(line):
            in_fence = not in_fence
    last = collected[-1]
    return not (in_fence or RE_ATX.match(last) or RE_HR.match(last)
                or indent_of(last) >= 4)


def split_row(line):
    s = line.strip()
    if s.startswith('|'):
        s = s[1:]
    if s.endswith('|') and not s.endswith('\\|'):
        s = s[:-1]
    return [c.strip().replace('\\|', '|')
            for c in re.split(r'(?<!\\)\|', s)]


class BlockParser:

    def __init__(self, inline):
        self.inline = inline
        self.slugs = {}

    # parse() returns a list of blocks, each ('p', inline HTML) for a
    # paragraph or ('b', HTML) for anything else, and whether blank
    # lines separated any of them (which makes a list item "loose").
    def parse(self, lines):
        blocks = []
        blank_between = False
        pending_blank = False
        i = 0
        while i < len(lines):
            if is_blank(lines[i]):
                pending_blank = bool(blocks)
                i += 1
                continue
            if pending_blank:
                blank_between = True
                pending_blank = False
            i = self.block(lines, i, blocks)
        return blocks, blank_between

    @staticmethod
    def render(blocks, tight=False):
        return '\n'.join(h if kind == 'b' or tight else '<p>%s</p>' % h
                         for kind, h in blocks)

    def block(self, lines, i, out):
        line = lines[i]
        if indent_of(line) >= 4:
            return self.indented_code(lines, i, out)
        m = fence_of(line)
        if m:
            return self.fenced_code(lines, i, out, m)
        m = RE_ATX.match(line)
        if m:
            self.heading(len(m.group(1)), m.group(2), out)
            return i + 1
        if RE_HR.match(line):
            out.append(('b', '<hr>'))
            return i + 1
        if RE_QUOTE.match(line):
            return self.quote(lines, i, out)
        if list_marker(line):
            return self.list_block(lines, i, out)
        kind = html_kind(line)
        if kind:
            return self.html_block(lines, i, out, kind)
        if self.table_start(lines, i):
            return self.table(lines, i, out)
        return self.paragraph(lines, i, out)

    def slug(self, inner_html):
        """GitHub's anchor for a heading, unique in the document."""
        text = strip_tags(inner_html).strip().lower()
        base = re.sub(r'[^\w\- ]', '', text).replace(' ', '-')
        count = self.slugs.get(base, 0)
        self.slugs[base] = count + 1
        return base if count == 0 else '%s-%d' % (base, count)

    def heading(self, level, text, out):
        inner = self.inline.render(text.strip())
        hid = esc_attr(self.slug(inner))
        out.append(('b', '<h%d id="%s"><a class="anchor" href="#%s" '
                    'aria-hidden="true"></a>%s</h%d>'
                    % (level, hid, hid, inner, level)))

    def indented_code(self, lines, i, out):
        body = []
        while i < len(lines) and (is_blank(lines[i])
                                  or indent_of(lines[i]) >= 4):
            body.append(lines[i][4:])
            i += 1
        while body and is_blank(body[-1]):
            body.pop()
            i -= 1
        out.append(('b', '<pre><code>%s\n</code></pre>'
                    % esc('\n'.join(body))))
        return i

    def fenced_code(self, lines, i, out, m):
        indent, fence = len(m.group(1)), m.group(2)
        info = m.group(3).strip()
        close = re.compile(r'^ {0,3}%s{%d,}[ \t]*$'
                           % (re.escape(fence[0]), len(fence)))
        body = []
        i += 1
        while i < len(lines) and not close.match(lines[i]):
            line = lines[i]
            body.append(line[min(indent, indent_of(line)):])
            i += 1
        i += 1  # the closing fence (or the end of the container)
        lang = unescape_md(info.split()[0]) if info else ''
        if lang == 'math':  # GitHub's ```math blocks
            out.append(('b', '<p>%s</p>'
                        % math_html('\n'.join(body), True)))
            return i
        cls = ' class="language-%s"' % esc_attr(lang) if lang else ''
        code = esc('\n'.join(body)) + ('\n' if body else '')
        out.append(('b', '<pre><code%s>%s</code></pre>' % (cls, code)))
        return i

    def quote(self, lines, i, out):
        inner = []
        while i < len(lines):
            line = lines[i]
            m = RE_QUOTE.match(line)
            if m:
                inner.append(line[m.end():])
            elif (not is_blank(line) and lazy_ok(inner)
                  and not interrupts(line)):
                inner.append(line)
            else:
                break
            i += 1
        alert = RE_ALERT.match(inner[0]) if inner else None
        if alert:
            kind = alert.group(1).lower()
            blocks, _ = self.parse(inner[1:])
            out.append(('b', '<div class="markdown-alert '
                        'markdown-alert-%s">\n<p class="markdown-alert-'
                        'title">%s</p>\n%s\n</div>'
                        % (kind, kind.capitalize(), self.render(blocks))))
        else:
            blocks, _ = self.parse(inner)
            out.append(('b', '<blockquote>\n%s\n</blockquote>'
                        % self.render(blocks)))
        return i

    def list_block(self, lines, i, out):
        first = list_marker(lines[i])
        n = len(lines)
        items = []
        loose = False
        while i < n:
            mk = list_marker(lines[i])
            if (not mk or mk.ordered != first.ordered
                    or mk.char != first.char or RE_HR.match(lines[i])):
                break
            body = [mk.first]
            i += 1
            while i < n:
                line = lines[i]
                if is_blank(line):
                    body.append('')
                elif indent_of(line) >= mk.width:
                    body.append(line[mk.width:])
                elif (not list_marker(line) and not interrupts(line)
                      and lazy_ok(body)):
                    body.append(line)
                else:
                    break
                i += 1
            # Blank lines after the item belong to what follows it.
            while len(body) > 1 and is_blank(body[-1]):
                body.pop()
                i -= 1
            task = ''
            t = RE_TASK.match(body[0])
            if t:
                checked = ' checked' if t.group(1) in 'xX' else ''
                task = ('<input type="checkbox" disabled%s> ' % checked)
                body[0] = body[0][t.end():]
            blocks, blank = self.parse(body)
            loose = loose or blank
            items.append((blocks, task))
            j = i
            while j < n and is_blank(lines[j]):
                j += 1
            nxt = list_marker(lines[j]) if j < n else None
            if j > i and nxt and nxt.ordered == first.ordered \
                    and nxt.char == first.char \
                    and not RE_HR.match(lines[j]):
                loose = True
                i = j
            elif j > i:
                break
        tag = 'ol' if first.ordered else 'ul'
        start = ''
        if first.ordered and first.start != 1:
            start = ' start="%d"' % first.start
        parts = ['<%s%s>' % (tag, start)]
        for blocks, task in items:
            cls = ' class="task-list-item"' if task else ''
            parts.append('<li%s>%s%s</li>'
                         % (cls, task, self.render(blocks, not loose)))
        parts.append('</%s>' % tag)
        out.append(('b', '\n'.join(parts)))
        return i

    def html_block(self, lines, i, out, kind):
        body = []
        if kind in (1, 2):
            end = RE_HTML1_END if kind == 1 else re.compile('-->')
            while i < len(lines):
                body.append(lines[i])
                i += 1
                if end.search(body[-1]):
                    break
        else:
            while i < len(lines) and not is_blank(lines[i]):
                body.append(lines[i])
                i += 1
        out.append(('b', '\n'.join(body)))
        return i

    @staticmethod
    def table_start(lines, i):
        return (i + 1 < len(lines) and '|' in lines[i]
                and RE_TABLE_DELIM.match(lines[i + 1])
                and len(split_row(lines[i]))
                == len(split_row(lines[i + 1])))

    def table(self, lines, i, out):
        head = split_row(lines[i])
        aligns = []
        for cell in split_row(lines[i + 1]):
            if cell.startswith(':') and cell.endswith(':'):
                aligns.append(' style="text-align: center"')
            elif cell.endswith(':'):
                aligns.append(' style="text-align: right"')
            elif cell.startswith(':'):
                aligns.append(' style="text-align: left"')
            else:
                aligns.append('')
        i += 2

        def row(cells, tag):
            cells = (cells + [''] * len(head))[:len(head)]
            return '<tr>%s</tr>' % ''.join(
                '<%s%s>%s</%s>' % (tag, a, self.inline.render(c), tag)
                for c, a in zip(cells, aligns))

        parts = ['<table>', '<thead>', row(head, 'th'), '</thead>']
        body = []
        while (i < len(lines) and not is_blank(lines[i])
               and not interrupts(lines[i])):
            body.append(row(split_row(lines[i]), 'td'))
            i += 1
        if body:
            parts += ['<tbody>'] + body + ['</tbody>']
        parts.append('</table>')
        out.append(('b', '\n'.join(parts)))
        return i

    def paragraph(self, lines, i, out):
        buf = [lines[i]]
        i += 1
        while i < len(lines):
            line = lines[i]
            if is_blank(line):
                break
            # Inside an open $$ formula, nothing can end the paragraph.
            if sum(b.count('$$') for b in buf) % 2 == 1:
                buf.append(line)
                i += 1
                continue
            m = RE_SETEXT.match(line)
            if m:
                level = 1 if m.group(1)[0] == '=' else 2
                self.heading(level,
                             '\n'.join(b.strip() for b in buf), out)
                return i + 1
            if interrupts(line) or self.table_start(lines, i):
                break
            buf.append(line)
            i += 1
        text = '\n'.join(b.lstrip() for b in buf).rstrip()
        out.append(('p', self.inline.render(text)))
        return i


# ---------------------------------------------------------------------
# From Markdown text to an HTML fragment
# ---------------------------------------------------------------------

def split_lines(text):
    """Splits text into lines, turning leading tabs into spaces."""
    lines = []
    for line in text.replace('\r\n', '\n').replace('\r', '\n') \
                    .replace('\0', '\ufffd').split('\n'):
        lead = re.match(r'[ \t]*', line).group()
        lines.append(lead.expandtabs(4) + line[len(lead):])
    return lines


def extract_refs(lines):
    """Removes link reference definitions ([name]: url "title") and
    returns the remaining lines and the definitions."""
    refs = {}
    kept = []
    in_fence = False
    may_define = True
    for line in lines:
        if fence_of(line):
            in_fence = not in_fence
        m = None if in_fence else RE_REFDEF.match(line)
        if m and may_define:
            label = norm_label(m.group(1))
            title = m.group(3)[1:-1] if m.group(3) else None
            refs.setdefault(label, (unescape_md(m.group(2)),
                                    unescape_md(title) if title
                                    else None))
            continue
        kept.append(line)
        may_define = is_blank(line)
    return kept, refs


def markdown_to_html(text):
    lines, refs = extract_refs(split_lines(text))
    parser = BlockParser(InlineParser(refs))
    blocks, _ = parser.parse(lines)
    return parser.render(blocks)


def read_text(path):
    data = path.read_bytes()
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16')
    try:
        return data.decode('utf-8-sig')
    except UnicodeDecodeError:
        return data.decode('cp1252', errors='replace')


# ---------------------------------------------------------------------
# The HTML page: GitHub-like style, theme switch and auto-reload
# ---------------------------------------------------------------------

CSS = """
:root {
  --fg: #1f2328; --muted: #59636e; --bg: #ffffff; --link: #0969da;
  --border: #d1d9e0; --border-muted: #d1d9e0b3;
  --code-bg: #818b981f; --pre-bg: #f6f8fa; --row-alt: #f6f8fa;
  --note: #0969da; --tip: #1a7f37; --important: #8250df;
  --warning: #9a6700; --caution: #d1242f;
}
:root[data-theme=dark] {
  --fg: #f0f6fc; --muted: #9198a1; --bg: #0d1117; --link: #4493f8;
  --border: #3d444d; --border-muted: #3d444db3;
  --code-bg: #656c7633; --pre-bg: #151b23; --row-alt: #151b23;
  --note: #4493f8; --tip: #3fb950; --important: #ab7df8;
  --warning: #d29922; --caution: #f85149;
}
html { background: var(--bg); }
body {
  margin: 0; color: var(--fg); background: var(--bg);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
    "Noto Sans", Helvetica, Arial, sans-serif;
  font-size: 16px; line-height: 1.5; word-wrap: break-word;
}
.crumbs {
  max-width: 980px; margin: 0 auto; padding: 12px 45px 0;
  box-sizing: border-box; font-size: 14px; color: var(--muted);
}
.markdown-body {
  max-width: 980px; margin: 0 auto; padding: 24px 45px 45px;
  box-sizing: border-box;
}
@media (max-width: 767px) {
  .markdown-body, .crumbs { padding-left: 15px; padding-right: 15px; }
}
.markdown-body > *:first-child { margin-top: 0 !important; }
a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }
p, blockquote, ul, ol, dl, table, pre, details,
.markdown-alert { margin-top: 0; margin-bottom: 16px; }
h1, h2, h3, h4, h5, h6 {
  margin-top: 24px; margin-bottom: 16px; font-weight: 600;
  line-height: 1.25; position: relative;
}
h1 { font-size: 2em; }
h2 { font-size: 1.5em; }
h1, h2 {
  padding-bottom: .3em; border-bottom: 1px solid var(--border-muted);
}
h3 { font-size: 1.25em; }
h4 { font-size: 1em; }
h5 { font-size: .875em; }
h6 { font-size: .85em; color: var(--muted); }
.anchor {
  position: absolute; left: -1.1em; padding-right: .3em;
  color: var(--muted); visibility: hidden; text-decoration: none;
}
.anchor::before { content: "#"; }
h1:hover .anchor, h2:hover .anchor, h3:hover .anchor,
h4:hover .anchor, h5:hover .anchor, h6:hover .anchor {
  visibility: visible; text-decoration: none;
}
strong, b { font-weight: 600; }
code, kbd, pre, tt {
  font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo,
    Consolas, "Liberation Mono", monospace;
}
code {
  padding: .2em .4em; margin: 0; font-size: 85%;
  white-space: break-spaces; background: var(--code-bg);
  border-radius: 6px;
}
pre {
  padding: 16px; overflow: auto; font-size: 85%; line-height: 1.45;
  background: var(--pre-bg); border-radius: 6px;
}
pre code {
  padding: 0; font-size: 100%; white-space: pre;
  background: transparent; border: 0;
}
blockquote {
  margin-left: 0; margin-right: 0; padding: 0 1em;
  color: var(--muted); border-left: .25em solid var(--border);
}
blockquote > :first-child { margin-top: 0; }
blockquote > :last-child { margin-bottom: 0; }
ul, ol { padding-left: 2em; }
ul ul, ul ol, ol ol, ol ul { margin-top: 0; margin-bottom: 0; }
li + li { margin-top: .25em; }
li > p { margin-top: 16px; }
li > p:first-child { margin-top: 0; }
.task-list-item { list-style-type: none; }
.task-list-item input { margin: 0 .2em .25em -1.4em;
  vertical-align: middle; }
table {
  border-spacing: 0; border-collapse: collapse; display: block;
  width: max-content; max-width: 100%; overflow: auto;
}
th { font-weight: 600; }
th, td { padding: 6px 13px; border: 1px solid var(--border); }
tr { background: var(--bg); border-top: 1px solid var(--border-muted); }
tr:nth-child(2n) { background: var(--row-alt); }
hr {
  height: .25em; padding: 0; margin: 24px 0;
  background: var(--border); border: 0;
}
img { max-width: 100%; box-sizing: content-box; }
mjx-container[display="true"] { overflow-x: auto; overflow-y: hidden; }
.markdown-alert {
  padding: .5rem 1rem; color: inherit;
  border-left: .25em solid var(--border);
}
.markdown-alert > :last-child { margin-bottom: 0; }
.markdown-alert-title { font-weight: 500; margin-bottom: 8px; }
.markdown-alert-note { border-left-color: var(--note); }
.markdown-alert-note .markdown-alert-title { color: var(--note); }
.markdown-alert-tip { border-left-color: var(--tip); }
.markdown-alert-tip .markdown-alert-title { color: var(--tip); }
.markdown-alert-important { border-left-color: var(--important); }
.markdown-alert-important .markdown-alert-title
  { color: var(--important); }
.markdown-alert-warning { border-left-color: var(--warning); }
.markdown-alert-warning .markdown-alert-title
  { color: var(--warning); }
.markdown-alert-caution { border-left-color: var(--caution); }
.markdown-alert-caution .markdown-alert-title
  { color: var(--caution); }
.listing { list-style: none; padding-left: 0; }
@media print {
  .crumbs { display: none; }
  .markdown-body { max-width: none; padding: 0; }
  pre, blockquote, table, img { break-inside: avoid; }
}
"""

JS = """
(function () {
  var root = document.documentElement;
  function apply(theme) { root.setAttribute('data-theme', theme); }
  var saved = null;
  try { saved = localStorage.getItem('mdv-theme'); } catch (e) {}
  apply(saved || (matchMedia('(prefers-color-scheme: dark)').matches
                  ? 'dark' : 'light'));
  document.addEventListener('keydown', function (e) {
    if (e.key !== 't' || e.ctrlKey || e.metaKey || e.altKey) return;
    var t = root.getAttribute('data-theme') === 'dark' ? 'light'
                                                         : 'dark';
    apply(t);
    try { localStorage.setItem('mdv-theme', t); } catch (e2) {}
  });

  var src = {{SRC}}, stamp = {{STAMP}};
  if (src === null) return;
  var key = 'mdv-scroll:' + src;
  var y = sessionStorage.getItem(key);
  if (y !== null) {
    sessionStorage.removeItem(key);
    addEventListener('load', function () {
      // Formulas change the page's height: wait for MathJax first.
      var mj = window.MathJax && MathJax.startup
               && MathJax.startup.promise;
      Promise.resolve(mj).then(function () { scrollTo(0, +y); });
    });
  }
  function poll() {
    fetch('/__stamp?p=' + encodeURIComponent(src), {cache: 'no-store'})
      .then(function (r) { return r.text(); })
      .then(function (t) {
        if (t !== 'missing' && t !== stamp) {
          sessionStorage.setItem(key, String(scrollY));
          location.reload();
        } else {
          setTimeout(poll, 700);
        }
      })
      .catch(function () { setTimeout(poll, 2000); });
  }
  setTimeout(poll, 700);
})();
"""

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{{BASE}}<title>{{TITLE}}</title>
<style>{{CSS}}</style>
<script>{{JS}}</script>
{{MATHJAX}}</head>
<body>
{{CRUMBS}}<article class="markdown-body tex2jax_ignore">
{{BODY}}
</article>
</body>
</html>
"""


# MathJax (from a CDN, so formulas need an internet connection) only
# typesets the elements marked tex2jax_process, so a $ elsewhere in
# the text stays a dollar.
MATHJAX = r"""<script>
window.MathJax = {
  tex: {inlineMath: [['\\(', '\\)']], displayMath: [['\\[', '\\]']]},
  options: {ignoreHtmlClass: 'tex2jax_ignore',
            processHtmlClass: 'tex2jax_process'}
};
</script>
<script defer
  src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js">
</script>
"""


def js_value(value):
    return json.dumps(value).replace('</', '<\\/')


def page(title, body, crumbs='', src=None, stamp=None, base=None):
    js = (JS.replace('{{SRC}}', js_value(src))
            .replace('{{STAMP}}', js_value(stamp)))
    base_tag = '<base href="%s">\n' % esc_attr(base) if base else ''
    crumbs_div = '<div class="crumbs">%s</div>\n' % crumbs if crumbs else ''
    mathjax = MATHJAX if 'tex2jax_process' in body else ''
    return (PAGE.replace('{{TITLE}}', esc(title))
                .replace('{{BASE}}', base_tag)
                .replace('{{MATHJAX}}', mathjax)
                .replace('{{CSS}}', CSS)
                .replace('{{JS}}', js)
                .replace('{{CRUMBS}}', crumbs_div)
                .replace('{{BODY}}', body))


def title_of(body, default):
    m = re.search(r'<h1[^>]*>(.*?)</h1>', body, re.S)
    return strip_tags(m.group(1)).strip() if m else default


# ---------------------------------------------------------------------
# The local web server
# ---------------------------------------------------------------------

def file_stamp(path):
    try:
        return str(path.stat().st_mtime_ns)
    except OSError:
        return 'missing'


def find_root(path):
    """The git repository containing path, else its directory."""
    start = path if path.is_dir() else path.parent
    for d in [start] + list(start.parents):
        if (d / '.git').exists():
            return d
    return start


class Server(http.server.ThreadingHTTPServer):
    # On Windows, address reuse would let two servers share a port.
    allow_reuse_address = os.name != 'nt'


class Viewer(http.server.BaseHTTPRequestHandler):
    root = Path('.')

    def log_message(self, *args):
        pass  # keep the console quiet

    def resolve(self, rel):
        target = (self.root / rel.lstrip('/')).resolve()
        if target == self.root or target.is_relative_to(self.root):
            return target
        return None

    def send(self, code, data, ctype):
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError):
            pass

    def send_page(self, code, text):
        self.send(code, text.encode('utf-8'), 'text/html; charset=utf-8')

    def crumbs(self, target):
        rel = target.relative_to(self.root).parts
        links = ['<a href="/">%s</a>' % esc(self.root.name or '/')]
        for k, part in enumerate(rel):
            href = '/' + '/'.join(urllib.parse.quote(p)
                                  for p in rel[:k + 1])
            if k < len(rel) - 1 or target.is_dir():
                links.append('<a href="%s/">%s</a>' % (href, esc(part)))
            else:
                links.append(esc(part))
        return ' / '.join(links)

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        rel = urllib.parse.unquote(url.path)
        if rel == '/__stamp':
            query = urllib.parse.parse_qs(url.query)
            target = self.resolve(query.get('p', [''])[0])
            stamp = file_stamp(target) if target else 'missing'
            self.send(200, stamp.encode(), 'text/plain')
            return
        target = self.resolve(rel)
        if target is None or not target.exists():
            self.send_page(404, page('Not found',
                                     '<h1>Not found</h1>\n<p>%s</p>'
                                     % esc(rel)))
            return
        if target.is_dir():
            if not url.path.endswith('/'):
                self.send_response(301)
                self.send_header('Location', url.path + '/')
                self.end_headers()
                return
            self.send_page(200, self.directory(target))
        elif target.suffix.lower() in MD_SUFFIXES:
            self.send_page(200, self.markdown(target))
        else:
            ctype = mimetypes.guess_type(target.name)[0]
            self.send(200, target.read_bytes(),
                      ctype or 'application/octet-stream')

    def markdown(self, target):
        stamp = file_stamp(target)
        body = markdown_to_html(read_text(target))
        src = '/' + target.relative_to(self.root).as_posix()
        return page(title_of(body, target.name), body,
                    self.crumbs(target), src, stamp)

    def directory(self, target):
        entries = sorted(target.iterdir(),
                         key=lambda p: (not p.is_dir(), p.name.lower()))
        items = []
        for p in entries:
            if p.name.startswith('.'):
                continue
            if p.is_dir():
                items.append('<li>&#128193; <a href="%s/">%s/</a></li>'
                             % (urllib.parse.quote(p.name), esc(p.name)))
            elif p.suffix.lower() in MD_SUFFIXES:
                items.append('<li>&#128196; <a href="%s">%s</a></li>'
                             % (urllib.parse.quote(p.name), esc(p.name)))
        body = '<ul class="listing">\n%s\n</ul>' % '\n'.join(items)
        readme = next((p for p in entries
                       if p.name.lower() == 'readme.md'), None)
        if readme:
            body += '\n<hr>\n' + markdown_to_html(read_text(readme))
        name = target.name or str(target)
        return page(name, body, self.crumbs(target))


# ---------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description='View Markdown files in the browser, '
                    'styled like GitHub.')
    ap.add_argument('path', nargs='?', default='.',
                    help='a Markdown file or a directory (default: .)')
    ap.add_argument('-o', '--output', metavar='HTML',
                    help='write a static HTML file instead of serving')
    ap.add_argument('-p', '--port', type=int, default=8765,
                    help='port of the local server (default: 8765)')
    ap.add_argument('-r', '--root', metavar='DIR',
                    help='directory to serve (default: the git '
                         "repository containing PATH, else PATH's "
                         'directory)')
    ap.add_argument('-n', '--no-browser', action='store_true',
                    help="don't open the browser")
    args = ap.parse_args()

    path = Path(args.path).resolve()
    if not path.exists():
        sys.exit('md_viewer: %s does not exist' % args.path)

    if args.output:
        if path.is_dir():
            sys.exit('md_viewer: -o needs a Markdown file')
        body = markdown_to_html(read_text(path))
        text = page(title_of(body, path.name), body,
                    base=path.parent.as_uri() + '/')
        Path(args.output).write_text(text, encoding='utf-8')
        print('Wrote', args.output)
        return

    root = Path(args.root).resolve() if args.root else find_root(path)
    if path != root and not path.is_relative_to(root):
        sys.exit('md_viewer: %s is outside %s' % (path, root))
    Viewer.root = root
    try:
        server = Server(('127.0.0.1', args.port), Viewer)
    except OSError:
        server = Server(('127.0.0.1', 0), Viewer)  # any free port

    rel = path.relative_to(root).as_posix()
    if path.is_dir():
        rel = '' if rel == '.' else rel + '/'
    url = 'http://127.0.0.1:%d/%s' % (server.server_port,
                                      urllib.parse.quote(rel))
    print('Serving %s\nat %s\nPress Ctrl+C to stop.' % (root, url),
          flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
