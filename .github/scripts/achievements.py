#!/usr/bin/env python3
"""Generate a GitHub achievements card as a standalone SVG.

Data comes from the GitHub REST + GraphQL APIs only, so it has no external
dependencies and no dependency on third-party badge services staying online.

Every achievement is scored on a S/A/B/C scale; anything below the lowest
cut-off is reported as X (locked). Locked achievements are dimmed so the card
reads as progress rather than a wall of completed work.
"""

import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"

TIERS = [
    ("S", 100, "#EB355E", "#731237"),
    ("A", 50, "#B59151", "#FFD576"),
    ("B", 20, "#7D6CFF", "#B2A8FF"),
    ("C", 10, "#2088FF", "#79B8FF"),
    ("X", 0, "#4A4A4A", "#2A2A2A"),
]

BG = "#0d1117"
CARD = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"

CARD_W, CARD_H = 268, 74
COLS = 3
PAD = 16


def _open():
    ctx = ssl.create_default_context()
    if os.environ.get("SKIP_TLS_VERIFY") == "1":
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def get(url, token, accept="application/vnd.github+json"):
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "profile-achievements")
    with urllib.request.urlopen(req, timeout=60, context=_open()) as resp:
        return json.loads(resp.read().decode("utf-8"))


def graphql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(GRAPHQL, data=body, method="POST")
    req.add_header("Authorization", f"bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "profile-achievements")
    with urllib.request.urlopen(req, timeout=60, context=_open()) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError(json.dumps(payload["errors"])[:400])
    return payload["data"]


def search_count(token, query):
    """Total hits for a search query, using the first page's total_count."""
    url = f"{API}/search/issues?q={urllib.parse.quote(query)}&per_page=1"
    return get(url, token, "application/vnd.github.text-match+json")["total_count"]


def collect(login, token):
    user = get(f"{API}/users/{login}", token)

    repos = []
    stars = forks = 0
    langs = set()
    topics = set()
    cursor = None
    query = """
    query($login: String!, $cursor: String) {
      user(login: $login) {
        repositories(first: 100, after: $cursor, ownerAffiliations: OWNER,
                     orderBy: {field: STARGAZERS, direction: DESC}) {
          pageInfo { hasNextPage endCursor }
          nodes {
            stargazerCount
            forkCount
            isFork
            isArchived
            createdAt
            primaryLanguage { name }
            repositoryTopics(first: 10) { nodes { topic { name } } }
          }
        }
        gists { totalCount }
      }
    }
    """
    gist_count = 0
    while True:
        payload = graphql(query, {"login": login, "cursor": cursor}, token)["user"]
        gist_count = (payload.get("gists") or {}).get("totalCount", 0)
        data = payload["repositories"]
        repos.extend(data["nodes"])
        if not data["pageInfo"]["hasNextPage"]:
            break
        cursor = data["pageInfo"]["endCursor"]
        if len(repos) >= 500:
            break

    for r in repos:
        stars += r["stargazerCount"]
        forks += r["forkCount"]
        if not r["isFork"] and not r["isArchived"]:
            if r.get("primaryLanguage"):
                langs.add(r["primaryLanguage"]["name"])
            for t in r.get("repositoryTopics", {}).get("nodes", []):
                topics.add(t["topic"]["name"])

    created = datetime.strptime(user["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )
    age_years = round((datetime.now(timezone.utc) - created).days / 365.25, 1)
    this_year = datetime.now(timezone.utc).year
    new_this_year = sum(
        1
        for r in repos
        if not r["isFork"] and r["createdAt"].startswith(str(this_year))
    )

    author = f"author:{login}"

    def safe(fn, default=0):
        """Optional stats must never break the whole card.

        GITHUB_TOKEN is scope-limited, so endpoints such as gists or some search
        qualifiers can return 403 in Actions even though they work for a PAT.
        A missing stat degrades that one achievement instead of the card.
        """
        try:
            return fn()
        except Exception:  # noqa: BLE001 - deliberately broad, see docstring
            return default

    merged = safe(lambda: search_count(token, f"is:pr is:merged {author}"))
    prs = safe(lambda: search_count(token, f"is:pr {author}"))
    prs_external = safe(lambda: search_count(token, f"is:pr {author} -user:{login}"))
    issues = safe(lambda: search_count(token, f"is:issue {author}"))
    issues_closed = safe(lambda: search_count(token, f"is:issue is:closed {author}"))
    reviews = safe(lambda: search_count(token, f"is:pr reviewed-by:{login} -author:{login}"))

    return {
        "repos": user["public_repos"],
        "own_repos": len([r for r in repos if not r["isFork"]]),
        "forks_own": len([r for r in repos if r["isFork"]]),
        "stars": stars,
        "forks": forks,
        "followers": user["followers"],
        "following": user["following"],
        "langs": len(langs),
        "topics": len(topics),
        "age_years": age_years,
        "new_this_year": new_this_year,
        "merged": merged,
        "prs": prs,
        "prs_external": prs_external,
        "issues": issues,
        "issues_closed": issues_closed,
        "reviews": reviews,
        "gists": gist_count,
    }


def tier(value, cuts):
    """cuts is a 3-tuple of descending thresholds for S, A, B."""
    for name, cut, _, _ in [TIERS[i] for i in range(3)]:
        if value >= cuts[["S", "A", "B"].index(name)]:
            return name
    return "X" if value > 0 else "X"


def build(login, d):
    def a(title, value, cuts, note=""):
        return {"title": title, "value": value, "rank": tier(value, cuts), "note": note}

    items = [
        a("Developer", d["own_repos"], (100, 40, 15), "public repos"),
        a("Stargazer", d["stars"], (1000, 200, 25), "stars earned"),
        a("Forker", d["forks"], (100, 25, 5), "forks received"),
        a("Influencer", d["followers"], (500, 100, 25), "followers"),
        a("Octonaut", d["issues"], (100, 40, 10), "issues opened"),
        a("Issue Solver", d["issues_closed"], (100, 40, 10), "issues closed"),
        a("Pull Sharer", d["prs"], (200, 60, 15), "PRs opened"),
        a("Merger", d["merged"], (100, 30, 8), "PRs merged"),
        a("Contributor", d["prs_external"], (40, 15, 4), "PRs to other repos"),
        a("Reviewer", d["reviews"], (60, 20, 5), "PRs reviewed"),
        a("Polyglot", d["langs"], (15, 8, 4), "languages used"),
        a("Trendsetter", d["topics"], (60, 25, 8), "repo topics"),
        a("Explorer", d["new_this_year"], (25, 10, 3), f"repos in {datetime.now(timezone.utc).year}"),
        a("Veteran", d["age_years"], (4, 3, 2), "account age (years)"),
        a("Gister", d["gists"], (20, 8, 2), "public gists"),
    ]

    unlocked = sum(1 for i in items if i["rank"] != "X")
    rows = (len(items) + COLS - 1) // COLS
    w = PAD * 2 + COLS * CARD_W + (COLS - 1) * PAD
    head_h = 74
    h = head_h + rows * (CARD_H + PAD) + PAD + 34

    o = []
    add = o.append
    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="GitHub achievements for {login}">'
    )
    add(
        "<defs><linearGradient id='hd' x1='0' y1='0' x2='1' y2='0'>"
        "<stop offset='0%' stop-color='#161b22'/><stop offset='100%' stop-color='#0d1117'/>"
        "</linearGradient></defs>"
    )
    add(f'<rect width="{w}" height="{h}" rx="14" fill="{BG}"/>')
    add(
        f'<rect x="1" y="1" width="{w-2}" height="{h-2}" rx="14" fill="none" '
        f'stroke="{BORDER}"/>'
    )
    add(f'<rect x="1" y="1" width="{w-2}" height="{head_h-10}" rx="13" fill="url(#hd)"/>')

    add(
        f'<text x="{PAD+8}" y="34" font-family="Segoe UI,DejaVu Sans,sans-serif" '
        f'font-size="19" font-weight="700" fill="{TEXT}">🏆 Achievements</text>'
    )
    add(
        f'<text x="{PAD+8}" y="55" font-family="Segoe UI,DejaVu Sans,sans-serif" '
        f'font-size="12" fill="{MUTED}">{unlocked}/{len(items)} unlocked · '
        f'{d["stars"]} stars · {d["followers"]} followers · {d["own_repos"]} repos</text>'
    )

    top_tier = sum(1 for i in items if i["rank"] in ("S", "A", "B"))
    add(
        f'<text x="{w-PAD-8}" y="34" text-anchor="end" '
        f'font-family="Segoe UI,DejaVu Sans,sans-serif" font-size="13" font-weight="700" '
        f'fill="#FFD576">{top_tier} at tier B+</text>'
    )
    add(
        f'<text x="{w-PAD-8}" y="54" text-anchor="end" '
        f'font-family="Segoe UI,DejaVu Sans,sans-serif" font-size="12" '
        f'fill="{MUTED}">{unlocked}/{len(items)} unlocked</text>'
    )

    for idx, it in enumerate(items):
        r, c = divmod(idx, COLS)
        x = PAD + c * (CARD_W + PAD)
        y = head_h + r * (CARD_H + PAD)
        rank = next(t for t in TIERS if t[0] == it["rank"])
        _, _, fg, deep = rank
        locked = it["rank"] == "X"
        op = "0.45" if locked else "1"
        add(f'<g opacity="{op}">')
        add(
            f'<rect x="{x}" y="{y}" width="{CARD_W}" height="{CARD_H}" rx="10" '
            f'fill="{CARD}" stroke="{BORDER}"/>'
        )
        add(
            f'<rect x="{x}" y="{y}" width="4" height="{CARD_H}" rx="2" fill="{fg}"/>'
        )
        add(
            f'<text x="{x+16}" y="{y+30}" font-family="Segoe UI,DejaVu Sans,sans-serif" '
            f'font-size="15" font-weight="700" fill="{TEXT}">{it["title"]}</text>'
        )
        add(
            f'<text x="{x+16}" y="{y+50}" font-family="Segoe UI,DejaVu Sans,sans-serif" '
            f'font-size="12" fill="{MUTED}">{it["value"]:,} · {it["note"]}</text>'
        )
        pill = f"{it['rank']}"
        pw = 30
        add(
            f'<rect x="{x+CARD_W-pw-12}" y="{y+14}" width="{pw}" height="24" rx="12" '
            f'fill="{deep}" stroke="{fg}"/>'
        )
        add(
            f'<text x="{x+CARD_W-pw//2-12}" y="{y+31}" text-anchor="middle" '
            f'font-family="Segoe UI,DejaVu Sans,sans-serif" font-size="13" '
            f'font-weight="800" fill="{fg}">{pill}</text>'
        )
        add("</g>")

    ly = head_h + rows * (CARD_H + PAD) + 4
    add(
        f'<text x="{PAD+8}" y="{ly+20}" font-family="Segoe UI,DejaVu Sans,sans-serif" '
        f'font-size="11" fill="{MUTED}">'
        "S Master · A Super · B Great · C Good · X locked &#160;|&#160; "
        f'generated {datetime.now(timezone.utc):%Y-%m-%d}</text>'
    )
    add("</svg>")
    return "\n".join(o)


def main():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("METRICS_TOKEN")
    login = os.environ.get("GITHUB_USER") or os.environ.get("GITHUB_REPOSITORY_OWNER")
    out = os.environ.get("OUTPUT", "metrics.achievements.svg")
    if not token:
        sys.exit("GITHUB_TOKEN is required")
    if not login:
        sys.exit("GITHUB_USER is required")
    data = collect(login, token)
    svg = build(login, data)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"wrote {out} ({len(svg)} bytes)")
    print(json.dumps(data, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
