"""Builds this profile from public GitHub data; run by .github/workflows/update.yml.

Writes README.md and assets/*.svg (github.com/idoodler) and profile.json (read by idoodler.de), so both
show the same data. Hand-written content lives in profile.toml; don't edit the generated files.
Standard library only. A failed run writes nothing, so the last good version stays online.
"""

import html
import json
import os
import re
import shutil
import textwrap
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

USER = os.environ.get("GITHUB_USER", "idoodler")
TOKEN = os.environ.get("GITHUB_TOKEN", "")
ROOT = Path(__file__).resolve().parent

LANGUAGE_REPOS = 25  # most recently pushed repos whose languages are counted
LANGUAGE_HALF_LIFE_YEARS = 1.5  # a repo last pushed this long ago counts half
PROJECT_COUNT = 9
COMMIT_REPOS = 6
COMMIT_COUNT = 10
COMMITS_PER_REPO = 3
# Generated-file languages that say nothing about what the code is written in
IGNORED_LANGUAGES = {"Makefile", "Batchfile", "Procfile", "Roff", "CMake", "M4", "Nix"}

# GitHub's language colours for what's likely to show up; the rest get a neutral colour
LANGUAGE_COLORS = {
    "JavaScript": "#f1e05a", "TypeScript": "#3178c6", "Kotlin": "#a97bff", "Swift": "#f05138",
    "Objective-C": "#438eff", "Python": "#3572a5", "HTML": "#e34c26", "CSS": "#663399",
    "SCSS": "#c6538c", "Shell": "#89e051", "QML": "#44a51c", "Java": "#b07219", "Go": "#00add8",
    "Rust": "#dea584", "C++": "#f34b7d", "C": "#555555", "Dockerfile": "#384d54", "Ruby": "#701516",
    "PHP": "#4f5d95", "Vue": "#41b883", "Svelte": "#ff3e00", "Dart": "#00b4ab", "Lua": "#000080",
}
NEUTRAL = "#8b949e"

THEMES = {
    "dark": {
        "card": "#14161c", "line": "#25272e", "fg": "#eef0f4", "muted": "#9198a6",
        "levels": ["#1f2127", "#3b2f73", "#5a45ad", "#7f66e6", "#b7a5ff"],
    },
    "light": {
        "card": "#ffffff", "line": "#e4e5ea", "fg": "#15171c", "muted": "#626a78",
        "levels": ["#eeeef1", "#d6ccff", "#a998f5", "#7a60e6", "#4c32b8"],
    },
}
FONT = "Inter, system-ui, -apple-system, 'Segoe UI', Roboto, 'Noto Sans', sans-serif"
WIDTH = 840


# GitHub data

def fetch(url, accept="application/vnd.github+json"):
    headers = {"User-Agent": f"{USER}-profile", "Accept": accept}
    if TOKEN and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {TOKEN}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode()


def api(path):
    return json.loads(fetch(f"https://api.github.com{path}"))


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def contributions():
    """The calendar from the public profile page, so the numbers match what GitHub shows there."""
    page = fetch(f"https://github.com/users/{USER}/contributions", accept="text/html")
    counts = {}
    for target, text in re.findall(r'<tool-tip[^>]*\bfor="([^"]+)"[^>]*>([^<]*)</tool-tip>', page):
        match = re.match(r"\s*([\d,]+) contributions?", html.unescape(text))
        counts[target] = int(match.group(1).replace(",", "")) if match else 0
    days = []
    for cell in re.findall(r"<td[^>]*\bdata-date=[^>]*>", page):
        date = re.search(r'data-date="([\d-]+)"', cell)
        level = re.search(r'data-level="(\d)"', cell)
        cell_id = re.search(r'\bid="([^"]+)"', cell)
        if date and level and cell_id:
            days.append({"date": date.group(1), "level": int(level.group(1)), "count": counts.get(cell_id.group(1), 0)})
    days.sort(key=lambda day: day["date"])
    if len(days) < 300:
        raise ValueError(f"contribution calendar: only {len(days)} days parsed")
    return {"total": sum(day["count"] for day in days), "days": days}


def collect(config):
    now = datetime.now(timezone.utc)
    user = api(f"/users/{USER}")
    own = [repo for repo in api(f"/users/{USER}/repos?type=owner&sort=pushed&per_page=100") if not repo["fork"]]

    # Public repos elsewhere (organizations) the user pushed to in the last 90 days
    known = {repo["full_name"] for repo in own}
    others = []
    names = {e["repo"]["name"] for e in api(f"/users/{USER}/events/public?per_page=100") if e["type"] == "PushEvent"}
    for name in sorted(names - known):
        try:
            repo = api(f"/repos/{name}")
        except urllib.error.HTTPError:
            continue
        if not repo["private"] and not repo["fork"]:
            others.append(repo)

    # Not archived, and not this profile repo
    repos = [repo for repo in own + others if not repo["archived"] and repo["name"].lower() != USER.lower()]
    repos.sort(key=lambda repo: repo["pushed_at"], reverse=True)

    # Languages: each repo's mix (so one big codebase doesn't hide the rest), weighted down by the age of its
    # last push
    weights = {}
    for repo in repos[:LANGUAGE_REPOS]:
        factor = 0.5 ** ((now - parse_time(repo["pushed_at"])).days / 365.25 / LANGUAGE_HALF_LIFE_YEARS)
        sizes = {
            language: size
            for language, size in api(f"/repos/{repo['full_name']}/languages").items()
            if language not in IGNORED_LANGUAGES
        }
        for language, size in sizes.items():
            weights[language] = weights.get(language, 0) + size / sum(sizes.values()) * factor
    total_weight = sum(weights.values()) or 1
    languages = [
        {"name": name, "share": round(weight / total_weight, 3)}
        for name, weight in sorted(weights.items(), key=lambda item: item[1], reverse=True)
        if weight / total_weight >= 0.01
    ][:10]

    # Latest commits by the user in the most recently pushed repos, without merges
    commits = []
    for repo in repos[:COMMIT_REPOS]:
        try:
            items = api(f"/repos/{repo['full_name']}/commits?author={USER}&per_page=10")
        except urllib.error.HTTPError:
            continue  # empty repos answer 409
        for item in [item for item in items if len(item["parents"]) < 2][:COMMITS_PER_REPO]:
            commits.append({
                "repo": repo["full_name"],
                "message": item["commit"]["message"].split("\n", 1)[0][:140],
                "url": item["html_url"],
                "date": item["commit"]["author"]["date"],
            })
    commits.sort(key=lambda commit: commit["date"], reverse=True)

    return {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "user": {key: user.get(key) for key in (
            "login", "name", "avatar_url", "html_url", "bio", "company", "location", "followers", "public_repos",
        )},
        "interests": config.get("interests", []),
        "socials": config.get("socials", []),
        "stars": sum(repo["stargazers_count"] for repo in own),
        "contributions": contributions(),
        "languages": languages,
        "projects": [
            {
                "name": repo["name"],
                "full_name": repo["full_name"],
                "owner": repo["owner"]["login"],
                "url": repo["html_url"],
                "description": repo["description"],
                "language": repo["language"],
                "stars": repo["stargazers_count"],
                "pushed_at": repo["pushed_at"],
            }
            for repo in repos[:PROJECT_COUNT]
        ],
        "commits": commits[:COMMIT_COUNT],
    }


# SVG cards: the page's design for the README, which allows no CSS or scripts

def svg(width, height, body):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">{body}</svg>\n'
    )


def card(x, y, width, height, theme):
    return (
        f'<rect x="{x + 0.5}" y="{y + 0.5}" width="{width - 1}" height="{height - 1}" rx="14" '
        f'fill="{theme["card"]}" stroke="{theme["line"]}"/>'
    )


def text(x, y, value, fill, size, weight=400):
    return f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" font-weight="{weight}">{escape(str(value))}</text>'


def text_width(value, size):
    """Rough width: SVG text can't be measured here, and capitals run wider."""
    return round(sum(0.72 if char.isupper() else 0.55 for char in value) * size)


def stat_items(data):
    return [
        (data["contributions"]["total"], "contributions (12 mo)"),
        (data["user"]["public_repos"], "public repositories"),
        (data["stars"], "stars"),
        (data["user"]["followers"], "followers"),
    ]


def stats_svg(data, theme):
    gap, height = 12, 84
    width = (WIDTH - 3 * gap) // 4
    body = ""
    for index, (value, label) in enumerate(stat_items(data)):
        x = index * (width + gap)
        body += card(x, 0, width, height, theme)
        body += text(x + 20, 38, f"{value:,}", theme["fg"], 26, 700)
        body += text(x + 20, 62, label, theme["muted"], 13)
    return svg(WIDTH, height, body)


def calendar_svg(data, theme):
    days = data["contributions"]["days"]
    first = (datetime.fromisoformat(days[0]["date"]).weekday() + 1) % 7  # columns start on Sunday
    step, cell = 14, 11
    columns = (len(days) + first - 1) // 7 + 1
    left = (WIDTH - (columns * step - (step - cell))) // 2
    top = 52
    body = card(0, 0, WIDTH, top + 7 * step + 16, theme) + text(20, 32, "Activity", theme["fg"], 15, 600)
    for index, day in enumerate(days):
        column, row = divmod(index + first, 7)
        body += (
            f'<rect x="{left + column * step}" y="{top + row * step}" width="{cell}" height="{cell}" rx="3" '
            f'fill="{theme["levels"][day["level"]]}"/>'
        )
    return svg(WIDTH, top + 7 * step + 16, body)


def languages_svg(data, theme):
    bar_width = WIDTH - 40
    total = sum(language["share"] for language in data["languages"]) or 1
    bar = ""
    x = 20.0
    for language in data["languages"]:
        width = language["share"] / total * bar_width
        color = LANGUAGE_COLORS.get(language["name"], NEUTRAL)
        bar += f'<rect x="{x:.1f}" y="48" width="{max(width - 2, 2):.1f}" height="10" fill="{color}"/>'
        x += width
    legend = ""
    x, y = 20, 84
    for language in data["languages"]:
        percent = f'{round(language["share"] * 100)}%'
        item = 18 + text_width(f'{language["name"]} {percent}', 14) + 22
        if x + item > WIDTH - 20:
            x, y = 20, y + 24
        color = LANGUAGE_COLORS.get(language["name"], NEUTRAL)
        legend += f'<circle cx="{x + 5}" cy="{y - 5}" r="5" fill="{color}"/>'
        legend += (
            f'<text x="{x + 18}" y="{y}" font-size="14" fill="{theme["fg"]}">{escape(language["name"])} '
            f'<tspan fill="{theme["muted"]}">{percent}</tspan></text>'
        )
        x += item
    height = y + 22
    clip = f'<clipPath id="bar"><rect x="20" y="48" width="{bar_width}" height="10" rx="5"/></clipPath>'
    return svg(
        WIDTH,
        height,
        clip + card(0, 0, WIDTH, height, theme) + text(20, 32, "Languages lately", theme["fg"], 15, 600)
        + f'<g clip-path="url(#bar)">{bar}</g>' + legend,
    )


def shorten(value, limit):
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def project_svg(project, login, theme):
    width, height = 272, 132
    name = project["name"] if project["owner"] == login else project["full_name"]
    body = card(0, 0, width, height, theme) + text(20, 34, shorten(name, 28), theme["fg"], 16, 600)
    lines = textwrap.wrap(project["description"] or "", 36, max_lines=2, placeholder="…")
    for index, line in enumerate(lines):
        body += text(20, 58 + index * 20, line, theme["muted"], 13.5)
    x = 20
    if project["language"]:
        color = LANGUAGE_COLORS.get(project["language"], NEUTRAL)
        body += f'<circle cx="{x + 5}" cy="108" r="5" fill="{color}"/>'
        body += text(x + 16, 112, project["language"], theme["muted"], 12.5)
        x += 16 + text_width(project["language"], 12.5) + 14
    if project["stars"]:
        star = f'★ {project["stars"]}'
        body += text(x, 112, star, theme["muted"], 12.5)
        x += text_width(star, 12.5) + 18
    body += text(x, 112, f'{parse_time(project["pushed_at"]):%b %Y}', theme["muted"], 12.5)
    return svg(width, height, body)


# README

def picture(name, alt, width="100%"):
    return (
        f'<picture><source media="(prefers-color-scheme: dark)" srcset="assets/{name}-dark.svg">'
        f'<img src="assets/{name}-light.svg" alt="{html.escape(alt)}" width="{width}"></picture>'
    )


def markdown_text(value):
    return re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", value)


def readme(data):
    lines = ["<!-- Generated by build.py from GitHub data and profile.toml, don't edit. Also shown on idoodler.de. -->", ""]
    if data["user"].get("bio"):
        lines += [markdown_text(data["user"]["bio"]), ""]
    lines += [" · ".join(markdown_text(interest) for interest in data["interests"]), ""]
    lines += [" · ".join(f'[{markdown_text(s["name"])}]({s["url"]})' for s in data["socials"]), ""]
    stats = ", ".join(f"{value:,} {label}" for value, label in stat_items(data))
    total = data["contributions"]["total"]
    languages = ", ".join(f'{item["name"]} {round(item["share"] * 100)}%' for item in data["languages"])
    lines += [
        picture("stats", stats), "",
        picture("activity", f"{total:,} contributions in the last 12 months"), "",
        picture("languages", f"Languages lately: {languages}"), "",
        "### Recently active projects", "",
    ]
    lines.append("<p>")
    for index, project in enumerate(data["projects"]):
        description = f': {project["description"]}' if project["description"] else ""
        lines.append(f'<a href="{html.escape(project["url"])}">{picture(f"project-{index}", project["name"] + description, "32%")}</a>')
    lines += ["</p>", "", f'[All repositories →](https://github.com/{USER}?tab=repositories)', ""]
    lines += ["### Latest commits", ""]
    for commit in data["commits"]:
        repo = commit["repo"].split("/")[-1]
        lines.append(f'- [{markdown_text(commit["message"])}]({commit["url"]}) · {markdown_text(repo)} · {parse_time(commit["date"]):%-d %b %Y}')
    return "\n".join(lines) + "\n"


def main():
    with open(ROOT / "profile.toml", "rb") as file:
        config = tomllib.load(file)
    data = collect(config)

    # generated_at marks the last change, so a run without news leaves every file as it is
    try:
        previous = json.loads((ROOT / "profile.json").read_text())
    except (OSError, ValueError):
        previous = {}
    if {**previous, "generated_at": ""} == {**data, "generated_at": ""}:
        data["generated_at"] = previous["generated_at"]

    # Render everything before touching a file, so a failure can't leave half an update
    assets = {}
    for name, theme in THEMES.items():
        assets[f"stats-{name}.svg"] = stats_svg(data, theme)
        assets[f"activity-{name}.svg"] = calendar_svg(data, theme)
        assets[f"languages-{name}.svg"] = languages_svg(data, theme)
        for index, project in enumerate(data["projects"]):
            assets[f"project-{index}-{name}.svg"] = project_svg(project, data["user"]["login"], theme)
    readme_text = readme(data)

    shutil.rmtree(ROOT / "assets", ignore_errors=True)
    (ROOT / "assets").mkdir()
    for name, content in assets.items():
        (ROOT / "assets" / name).write_text(content)
    (ROOT / "README.md").write_text(readme_text)
    (ROOT / "profile.json").write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    print(f'{len(data["projects"])} projects, {len(data["commits"])} commits, generated_at {data["generated_at"]}')


if __name__ == "__main__":
    main()
