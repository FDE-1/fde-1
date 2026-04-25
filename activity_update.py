#!/usr/bin/env python3
"""
fetch_activity.py
Fetches the latest 5 GitHub activities for a user and updates their README.md
between <!--START_SECTION:activity--> and <!--END_SECTION:activity--> markers.
Also cache-busts all <source srcset="..."> URLs in <picture> blocks so GitHub's
CDN proxy is forced to re-fetch streak/LeetCode cards on every run.

Supported event types:
  PushEvent, PullRequestEvent, PullRequestReviewEvent,
  CreateEvent, DeleteEvent, IssuesEvent, IssueCommentEvent,
  ForkEvent, WatchEvent, ReleaseEvent, MemberEvent
"""

import os
import re
import sys
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
GITHUB_TOKEN    = os.environ.get("GH_TOKEN", "")
GITHUB_USERNAME = os.environ.get("GITHUB_USERNAME", "")
README_PATH     = os.environ.get("README_PATH", "README.md")
MAX_EVENTS      = int(os.environ.get("MAX_EVENTS", "5"))

START_MARKER = "<!--START_SECTION:activity-->"
END_MARKER   = "<!--END_SECTION:activity-->"

EMOJI = {
    "PushEvent":              "📦",
    "PullRequestEvent":       "🔀",
    "PullRequestReviewEvent": "👀",
    "IssuesEvent":            "🐛",
    "IssueCommentEvent":      "💬",
    "CreateEvent":            "🌱",
    "DeleteEvent":            "🗑️",
    "ForkEvent":              "🍴",
    "WatchEvent":             "⭐",
    "ReleaseEvent":           "🚀",
    "MemberEvent":            "🤝",
}

# ── GitHub API helpers ────────────────────────────────────────────────────────

def gh_get(url: str) -> dict | list:
    """Make an authenticated GET request to the GitHub API."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "github-activity-readme-bot")
    if GITHUB_TOKEN:
        req.add_header("Authorization", f"Bearer {GITHUB_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[ERROR] HTTP {e.code} fetching {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"[ERROR] Network error fetching {url}: {e.reason}", file=sys.stderr)
        sys.exit(1)


# ── Event formatters ──────────────────────────────────────────────────────────

def fmt_push(event: dict) -> str:
    payload  = event["payload"]
    repo     = event["repo"]["name"]
    branch   = payload.get("ref", "refs/heads/main").split("/")[-1]
    commits  = payload.get("commits", [])
    n        = len(commits)
    msg      = commits[-1]["message"].split("\n")[0][:72] if commits else "(no commits)"
    return f'Pushed {n} commit{"s" if n != 1 else ""} to `{repo}:{branch}` — _{msg}_'


def fmt_pull_request(event: dict) -> str:
    pr      = event["payload"]["pull_request"]
    action  = event["payload"]["action"]
    repo    = event["repo"]["name"]
    number  = pr["number"]
    title   = pr["title"][:72]
    merged  = pr.get("merged", False)
    if action == "closed" and merged:
        verb = "Merged"
    elif action == "opened":
        verb = "Opened"
    elif action == "closed":
        verb = "Closed"
    elif action == "reopened":
        verb = "Reopened"
    else:
        verb = action.capitalize()
    return f'{verb} PR #{number} in `{repo}` — _{title}_'


def fmt_pr_review(event: dict) -> str:
    review  = event["payload"]["review"]
    pr      = event["payload"]["pull_request"]
    repo    = event["repo"]["name"]
    state   = review.get("state", "").replace("_", " ").lower()
    number  = pr["number"]
    title   = pr["title"][:60]
    return f'Reviewed PR #{number} ({state}) in `{repo}` — _{title}_'


def fmt_issues(event: dict) -> str:
    issue   = event["payload"]["issue"]
    action  = event["payload"]["action"]
    repo    = event["repo"]["name"]
    number  = issue["number"]
    title   = issue["title"][:72]
    return f'{action.capitalize()} issue #{number} in `{repo}` — _{title}_'


def fmt_issue_comment(event: dict) -> str:
    issue   = event["payload"]["issue"]
    repo    = event["repo"]["name"]
    number  = issue["number"]
    title   = issue["title"][:60]
    return f'Commented on issue #{number} in `{repo}` — _{title}_'


def fmt_create(event: dict) -> str:
    ref_type = event["payload"].get("ref_type", "branch")
    ref      = event["payload"].get("ref") or ""
    repo     = event["repo"]["name"]
    if ref_type == "repository":
        return f'Created repository `{repo}`'
    return f'Created {ref_type} `{ref}` in `{repo}`'


def fmt_delete(event: dict) -> str:
    ref_type = event["payload"].get("ref_type", "branch")
    ref      = event["payload"].get("ref", "")
    repo     = event["repo"]["name"]
    return f'Deleted {ref_type} `{ref}` in `{repo}`'


def fmt_fork(event: dict) -> str:
    forkee  = event["payload"]["forkee"]["full_name"]
    origin  = event["repo"]["name"]
    return f'Forked `{origin}` → `{forkee}`'


def fmt_watch(event: dict) -> str:
    return f'Starred `{event["repo"]["name"]}`'


def fmt_release(event: dict) -> str:
    rel     = event["payload"]["release"]
    repo    = event["repo"]["name"]
    tag     = rel.get("tag_name", "")
    name    = rel.get("name") or tag
    return f'Released `{tag}` in `{repo}` — _{name}_'


def fmt_member(event: dict) -> str:
    member  = event["payload"]["member"]["login"]
    repo    = event["repo"]["name"]
    action  = event["payload"]["action"]
    return f'{action.capitalize()} `{member}` as collaborator in `{repo}`'


FORMATTERS = {
    "PushEvent":              fmt_push,
    "PullRequestEvent":       fmt_pull_request,
    "PullRequestReviewEvent": fmt_pr_review,
    "IssuesEvent":            fmt_issues,
    "IssueCommentEvent":      fmt_issue_comment,
    "CreateEvent":            fmt_create,
    "DeleteEvent":            fmt_delete,
    "ForkEvent":              fmt_fork,
    "WatchEvent":             fmt_watch,
    "ReleaseEvent":           fmt_release,
    "MemberEvent":            fmt_member,
}

# ── Cache-busting ─────────────────────────────────────────────────────────────

def bust_picture_cache(content: str, timestamp: str) -> str:
    """
    Append/replace ?t=TIMESTAMP (or &t=TIMESTAMP) on every srcset URL inside
    a <source> tag, so GitHub's camo CDN is forced to re-fetch the image.

    Handles both forms:
      srcset="https://example.com/img?foo=bar"   →  ...?foo=bar&t=20250425120000
      srcset="https://example.com/img"           →  ...?t=20250425120000
    """
    def replace_srcset(m: re.Match) -> str:
        url = m.group(1)
        # Strip any existing &t=... or ?t=... cache-buster
        url = re.sub(r'[&?]t=\d+', '', url)
        # Append the new one
        sep = '&' if '?' in url else '?'
        return f'srcset="{url}{sep}t={timestamp}"'

    return re.sub(r'srcset="([^"]+)"', replace_srcset, content)


# ── Core logic ────────────────────────────────────────────────────────────────

def fetch_events(username: str, limit: int = 5) -> list[str]:
    """Fetch public events and return formatted lines."""
    url    = f"https://api.github.com/users/{username}/events?per_page=50"
    events = gh_get(url)

    lines = []
    for event in events:
        etype = event.get("type", "")
        fmt   = FORMATTERS.get(etype)
        if fmt is None:
            continue
        try:
            text  = fmt(event)
            emoji = EMOJI.get(etype, "🔹")
            ts    = event.get("created_at", "")
            try:
                dt   = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                date = dt.strftime("%b %d, %Y")
            except ValueError:
                date = ts[:10]
            lines.append(f"{emoji} {text} — `{date}`")
        except (KeyError, IndexError, TypeError) as exc:
            print(f"[WARN] Could not format {etype}: {exc}", file=sys.stderr)
            continue

        if len(lines) >= limit:
            break

    return lines


def update_readme(lines: list[str], path: str) -> bool:
    """
    1. Inject activity lines between the section markers.
    2. Cache-bust all <source srcset="..."> URLs.
    Returns True if the file was changed.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        print(f"[ERROR] README not found at {path}", file=sys.stderr)
        sys.exit(1)

    if START_MARKER not in content or END_MARKER not in content:
        print(
            f"[ERROR] Markers not found in {path}.\n"
            f"  Add these lines to your README:\n"
            f"  {START_MARKER}\n"
            f"  {END_MARKER}",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── 1. Update activity section ────────────────────────────────────────────
    new_block = (
        START_MARKER
        + "\n\n"
        + "\n".join(f"1. {line}" for line in lines)
        + "\n\n"
        + END_MARKER
    )
    pattern     = re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER)
    new_content = re.sub(pattern, new_block, content, flags=re.DOTALL)

    # ── 2. Cache-bust <source srcset="..."> URLs ──────────────────────────────
    timestamp   = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    new_content = bust_picture_cache(new_content, timestamp)
    busted      = new_content.count(f"t={timestamp}")
    if busted:
        print(f"[INFO] Cache-busted {busted} srcset URL(s) with t={timestamp}.")

    if new_content == content:
        print("[INFO] README already up-to-date. No changes written.")
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    print(f"[INFO] README updated with {len(lines)} activit{'y' if len(lines)==1 else 'ies'}.")
    return True


def main():
    if not GITHUB_USERNAME:
        print(
            "[ERROR] GITHUB_USERNAME is not set.\n"
            "  Export it: export GITHUB_USERNAME=your-handle",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[INFO] Fetching latest {MAX_EVENTS} events for @{GITHUB_USERNAME} …")
    lines = fetch_events(GITHUB_USERNAME, MAX_EVENTS)

    if not lines:
        print("[WARN] No supported events found. README will not be updated.")
        return

    for i, line in enumerate(lines, 1):
        print(f"  {i}. {line}")

    update_readme(lines, README_PATH)


if __name__ == "__main__":
    main()