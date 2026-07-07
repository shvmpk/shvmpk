#!/usr/bin/env python3
"""
update_pinned_repos.py

Builds a "showcase" section for your GitHub profile README and writes it
between two marker comments:

    <!-- PINNED-REPOS:START -->
    ...generated content...
    <!-- PINNED-REPOS:END -->

Everything else in README.md is left untouched.

Two modes, chosen automatically:

1. CURATED MODE (recommended if you want full control)
   If a file named "showcase-repos.txt" exists (path configurable via
   SHOWCASE_FILE), each non-empty, non-comment line should be a repo in
   "owner/name" form, e.g.:

       your-username/cool-project
       your-username/another-project
       some-org/repo-you-contribute-to

   These exact repos are fetched and shown, in the order listed.

2. PINNED MODE (default/fallback)
   If no curated file is found, falls back to fetching your actual
   GitHub-pinned repositories (the ones shown on your profile page) via
   GraphQL.

Required environment variables:
    GH_TOKEN      - a GitHub token with at least public read access.
                    Falls back to GITHUB_TOKEN if GH_TOKEN is not set.
    GH_USERNAME   - your GitHub username (used for pinned-mode, and as the
                    default owner for curated entries written as just
                    "repo-name" with no "owner/"). Falls back to
                    GITHUB_REPOSITORY_OWNER if not provided.

Optional environment variables:
    MAX_PINNED    - max pinned repos to fetch in pinned mode (default 6).
    README_PATH   - path to the README file to update (default "README.md").
    SHOWCASE_FILE - path to the curated repo list (default "showcase-repos.txt").
"""

import json
import os
import sys
import urllib.request
import urllib.error

GRAPHQL_URL = "https://api.github.com/graphql"

START_MARKER = "<!-- PINNED-REPOS:START -->"
END_MARKER = "<!-- PINNED-REPOS:END -->"

REPO_FIELDS = """
    name
    nameWithOwner
    description
    url
    homepageUrl
    stargazerCount
    forkCount
    primaryLanguage {
      name
    }
    repositoryTopics(first: 6) {
      nodes {
        topic {
          name
        }
      }
    }
    isFork
    isArchived
"""

PINNED_QUERY = f"""
query ($login: String!, $maxItems: Int!) {{
  user(login: $login) {{
    pinnedItems(first: $maxItems, types: [REPOSITORY]) {{
      nodes {{
        ... on Repository {{
          {REPO_FIELDS}
        }}
      }}
    }}
  }}
}}
"""


def get_env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        print(f"ERROR: required environment variable '{name}' is not set.", file=sys.stderr)
        sys.exit(1)
    return value


def graphql_request(token, query, variables):
    payload = {"query": query, "variables": variables}
    req = urllib.request.Request(
        GRAPHQL_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "pinned-repos-readme-script",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")
        print(f"ERROR: GitHub API request failed ({e.code}): {detail}", file=sys.stderr)
        sys.exit(1)

    if "errors" in body:
        print(f"ERROR: GraphQL errors: {body['errors']}", file=sys.stderr)
        sys.exit(1)

    return body["data"]


def fetch_pinned_repos(token, username, max_items):
    data = graphql_request(token, PINNED_QUERY, {"login": username, "maxItems": max_items})
    user = data.get("user")
    if not user:
        print(f"ERROR: no such user '{username}', or token lacks access.", file=sys.stderr)
        sys.exit(1)
    return user["pinnedItems"]["nodes"]


def read_curated_list(showcase_file, default_owner):
    """Parse 'owner/name' or bare 'name' lines into (owner, name) tuples."""
    entries = []
    with open(showcase_file, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "/" in line:
                owner, name = line.split("/", 1)
            else:
                owner, name = default_owner, line
            entries.append((owner.strip(), name.strip()))
    return entries


def fetch_curated_repos(token, entries):
    """Fetch a specific, ordered list of repos using aliased GraphQL queries."""
    if not entries:
        return []

    alias_fields = []
    for i, (owner, name) in enumerate(entries):
        alias_fields.append(
            f'r{i}: repository(owner: "{owner}", name: "{name}") {{ {REPO_FIELDS} }}'
        )

    query = "query {\n" + "\n".join(alias_fields) + "\n}"
    data = graphql_request(token, query, {})

    repos = []
    for i, (owner, name) in enumerate(entries):
        repo = data.get(f"r{i}")
        if repo is None:
            print(
                f"WARNING: could not fetch '{owner}/{name}' (typo, private, or no access?) — skipping.",
                file=sys.stderr,
            )
            continue
        repos.append(repo)
    return repos


def render_markdown(repos):
    if not repos:
        return "_No repositories to show yet — add some to showcase-repos.txt or pin some on your profile!_"

    # A clean GitHub-flavored-markdown table renders far more reliably
    # across README viewers than nested HTML tables, and keeps things
    # simple: just the repo name (linked) and its description.
    lines = [
        "| Repository | Description |",
        "| :--- | :--- |",
    ]

    for repo in repos:
        name = repo["nameWithOwner"]
        url = repo["url"]
        desc = (repo.get("description") or "_No description provided._").replace("|", "\\|").replace("\n", " ")

        lines.append(f"| **[{name}]({url})** | {desc} |")

    return "\n".join(lines)


def update_readme(readme_path, generated_content):
    if not os.path.exists(readme_path):
        print(f"'{readme_path}' not found, creating a new one.", file=sys.stderr)
        content = f"{START_MARKER}\n{END_MARKER}\n"
    else:
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()

    if START_MARKER not in content or END_MARKER not in content:
        print(
            f"ERROR: could not find '{START_MARKER}' / '{END_MARKER}' markers in "
            f"{readme_path}. Add them where you want the showcase to appear.",
            file=sys.stderr,
        )
        sys.exit(1)

    before = content.split(START_MARKER)[0]
    after = content.split(END_MARKER)[1]

    new_content = f"{before}{START_MARKER}\n\n{generated_content}\n\n{END_MARKER}{after}"

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Updated {readme_path}.")


def main():
    token = get_env("GH_TOKEN") or get_env("GITHUB_TOKEN", required=True)
    username = get_env("GH_USERNAME") or get_env("GITHUB_REPOSITORY_OWNER", required=True)
    max_items = int(get_env("MAX_PINNED", "6"))
    readme_path = get_env("README_PATH", "README.md")
    showcase_file = get_env("SHOWCASE_FILE", "showcase-repos.txt")

    if os.path.exists(showcase_file):
        print(f"Found '{showcase_file}' — using curated repo list.")
        entries = read_curated_list(showcase_file, default_owner=username)
        repos = fetch_curated_repos(token, entries)
    else:
        print(f"No '{showcase_file}' found — falling back to your pinned repos.")
        repos = fetch_pinned_repos(token, username, max_items)

    markdown = render_markdown(repos)
    update_readme(readme_path, markdown)


if __name__ == "__main__":
    main()