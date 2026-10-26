#!/usr/bin/env python3
"""Build and check the portfolio from content/portfolio.toml. Standard library only.

  python3 scripts/portfolio.py build             write README.md, content/portfolio.json and
                                                 assets/banner-{light,dark}.svg
  python3 scripts/portfolio.py check [--online]  schema, lint, number rule and drift; --online
                                                 adds claims at their ref, repo descriptions,
                                                 HEAD READMEs and links, for shown projects
  python3 scripts/portfolio.py selftest          asserts on the rules themselves

Private terms come from the PRIVATE_TERMS environment variable (a CI secret, one term per line)
or a gitignored .private-terms file. They are reported by number, never by text.
"""

import html
import http.client
import json
import os
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOML = ROOT / "content" / "portfolio.toml"
README, JSON = ROOT / "README.md", ROOT / "content" / "portfolio.json"
BANNERS = {
    "light": ROOT / "assets" / "banner-light.svg",
    "dark": ROOT / "assets" / "banner-dark.svg",
}

GROUPS = ("flagship", "software", "coursework", "open-source")
REF = re.compile(r"v\d+\.\d+\.\d+|[0-9a-f]{40}")
NUM = re.compile(r"(?<![\w.])\d[\d,.]*%?")  # 'v1.1.0', 'H1' and 'COMPX525' are not numbers
# Figure, en and em dashes, horizontal bar, two and three em dashes, and their HTML entities;
# built from code points so this file passes its own check.
DASH = re.compile(
    f"[{chr(0x2012)}-{chr(0x2015)}{chr(0x2E3A)}{chr(0x2E3B)}]|&[mn]dash;|&#821[12];|&#x201[34];",
    re.IGNORECASE,
)
SPACED_HYPHEN = re.compile(r"(?<=\S) - (?=\S)")
BANNED = (
    "production", "injection-proof", "robust", "deployed on aws", "runs on rds", "saves 58%",
    "worse model", "graduated", "seasonal drift", "zero context loss", "passionate", "expert",
    "cutting-edge", "state-of-the-art", "seamless", "world-class", "powerful",
)  # fmt: skip
PII = (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), re.compile(r"\+?\d[\d ()]{8,}\d"))
URL = re.compile(r"https?://[^\s)\"'<>]+")
NOT_PII = re.compile(URL.pattern + r"|\b[0-9a-f]{40}\b")  # links and commit SHAs
SEP = " · "
COURSEWORK = "Coursework, University of Waikato"
HEADINGS = {
    "software": "Software",
    "coursework": "Machine learning coursework, University of Waikato",
    "open-source": "Open source",
}
COURSEWORK_NOTE = (
    "Individual work unless stated. Course data and trained weights are not published."
)

# Hand-made 5x7 glyphs, rows top to bottom, '#' lit.
GLYPHS = {
    "A": ".###. #...# #...# ##### #...# #...# #...#",
    "C": ".###. #...# #.... #.... #.... #...# .###.",
    "E": "##### #.... #.... ####. #.... #.... #####",
    "G": ".###. #...# #.... #.### #...# #...# .###.",
    "I": ".###. ..#.. ..#.. ..#.. ..#.. ..#.. .###.",
    "K": "#...# #..#. #.#.. ##... #.#.. #..#. #...#",
    "L": "#.... #.... #.... #.... #.... #.... #####",
    "M": "#...# ##.## #.#.# #.#.# #...# #...# #...#",
    "N": "#...# #...# ##..# #.#.# #..## #...# #...#",
    "O": ".###. #...# #...# #...# #...# #...# .###.",
    "P": "####. #...# #...# ####. #.... #.... #....",
    "R": "####. #...# #...# ####. #.#.. #..#. #...#",
    "U": "#...# #...# #...# #...# #...# #...# .###.",
}
PALETTES = {
    "light": {"ink": "#1F2328", "muted": "#8C959F", "accent": "#C2410C", "on_accent": "#FFFFFF"},
    "dark": {"ink": "#E6EDF3", "muted": "#7D8590", "accent": "#FB923C", "on_accent": "#0D1117"},
}


# ---------- rules ----------


def norm(s: str) -> str:
    """Verbatim modulo markdown emphasis, code ticks, curly quotes and whitespace."""
    s = s.replace("**", "").replace("`", "")
    s = s.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"'})
    return " ".join(s.split())


def numbers(s: str) -> set[str]:
    return {m.group().rstrip(".,") for m in NUM.finditer(s)}


def phrase(p: str) -> re.Pattern:
    return re.compile(rf"(?<!\w){re.escape(p)}(?!\w)", re.IGNORECASE)


def lint(text, where, banned=BANNED, pii=True, spaced=False, terms=(), skip=None):
    """Problems in text as 'where:line: message'. Private terms are named only by number."""
    found = []
    patterns = [(p, phrase(p)) for p in banned]
    privates = [(n, phrase(t)) for n, t in enumerate(terms, 1)]
    for n, line in enumerate(text.splitlines(), 1):
        if skip and skip.search(line):
            continue
        say = lambda msg, n=n: found.append(f"{where}:{n}: {msg}")
        if DASH.search(line):
            say("em or en dash")
        if spaced and SPACED_HYPHEN.search(line):
            say("spaced hyphen used as a dash")
        for p in (p for p, rx in patterns if rx.search(line)):
            say(f"banned phrase '{p}'")
        for number, rx in privates:
            if rx.search(line):
                say(f"private term #{number}")
        if pii and any(rx.search(NOT_PII.sub("", line)) for rx in PII):
            say("looks like an email address or phone number")
    return found


def private_terms() -> list[str]:
    raw = os.environ.get("PRIVATE_TERMS") or ""
    local = ROOT / ".private-terms"
    if not raw.strip() and local.exists():
        raw = local.read_text(encoding="utf-8")
    terms = [t.strip() for t in raw.splitlines() if t.strip() and not t.startswith("#")]
    if not terms:
        if os.environ.get("CI"):
            sys.exit("PRIVATE_TERMS is empty: set the secret (one term per line)")
        print("note: no private terms loaded (.private-terms or PRIVATE_TERMS); not checked")
    return terms


# ---------- content ----------


def load() -> dict:
    return tomllib.loads(TOML.read_text(encoding="utf-8"))


def shown(data: dict) -> list[dict]:
    return [p for p in data["project"] if p["status"] == "show"]


def claims(p: dict) -> list[dict]:
    return p.get("claim", [])


def validate(data: dict) -> list[str]:
    errors = []
    if data.get("schema") != 1:
        errors.append("schema must be 1")
    need = ("name", "headline", "education", "location", "availability", "provenance")
    errors += [f"profile.{k} missing" for k in need + ("banner_alt",) if k not in data["profile"]]
    for x in data.get("link", []):
        if "icon" in x and not (README.parent / x["icon"]).is_file():
            errors.append(f"link {x['label']}: icon {x['icon']} not found")
    ids = [p.get("id") for p in data["project"]]
    errors += [f"duplicate id {i}" for i in sorted({i for i in ids if ids.count(i) > 1})]
    for p in data["project"]:
        pid = p.get("id", "?")
        bad = lambda msg, pid=pid: errors.append(f"{pid}: {msg}")
        for key in ("id", "title", "repo", "group", "status", "short", "card", "stack"):
            if key not in p:
                bad(f"{key} missing")
        if p.get("group") not in GROUPS:
            bad(f"group must be one of {GROUPS}")
        if p.get("status") not in ("show", "hold"):
            bad("status must be show or hold")
        if len(p.get("short", "")) > 60:
            bad("short is over 60 characters")
        if len(p.get("pitch", "")) > 160:
            bad("pitch is over 160 characters")
        if (p.get("group") == "flagship") != ("label" in p):
            bad("a label is required on flagships and only there")
        label = p.get("label", "")
        if len(label) > 6 or any(c not in GLYPHS for c in label):
            bad("label must be at most 6 characters from the banner glyphs")
        if p.get("group") == "coursework" and "course" not in p:
            bad("course missing")
        for c in claims(p):
            if any(ch.isdigit() for ch in c.get("lead", "")):
                bad("a claim lead must not hold numbers; leads are not verified")
        missing = set()
        for value in [p.get(k, "") for k in ("short", "pitch", "card")]:
            missing |= numbers(value)
        missing -= set().union(*(numbers(c["text"]) for c in claims(p)))
        if missing:
            bad(f"numbers not found in any of its claims: {sorted(missing)}")
        if p.get("status") != "show":
            continue
        refs = [p.get("ref")] if p.get("release") or claims(p) or p.get("results") else []
        refs += [c["ref"] for c in claims(p) if "ref" in c]
        refs += [p["visual"]["ref"]] if "ref" in p.get("visual", {}) else []
        visual = p.get("visual")
        if visual is not None:
            # A file in the project's own repo, pinned to a commit, described for screen readers.
            if not re.fullmatch(r"[\w./-]+\.(svg|png|gif)", visual.get("src", "")):
                bad("visual src must be a path to an .svg, .png or .gif file in the repo")
            if not re.fullmatch(r"[0-9a-f]{40}", visual.get("ref", "")):
                bad("visual ref must be a 40-hex commit SHA")
            if not visual.get("alt"):
                bad("visual alt missing")
            if len(visual.get("caption", "")) > 200:
                bad("visual caption is over 200 characters")
        for ref in refs:
            if not REF.fullmatch(ref or ""):
                bad(f"ref {ref!r} must be a tag vX.Y.Z or a 40-hex SHA")
    return errors


def at_ref(ref: str) -> str:
    return ref if ref.startswith("v") else f"commit {ref[:7]}"


def blob(repo: str, ref: str, path: str) -> str:
    return f"https://github.com/{repo}/blob/{ref}/{path}"


def raw(repo: str, ref: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"


def urls(p: dict) -> dict:
    repo, ref = p["repo"], p.get("ref")
    at = lambda path: path if "://" in path else blob(repo, ref, path)
    visual = p.get("visual")
    if visual:
        visual = (
            visual["src"]
            if "://" in visual["src"]
            else raw(repo, visual.get("ref", ref), visual["src"])
        )
    return {
        "repo": f"https://github.com/{repo}",
        "release": f"https://github.com/{repo}/releases/tag/{ref}" if p.get("release") else None,
        "results": at(p["results"]) if "results" in p else None,
        "visual": visual,
        "demo": p.get("demo"),
        "space": p.get("space"),
        "extra": [{"label": e["label"], "url": at(e["url"])} for e in p.get("extra", [])],
    }


def source(p: dict, c: dict) -> tuple[str, str]:
    repo, ref, path = (
        c.get("repo", p["repo"]),
        c.get("ref", p.get("ref")),
        c.get("path", "README.md"),
    )
    name = "README" if path == "README.md" else path
    return blob(repo, ref, path), f"source: {name} at {at_ref(ref)}"


def byline(p: dict) -> str:
    if p["group"] == "coursework":
        who = (
            f"group project of {p['team_size']}, my part: {p['role']}"
            if "team_size" in p
            else "individual"
        )
        return SEP.join([COURSEWORK, p["course"], who])
    if p["group"] == "open-source":
        return "Upstream contributions"
    parts = ["Personal project"] + ([p["ref"]] if p.get("release") else [])
    return SEP.join(parts + (["Synthetic data only"] if p.get("synthetic_data") else []))


def project_json(p: dict) -> dict:
    return {
        "id": p["id"],
        "title": p["title"],
        "group": p["group"],
        "label": p.get("label"),
        "ref": p.get("ref"),
        "short": p["short"],
        "pitch": p.get("pitch"),
        "card": p["card"],
        "stack": p["stack"],
        "byline": byline(p),
        "flags": {
            "synthetic_data": bool(p.get("synthetic_data")),
            "course": p.get("course"),
            "team_size": p.get("team_size"),
            "role": p.get("role"),
        },
        "urls": urls(p),
        "visual_alt": p.get("visual", {}).get("alt"),
        "visual_caption": p.get("visual", {}).get("caption"),
        "demo_note": p.get("demo_note"),
        "claims": [
            {"lead": c.get("lead"), "text": c["text"], "source": source(p, c)[0]} for c in claims(p)
        ],
    }


def portfolio_json(data: dict) -> str:
    doc = {
        "schema": data["schema"],
        "profile": data["profile"],
        "links": [
            {k: x[k] for k in ("label", "url", "icon") if k in x} for x in data["link"] if x["show"]
        ],
        "projects": [project_json(p) for p in shown(data)],
    }
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


# ---------- README ----------


def md(s: str) -> str:
    """Escape what GitHub markdown would otherwise read as markup (a single ~ strikes out).
    An underscore inside a word, as in run_python, is left alone."""
    return re.sub(r"[\\`*~\[\]<>]|(?<!\w)_|_(?!\w)", lambda m: "\\" + m.group(), s)


def attr(s: str) -> str:
    return html.escape(s, quote=False).replace('"', "&quot;")


def claim_line(p: dict, c: dict) -> str:
    href, text = source(p, c)
    lead = f"**{md(c['lead'])}** " if c.get("lead") else ""
    return f'{lead}{md(c["text"])} <sup><a href="{href}">{text}</a></sup>'


def link_row(p: dict) -> list[str]:
    u, ref = urls(p), p.get("ref")
    row = [f"[Release {ref}]({u['release']})"] if u["release"] else []
    if u["results"]:
        anchor = u["results"].partition("#")[2]
        name = anchor.replace("-", " ").capitalize() if anchor else "Results"
        row.append(f"[{name} at {at_ref(ref)}]({u['results']})")
    row += [f"[{md(e['label'])}]({e['url']})" for e in u["extra"]]
    if u["demo"]:
        row.append(f"[Demo]({u['demo']})" + (f" ({p['demo_note']})" if p.get("demo_note") else ""))
    return row


def flagship(p: dict) -> list[str]:
    u = urls(p)
    out = [
        f"### [{md(p['title'])}]({u['repo']})",
        f"<sub>{byline(p)}{SEP}{', '.join(p['stack'])}</sub>",
    ]
    out += ["", md(p["card"]), ""]
    out += [f"- {claim_line(p, c)}" for c in claims(p)[:3]]
    if u["visual"]:
        alt = attr(p["visual"]["alt"])
        out += ["", f'<a href="{u["repo"]}"><img src="{u["visual"]}" width="640" alt="{alt}"></a>']
    return out + ["", SEP.join(link_row(p)), ""]


def entry(p: dict) -> str:
    u = urls(p)
    sub = byline(p).removeprefix(COURSEWORK + SEP)
    body = " ".join([md(p["card"])] + [claim_line(p, c) for c in claims(p)[:1]])
    tail = "".join(SEP + x for x in link_row(p))
    return f"- **[{md(p['title'])}]({u['repo']})** <sub>{sub}{SEP}{', '.join(p['stack'])}</sub><br>{body}{tail}"


def render_readme(data: dict) -> str:
    prof, projects = data["profile"], shown(data)
    shown_links = [x for x in data["link"] if x["show"] and x["label"] != "GitHub"]
    links = " ".join(
        f'<a href="{attr(x["url"])}"><img src="{attr(x["icon"])}" height="32" alt="{attr(x["label"])}"></a>'
        if "icon" in x
        else f"[{x['label']}]({x['url']})"
        for x in shown_links
    )
    out = [
        (
            "<!-- Generated by scripts/portfolio.py from content/portfolio.toml."
            " Edit that file, then run: python3 scripts/portfolio.py build -->"
        ),
        "<picture>",
        '  <source media="(prefers-color-scheme: dark)" srcset="assets/banner-dark.svg">',
        '  <source media="(prefers-color-scheme: light)" srcset="assets/banner-light.svg">',
        f'  <img src="assets/banner-light.svg" width="1200" alt="{attr(prof["banner_alt"])}">',
        "</picture>",
        "",
        f"**{md(prof['name'])}.** {md(prof['headline'])}<br>",
        " ".join(md(prof[k]) for k in ("education", "location", "availability") if prof[k]),
        "",
        links,
        "",
        f"<sub>{md(prof['provenance'])}</sub>",
        "",
        "## Selected work",
        "",
    ]
    for p in projects:
        if p["group"] == "flagship":
            out += flagship(p)
    rest = [g for g in HEADINGS if any(p["group"] == g for p in projects)]
    if rest:
        out += ["## More work", ""]
    for g in rest:
        out += [f"### {HEADINGS[g]}"]
        out += [f"<sub>{COURSEWORK_NOTE}</sub>", ""] if g == "coursework" else []
        out += [entry(p) for p in projects if p["group"] == g] + [""]
    return "\n".join(out).rstrip("\n") + "\n"


# ---------- banner ----------


def dot_paths(text: str, x0: int, y0: int, cell: int, size: int) -> tuple[str, str]:
    """Path data for the lit and unlit dots of text in the 5x7 glyphs."""
    lit, unlit = [], []
    for i, ch in enumerate(text):
        if ch == " ":
            continue
        for r, row in enumerate(GLYPHS[ch].split()):
            for c, bit in enumerate(row):
                x, y = x0 + (i * 6 + c) * cell, y0 + r * cell
                (lit if bit == "#" else unlit).append(f"M{x} {y}h{size}v{size}h-{size}z")
    return "".join(lit), "".join(unlit)


def glass(kind: int, x: int, first: bool, pal: dict) -> str:
    """One piece of glassware in a 128-wide slot standing on the bench at y=172: a flask, a
    beaker or a rack of test tubes. The first holds the accent colour and gives off bubbles."""
    ink, muted = pal["ink"], pal["muted"]
    liquid = f'fill="{pal["accent"]}"' if first else f'fill="{muted}" fill-opacity="0.3"'
    line = f'fill="none" stroke="{ink}" stroke-width="3" stroke-linejoin="round"'
    if kind == 0:  # Erlenmeyer flask
        parts = [
            f'<path {liquid} d="M{x + 36} 126H{x + 92}L{x + 110} 166H{x + 18}z"/>',
            f'<path {line} d="M{x + 52} 44V84L{x + 14} 168Q{x + 14} 172 {x + 18} 172H{x + 110}'
            f'Q{x + 114} 172 {x + 114} 168L{x + 76} 84V44M{x + 46} 44H{x + 82}"/>',
        ]
    elif kind == 1:  # beaker with a spout and graduations
        parts = [
            f'<rect x="{x + 35}" y="112" width="58" height="57" {liquid}/>',
            f'<path {line} d="M{x + 24} 46L{x + 32} 54V164Q{x + 32} 172 {x + 40} 172H{x + 88}'
            f'Q{x + 96} 172 {x + 96} 164V52M{x + 92} 52H{x + 100}"/>',
            *(
                f'<path d="M{x + 32} {y}H{x + 44}" stroke="{muted}" stroke-width="2"/>'
                for y in (76, 96, 116, 136)
            ),
        ]
    else:  # three test tubes in a rack
        parts = []
        for i, top in enumerate((96, 120, 108)):
            tx = x + 30 + i * 26
            parts += [
                f'<path {liquid} d="M{tx + 3} {top}H{tx + 13}V{150}Q{tx + 13} 155 {tx + 8} 155'
                f'Q{tx + 3} 155 {tx + 3} 150z"/>',
                f'<path {line} d="M{tx} 64V150Q{tx} 158 {tx + 8} 158Q{tx + 16} 158 {tx + 16} 150V64"/>',
            ]
        parts.append(
            f'<path {line} d="M{x + 16} 124H{x + 112}M{x + 22} 124V172M{x + 106} 124V172"/>'
        )
    if first:  # pixel bubbles rising from the flask
        parts += [
            f'<rect x="{bx}" y="{by}" width="{w}" height="{w}" fill="{pal["accent"]}"/>'
            for bx, by, w in ((x + 61, 30, 6), (x + 70, 18, 4), (x + 58, 8, 4))
        ]
    return "".join(parts)


def plate(x: int, label: str, first: bool, pal: dict) -> str:
    """A sample label on the bench edge, in the 5x7 glyphs at 2 px a dot."""
    fill = f'fill="{pal["accent"]}"' if first else f'fill="{pal["muted"]}" fill-opacity="0.25"'
    width = (6 * len(label) - 1) * 2
    letters, _ = dot_paths(label, x + (128 - width) // 2, 188, 2, 2)
    return (
        f'<rect x="{x + 20}" y="180" width="88" height="30" rx="3" {fill}/>'
        f'<path fill="{pal["on_accent"] if first else pal["ink"]}" d="{letters}"/>'
    )


def banner(data: dict, theme: str) -> str:
    pal = PALETTES[theme]
    lit, unlit = dot_paths(data["profile"]["name"].upper(), 48, 85, 10, 8)
    labels = [p["label"] for p in data["project"] if p["group"] == "flagship"][:3]
    bench = "".join(
        glass(i, 760 + i * 144, i == 0, pal) + plate(760 + i * 144, label, i == 0, pal)
        for i, label in enumerate(labels)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="240" viewBox="0 0 1200 240"'
        f' role="img" aria-labelledby="title"><title id="title">{attr(data["profile"]["banner_alt"])}</title>'
        f'<path fill="{pal["muted"]}" fill-opacity="0.12" d="{unlit}"/>'
        f'<path fill="{pal["ink"]}" d="{lit}"/>{bench}'
        f'<path d="M752 173H1184" stroke="{pal["ink"]}" stroke-width="3"/></svg>\n'
    )


def outputs(data: dict) -> dict[Path, str]:
    built = {README: render_readme(data), JSON: portfolio_json(data)}
    return built | {path: banner(data, theme) for theme, path in BANNERS.items()}


# ---------- commands ----------


def repo_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    ).stdout.decode()
    paths = [ROOT / p for p in listed.split("\0") if p]
    return [p for p in paths if p.is_file() and b"\0" not in p.read_bytes()[:8192]]


def check_offline(data: dict, terms: list[str]) -> list[str]:
    errors = validate(data)
    built = outputs(data)
    errors += [
        f"{p.relative_to(ROOT)} is out of date; run: python3 scripts/portfolio.py build"
        for p, text in built.items()
        if not p.exists() or p.read_text(encoding="utf-8") != text
    ]
    copy = re.compile(r"^\s*banned\s*=")  # the per-project banned list is exempt
    errors += lint(
        TOML.read_text(encoding="utf-8"),
        "content/portfolio.toml",
        spaced=True,
        terms=terms,
        skip=copy,
    )
    for p in shown(data):
        extra = tuple(p.get("banned", ()))
        text = json.dumps(project_json(p), ensure_ascii=False, indent=1)
        errors += lint(text, f"portfolio.json[{p['id']}]", banned=BANNED + extra)
    errors += lint(built[README], "README.md", terms=terms)
    for path in repo_files():
        if path not in (TOML, README):
            rel = str(path.relative_to(ROOT))
            errors += lint(
                path.read_text(encoding="utf-8", errors="ignore"),
                rel,
                banned=(),
                pii=False,
                terms=terms,
            )
    return errors


def fetch(url: str) -> tuple[int | str, str]:
    headers = {"User-Agent": "Mozilla/5.0 (portfolio link check)"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    try:
        request = urllib.request.Request(url.partition("#")[0], headers=headers)
        with urllib.request.urlopen(request, timeout=90) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (OSError, http.client.HTTPException) as exc:  # timeouts and dropped connections
        return type(exc).__name__, ""


def check_online(data: dict, terms: list[str]) -> list[str]:
    projects = shown(data)
    wanted = {
        raw(c.get("repo", p["repo"]), c.get("ref", p.get("ref")), c.get("path", "README.md"))
        for p in projects
        for c in claims(p)
    }
    own = [p for p in projects if p["repo"].startswith("NecmiKural/")]
    wanted |= {f"https://api.github.com/repos/{p['repo']}" for p in own}
    wanted |= {raw(p["repo"], "HEAD", "README.md") for p in own}
    links = set(URL.findall(README.read_text(encoding="utf-8") + JSON.read_text(encoding="utf-8")))
    with ThreadPoolExecutor(8) as pool:
        got = dict(zip(sorted(wanted | links), pool.map(fetch, sorted(wanted | links))))
    errors = []
    for p in projects:
        for c in claims(p):
            url = raw(
                c.get("repo", p["repo"]), c.get("ref", p.get("ref")), c.get("path", "README.md")
            )
            if norm(c["text"]) not in norm(got[url][1]):
                errors.append(f"{p['id']}: claim not found verbatim at {url}: {c['text'][:70]}")
    for p in own:
        extra = tuple(p.get("banned", ()))
        status, body = got[f"https://api.github.com/repos/{p['repo']}"]
        description = (json.loads(body).get("description") or "") if status == 200 else ""
        if status != 200:
            errors.append(f"{p['repo']}: description not readable ({status})")
        errors += lint(
            description, f"{p['repo']} description", banned=BANNED + extra, pii=False, terms=terms
        )
        status, body = got[raw(p["repo"], "HEAD", "README.md")]
        if status != 200:
            errors.append(f"{p['repo']}: README at HEAD not readable ({status})")
        errors += lint(
            body, f"{p['repo']} README at HEAD", banned=BANNED + extra, pii=False, terms=terms
        )
    for url in sorted(links):
        status = got[url][0]
        if status != 200 and not (status == 999 and "linkedin.com" in url):
            errors.append(f"link {url} returned {status}")
    n = sum(len(claims(p)) for p in projects)
    print(f"online: {n} claims, {len(own)} repos (description and HEAD README), {len(links)} links")
    return errors


def selftest() -> None:
    assert md("run_python, _x_ ~66%") == "run_python, \\_x\\_ \\~66%"
    assert norm("**0 wrong**  `x` it’s “ok”") == '0 wrong x it\'s "ok"'
    assert numbers("v1.1.0, H1, COMPX525: 95% of 0.8940 in 2026.") == {"95%", "0.8940", "2026"}
    assert numbers("27 of 30 (90.0%)") == {"27", "30", "90.0%"}
    assert REF.fullmatch("v1.1.0") and REF.fullmatch("a" * 40) and not REF.fullmatch("main")
    for dash in (chr(0x2013), chr(0x2014), "&" + "mdash;", "&#" + "8211;"):
        assert lint(f"fine\nbad {dash} here", "f") == ["f:2: em or en dash"]
    assert lint("well-tested, a - b", "f") == []
    assert lint("well-tested, a - b", "f", spaced=True) == ["f:1: spaced hyphen used as a dash"]
    assert lint("Production checklist", "f") == ["f:1: banned phrase 'production'"]
    assert lint("production-ready", "f") and not lint("reproduction is fine", "f")
    assert lint("Mail me at a.b@example.org", "f") and lint("call +64 21 123 4567", "f")
    assert not lint("27 of 30, 582 decisions, https://x.org/a/1234567890123", "f")
    hit = lint("Our secret is Zorblat.", "f", terms=["nothing", "zorblat"])
    assert hit == ["f:1: private term #2"] and "orblat" not in hit[0].lower()
    assert all(len(g.split()) == 7 and all(len(r) == 5 for r in g.split()) for g in GLYPHS.values())
    project = {
        "id": "x", "title": "x", "repo": "o/x", "ref": "v1.0.0", "group": "software",
        "status": "show", "short": "s", "card": "27 of 30 held", "stack": [],
        "claim": [{"text": "27 of 30 (90.0%)"}],
    }  # fmt: skip
    data = {
        "schema": 1,
        "profile": dict.fromkeys(
            (
                "name",
                "headline",
                "education",
                "location",
                "availability",
                "provenance",
                "banner_alt",
            ),
            "x",
        ),
        "project": [project],
    }
    assert validate(data) == []
    assert validate(data | {"project": [project | {"card": "31 of 30"}]}) == [
        "x: numbers not found in any of its claims: ['31']"
    ]
    assert validate(data | {"project": [project | {"ref": "main"}]})[0].startswith("x: ref 'main'")
    real = load()
    assert outputs(real) == outputs(real), "build must be deterministic"
    assert all(len(banner(real, t).encode()) < 15_000 for t in PALETTES), "a banner is over 15 KB"
    print("selftest passed")


def main(argv: list[str]) -> None:
    command = argv[0] if argv else ""
    if command == "selftest":
        return selftest()
    if command == "build":
        data = load()
        if errors := validate(data):
            sys.exit("\n".join(errors))
        for path, text in outputs(data).items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
        return None
    if command != "check" or argv[1:] not in ([], ["--online"]):
        sys.exit(__doc__)
    data, terms = load(), private_terms()
    errors = check_offline(data, terms)
    if argv[1:] == ["--online"]:
        errors += check_online(data, terms)
    print("\n".join(errors) if errors else "check passed")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
