"""Fetch top GitHub followers via GraphQL and update the profile README with a ranked table.

Filters out inactive, follow-spam, and high-ratio accounts using configurable heuristics.
Intended to be run as a scheduled GitHub Actions workflow.
"""

"""
   Copyright 2026 Shivam Prakash <https://github.com/shvmpk>

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""
import requests
import json
import os
import sys
import re
from time import sleep
from functools import partial
from dataclasses import dataclass
from typing import Optional

# --- Tunables ---
# All of these can be overridden via environment variables of the same name,
# so you can adjust strictness from the GitHub Actions workflow without
# touching this file.
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", 3))
INITIAL_CWND = int(os.environ.get("INITIAL_CWND", 1))
INITIAL_SSTHRESH = int(os.environ.get("INITIAL_SSTHRESH", 20))
INACTIVITY_THRESHOLD = int(os.environ.get("INACTIVITY_THRESHOLD", 5))          # min contributions/year to count as "active"
NOTABLE_FOLLOWER_THRESHOLD = int(os.environ.get("NOTABLE_FOLLOWER_THRESHOLD", 500))  # marks skipped-but-notable users with *
TABLE_MAX_ENTRIES = int(os.environ.get("TABLE_MAX_ENTRIES", 21))
TABLE_COLUMNS = int(os.environ.get("TABLE_COLUMNS", 7))
MAX_FOLLOWERS_TO_SCAN = os.environ.get("MAX_FOLLOWERS_TO_SCAN")
MAX_FOLLOWERS_TO_SCAN = int(MAX_FOLLOWERS_TO_SCAN) if MAX_FOLLOWERS_TO_SCAN else None

# Quota formula: quota = follower_count * QUOTA_BASE_MULTIPLIER + star bonuses.
# Raising QUOTA_BASE_MULTIPLIER gives modest accounts (few/no starred repos)
# more room before being flagged as follow-spam. 1 = original strict behavior.
QUOTA_BASE_MULTIPLIER = float(os.environ.get("QUOTA_BASE_MULTIPLIER", 3))
# Max following:follower ratio allowed regardless of stars/quota — a hard
# backstop against obvious bot accounts (e.g. 190,000 following, 18,000 followers).
MAX_FOLLOW_RATIO = float(os.environ.get("MAX_FOLLOW_RATIO", 15))


QUERY_TEMPLATE = '''
query($login: String!, $pageSize: Int!, $cursor: String) {
    user(login: $login) {
        followers(first: $pageSize, after: $cursor) {
            pageInfo {
                endCursor
                hasNextPage
            }
            nodes {
                login
                name
                databaseId
                following {
                    totalCount
                }
                followers {
                    totalCount
                }
                repositories(
                    first: 20,
                    orderBy: { field: STARGAZERS, direction: DESC },
                ) {
                    nodes {
                        stargazerCount
                    }
                }
                repositoriesContributedTo(
                    first: 50,
                    contributionTypes: [COMMIT],
                    orderBy: { field: STARGAZERS, direction: DESC },
                ) {
                    nodes {
                        stargazerCount
                    }
                }
                contributionsCollection {
                    contributionCalendar {
                        totalContributions
                    }
                }
            }
        }
    }
}
'''


@dataclass(frozen=True, order=True)
class Follower:
    follower_count: int
    login: str
    user_id: int
    name: str


class GraphQLError(Exception):
    """Raised when the API returns a well-formed error we can't recover from."""
    pass


def run_query(handle: str, page_size: int, cursor: Optional[str], headers: dict) -> dict:
    """POST the GraphQL query and return the parsed JSON body.

    Raises GraphQLError on HTTP-level failure or if the body isn't JSON,
    so callers can distinguish this from network exceptions.
    """
    payload = {
        "query": QUERY_TEMPLATE,
        "variables": {
            "login": handle,
            "pageSize": page_size,
            "cursor": cursor,
        },
    }
    response = requests.post(
        "https://api.github.com/graphql",
        data=json.dumps(payload),
        headers=headers,
    )

    try:
        body = response.json()
    except ValueError:
        raise GraphQLError(
            f"Non-JSON response (status {response.status_code}): {response.text[:500]}"
        )

    if not response.ok:
        raise GraphQLError(
            f"HTTP {response.status_code}: {body.get('errors') or body}"
        )

    if "errors" in body and body["errors"]:
        raise GraphQLError(f"GraphQL errors: {body['errors']}")

    if "data" not in body or body["data"] is None:
        raise GraphQLError(f"Missing data in response: {body}")

    return body


def compute_quota(follower_count: int, repo_stars: list, contrib_stars: list) -> float:
    """Heuristic cap on how many people someone is 'allowed' to follow
    before we consider it follow-for-follow spam."""
    quota = follower_count * QUOTA_BASE_MULTIPLIER
    for i, star_count in enumerate(repo_stars):
        if star_count <= i:
            break
        quota += star_count * (i + 1)
    for i, star_count in enumerate(contrib_stars):
        if star_count <= i:
            break
        quota += i * 5
    return quota


def fetch_followers(handle: str, headers: dict, log) -> list:
    followers = []
    cursor = None
    scanned = 0

    # Separate retry budgets so unrelated failure modes don't share one strike count.
    network_retries = 0
    api_retries = 0
    parse_retries = 0

    cwnd = INITIAL_CWND
    ssthresh = INITIAL_SSTHRESH

    while True:
        try:
            body = run_query(handle, cwnd, cursor, headers)
            network_retries = 0
        except requests.RequestException as e:
            network_retries += 1
            if network_retries > MAX_RETRIES:
                raise
            log(f"Network error ({e}), retrying")
            sleep(5)
            continue
        except GraphQLError as e:
            api_retries += 1
            if api_retries > MAX_RETRIES:
                log(f"Giving up after {MAX_RETRIES} API errors: {e}")
                raise
            ssthresh = max(1, cwnd // 2)
            cwnd = INITIAL_CWND
            log(f"API error ({e}), entering slow start with ssthresh={ssthresh}")
            sleep(5)
            continue

        api_retries = 0
        if cwnd < ssthresh:
            cwnd = min(ssthresh, cwnd * 2)
        else:
            cwnd += 1

        res = body["data"]["user"]["followers"]

        try:
            for follower in res["nodes"]:
                following = follower["following"]["totalCount"]
                login = follower["login"]
                name = follower["name"] or login
                user_id = follower["databaseId"]
                follower_count = follower["followers"]["totalCount"]
                total_contributions = follower["contributionsCollection"][
                    "contributionCalendar"
                ]["totalContributions"]
                notable = follower_count > NOTABLE_FOLLOWER_THRESHOLD

                if total_contributions <= INACTIVITY_THRESHOLD:
                    log(
                        f"Skipped{'*' if notable else ''} (inactive): "
                        f"https://github.com/{login} with {follower_count} followers "
                        f"and {following} following"
                    )
                    continue

                repo_stars = [r["stargazerCount"] for r in follower["repositories"]["nodes"]]
                contrib_stars = [
                    r["stargazerCount"] for r in follower["repositoriesContributedTo"]["nodes"]
                ]
                quota = compute_quota(follower_count, repo_stars, contrib_stars)
                ratio_too_high = follower_count > 0 and (following / follower_count) > MAX_FOLLOW_RATIO

                if following > quota or ratio_too_high:
                    log(
                        f"Skipped{'*' if notable else ''} (quota): "
                        f"https://github.com/{login} with {follower_count} followers "
                        f"and {following} following"
                    )
                    continue

                entry = Follower(follower_count, login, user_id, name)
                followers.append(entry)
                log(str(entry))
        except (TypeError, KeyError) as e:
            parse_retries += 1
            if parse_retries > MAX_RETRIES:
                log(f"Unparseable response after {MAX_RETRIES} attempts: {res}")
                raise
            log(f"Parse error ({e}), retrying")
            ssthresh = max(1, cwnd // 2)
            cwnd = INITIAL_CWND
            sleep(5)
            continue

        scanned += len(res["nodes"])
        if MAX_FOLLOWERS_TO_SCAN and scanned >= MAX_FOLLOWERS_TO_SCAN:
            break
        if not res["pageInfo"]["hasNextPage"]:
            break
        cursor = res["pageInfo"]["endCursor"]

    return followers


def render_table(followers: list) -> str:
    html = "<table>\n"
    top = followers[:TABLE_MAX_ENTRIES]

    for i, follower in enumerate(top):
        if i % TABLE_COLUMNS == 0:
            if i != 0:
                html += "  </tr>\n"
            html += "  <tr>\n"
        html += f'''    <td align="center">
      <a href="https://github.com/{follower.login}">
        <img src="https://avatars2.githubusercontent.com/u/{follower.user_id}" width="100px;" alt="{follower.login}"/>
      </a>
      <br />
      <a href="https://github.com/{follower.login}">{follower.name}</a>
    </td>
'''
    html += "  </tr>\n</table>"
    return html


def update_readme(readme_path: str, html: str) -> None:
    with open(readme_path, "r") as readme:
        content = readme.read()

    new_content = re.sub(
        r"(?<=<!\-\-START_SECTION:top\-followers\-\->)[\s\S]*(?=<!\-\-END_SECTION:top\-followers\-\->)",
        f"\n{html}\n",
        content,
    )

    with open(readme_path, "w") as readme:
        readme.write(new_content)


def main():
    assert len(sys.argv) == 4, "Usage: getTopFollowers.py <handle> <token> <readmePath>"
    handle, token, readme_path = sys.argv[1], sys.argv[2], sys.argv[3]

    log = partial(print, flush=True)
    headers = {"Authorization": f"token {token}"}

    followers = fetch_followers(handle, headers, log)
    followers = sorted(set(followers), reverse=True)

    html = render_table(followers)
    update_readme(readme_path, html)


if __name__ == "__main__":
    main()
