#!/usr/bin/env python3
"""Evaluate the human-review-policy for one pull request and report it.

Called by action.yml. The rule:

  human-authored PR  needs HUMAN_APPROVALS (default 1) distinct human approvers
  bot-authored PR    needs BOT_APPROVALS (default 2) distinct human approvers

so every production PR has two distinct humans on it ("four eyes"): the author
and a reviewer, or two reviewers when the author is a bot.

Who counts as an approver: the reviewer's latest decisive review
(APPROVED / CHANGES_REQUESTED / DISMISSED; a COMMENTED review changes nothing,
as on GitHub) is APPROVED, the reviewer has write access to the repository
(on a public repository anyone can submit an approving review), and the
reviewer is neither

  - the PR author, nor
  - a bot (GitHub `type: Bot`, i.e. any GitHub App) or a listed service account
    (machine users have `type: User`, so they have to be named).

Pushing commits to the PR does not stop someone from approving it, and an
approval stays valid after new commits unless it is dismissed (by hand, or by
the repo's "dismiss stale approvals" setting, which then shows up as DISMISSED).

The result is written as a commit status (not a check run) on the PR's head
SHA: statuses are last-write-wins per context, so whichever event evaluated
last decides, without depending on how GitHub picks between several check runs
of the same name from different workflow runs.

Merge queues: a merge group gets its own commit, which needs the status too.
For a merge group the queued PR (named in the gh-readonly-queue/<base>/pr-<N>-
branch) is re-evaluated and the status is written on the merge group's commit.

Post-merge tracking: admins may merge past the check in an incident, and such
a PR then needs its review after the fact. A PR that is merged without meeting
the policy gets the POST_MERGE_LABEL label (default needs-post-merge-review)
and a comment; the label comes off, with another comment, once later reviews
on the merged PR meet the policy. Only PRs merged into the default branch,
the one the ruleset protects, are tracked.

Standard library only: the runner's system python, no install step.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")
DECISIVE_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}
# Machine users that are `type: User` on GitHub but are not people. GitHub Apps
# (`type: Bot`, `<name>[bot]`) are recognised without being listed here.
DEFAULT_SERVICE_ACCOUNTS = frozenset({"rollbar-circleci-machine", "web-flow", "ghost"})
DEFAULT_POST_MERGE_LABEL = "needs-post-merge-review"
MERGE_GROUP_BRANCH = re.compile(r"gh-readonly-queue/.+/pr-(\d+)-[0-9a-f]{40}$")
# Transient GitHub API failures are retried with these pauses (seconds).
RETRY_DELAYS = (2, 5, 10)
NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
MAX_DESCRIPTION = 140
# Repository permissions (collaborators/{user}/permission) that may approve.
WRITE_PERMISSIONS = {"admin", "maintain", "write"}


@dataclass
class Policy:
    human_approvals: int = 1
    bot_approvals: int = 2
    service_accounts: frozenset[str] = DEFAULT_SERVICE_ACCOUNTS


@dataclass
class Result:
    state: str  # "success" | "failure"
    description: str
    author_is_bot: bool
    required: int
    approvers: list[str] = field(default_factory=list)
    ignored: dict[str, str] = field(default_factory=dict)


def is_automated(user: dict | None, policy: Policy) -> bool:
    """A deleted account (user is None) is treated as automated: never eligible."""
    if not user:
        return True
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]") or login in policy.service_accounts


def evaluate(
    pr: dict, reviews: list[dict], policy: Policy, can_write: Callable[[str], bool]
) -> Result:
    """can_write(login): whether that reviewer has write access to the repository."""
    author = pr.get("user")
    author_login = ((author or {}).get("login") or "").lower()
    author_is_bot = is_automated(author, policy)
    required = policy.bot_approvals if author_is_bot else policy.human_approvals

    # Reviews come back oldest first; keep each reviewer's latest decisive one.
    latest: dict[str, dict] = {}
    reviewers: dict[str, dict | None] = {}
    for review in sorted(reviews, key=lambda r: (r.get("submitted_at") or "", r.get("id") or 0)):
        user = review.get("user")
        login = ((user or {}).get("login") or "").lower()
        if not login or review.get("state") not in DECISIVE_STATES:
            continue
        latest[login] = review
        reviewers[login] = user

    approvers, ignored = [], {}
    for login, review in sorted(latest.items()):
        if review["state"] != "APPROVED":
            continue
        if login == author_login:
            ignored[login] = "PR author"
        elif is_automated(reviewers[login], policy):
            ignored[login] = "bot or service account"
        elif not can_write(login):
            ignored[login] = "no write access"
        else:
            approvers.append(login)

    kind = "bot" if author_is_bot else "human"
    have = len(approvers)
    if have >= required:
        state = "success"
        description = f"{have}/{required} human approvals ({kind}-authored PR)"
    else:
        state = "failure"
        missing = required - have
        description = (
            f"{have}/{required} human approvals ({kind}-authored PR); needs {missing} more"
        )
    return Result(state, description[:MAX_DESCRIPTION], author_is_bot, required, approvers, ignored)


# --------------------------------------------------------------------------- #
# GitHub plumbing
# --------------------------------------------------------------------------- #


def request(method: str, url: str, token: str, body: dict | None = None):
    if not url.startswith("http"):
        url = f"{API}{url}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for delay in (*RETRY_DELAYS, None):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return (json.loads(raw) if raw else None), resp.headers.get("Link") or ""
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or delay is None:
                raise
        except (urllib.error.URLError, TimeoutError):
            if delay is None:
                raise
        print(f"GitHub API {method} {url} failed; retrying in {delay}s")
        time.sleep(delay)


def get_all(url: str, token: str) -> list:
    sep = "&" if "?" in url else "?"
    url, items = f"{url}{sep}per_page=100", []
    while url:
        page, link = request("GET", url, token)
        items.extend(page)
        match = NEXT_LINK.search(link)
        url = match.group(1) if match else ""
    return items


def write_access(repo: str, token: str) -> Callable[[str], bool]:
    """can_write for evaluate(), backed by the repository permissions API."""

    def can_write(login: str) -> bool:
        quoted = urllib.parse.quote(login, safe="")
        try:
            perm, _ = request("GET", f"/repos/{repo}/collaborators/{quoted}/permission", token)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # not a user (any more)
                return False
            raise
        return (perm or {}).get("permission") in WRITE_PERMISSIONS

    return can_write


def merge_group_pr(ref: str) -> int | None:
    match = MERGE_GROUP_BRANCH.search(ref or "")
    return int(match.group(1)) if match else None


def resolve_target(
    repo: str, token: str, event_name: str, event: dict, explicit: str
) -> tuple[int | None, str | None]:
    """(PR number, merge group commit to report on, if this is a merge group)."""
    if explicit:
        return int(explicit), None
    if event_name == "merge_group":
        group = event["merge_group"]
        return merge_group_pr(group.get("head_ref")), group["head_sha"]
    if event_name == "workflow_run" and event["workflow_run"].get("event") == "merge_group":
        run = event["workflow_run"]
        return merge_group_pr(run.get("head_branch")), run["head_sha"]
    return resolve_pr_number(repo, token, event_name, event), None


def resolve_pr_number(repo: str, token: str, event_name: str, event: dict) -> int | None:
    if "pull_request" in event:
        return int(event["pull_request"]["number"])
    if event_name == "workflow_run":
        run = event["workflow_run"]
        # pull_requests is filled in for same-repo PRs whose head is the run's SHA.
        for pr in run.get("pull_requests") or []:
            return int(pr["number"])
        # It is always empty for PRs from forks (public SDK repos), and can be
        # for a PR that was just merged: look the PR up by its head branch, then
        # by commit. state=all, so merged PRs are found for post-merge tracking.
        owner = ((run.get("head_repository") or {}).get("owner") or {}).get("login")
        if owner and run.get("head_branch"):
            head = urllib.parse.quote(f"{owner}:{run['head_branch']}", safe="")
            pulls, _ = request("GET", f"/repos/{repo}/pulls?state=all&head={head}", token)
            if (number := pick_pr(pulls, run["head_sha"])) is not None:
                return number
        pulls, _ = request("GET", f"/repos/{repo}/commits/{run['head_sha']}/pulls", token)
        return pick_pr(pulls, run["head_sha"])
    return None


def pick_pr(pulls: list[dict], head_sha: str) -> int | None:
    """The PR whose head is the run's commit, else the open one on that branch."""
    for pr in pulls:
        if (pr.get("head") or {}).get("sha") == head_sha:
            return int(pr["number"])
    for pr in pulls:
        if pr.get("state") == "open":
            return int(pr["number"])
    return None


def parse_list(raw: str) -> set[str]:
    return {item.strip().lower() for item in re.split(r"[,\s]+", raw or "") if item.strip()}


def policy_from_env() -> Policy:
    return Policy(
        human_approvals=int(os.environ.get("HUMAN_APPROVALS") or 1),
        bot_approvals=int(os.environ.get("BOT_APPROVALS") or 2),
        service_accounts=frozenset(
            DEFAULT_SERVICE_ACCOUNTS | parse_list(os.environ.get("SERVICE_ACCOUNTS", ""))
        ),
    )


def track_post_merge(repo: str, token: str, pr: dict, result: Result, label: str) -> None:
    """Flag a PR merged without meeting the policy until its post-merge review is done."""
    number = pr["number"]
    flagged = label in {lbl["name"] for lbl in pr.get("labels") or []}
    kind = "bot" if result.author_is_bot else "human"
    have, required = len(result.approvers), result.required
    base = pr.get("base") or {}
    if base.get("ref") != (base.get("repo") or {}).get("default_branch"):
        # The ruleset only protects the default branch; merges elsewhere (stacked
        # PRs, release branches) never needed the policy.
        print(f"PR #{number} was merged into {base.get('ref')}, not the default branch")
        return
    if result.state != "success" and not flagged:
        request("POST", f"/repos/{repo}/issues/{number}/labels", token, {"labels": [label]})
        body = (
            f"This PR was merged without meeting `human-review-policy`: it has {have} of the "
            f"{required} human approval(s) a {kind}-authored PR needs. Merging past the "
            "check is reserved for incidents, and a **post-merge review is required**.\n\n"
            f"`{label}` comes off once {required - have} more human (not the author, not a bot) "
            "approves this PR. Reviews can still be submitted after merge, from "
            "*Files changed* → *Review changes*."
        )
        request("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        print(f"PR #{number} merged without meeting the policy: labelled {label}")
    elif result.state == "success" and flagged:
        quoted = urllib.parse.quote(label, safe="")
        request("DELETE", f"/repos/{repo}/issues/{number}/labels/{quoted}", token)
        approvers = ", ".join(f"@{a}" for a in result.approvers)
        body = f"Post-merge review complete ({have}/{required} human approvals: {approvers})."
        request("POST", f"/repos/{repo}/issues/{number}/comments", token, {"body": body})
        print(f"PR #{number} post-merge review complete: removed {label}")


def write_summary(pr: dict, result: Result) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = [
        f"### human-review-policy: {result.state}",
        "",
        f"PR #{pr['number']} by `{(pr.get('user') or {}).get('login', 'ghost')}` "
        f"({'bot' if result.author_is_bot else 'human'}-authored) needs "
        f"{result.required} human approval(s) besides the author.",
        "",
        f"Counted: {', '.join(f'`{a}`' for a in result.approvers) or 'none'}",
    ]
    if result.ignored:
        lines += ["", "Not counted:"]
        lines += [f"- `{login}`: {why}" for login, why in sorted(result.ignored.items())]
    with open(path, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> int:
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    context = os.environ.get("STATUS_CONTEXT") or "human-review-policy"
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    post_merge_label = os.environ.get("POST_MERGE_LABEL", DEFAULT_POST_MERGE_LABEL).strip()
    target_url = os.environ.get("TARGET_URL")

    def post_status(sha: str, state: str, description: str) -> None:
        status = {"state": state, "context": context, "description": description}
        if target_url:
            status["target_url"] = target_url
        request("POST", f"/repos/{repo}/statuses/{sha}", token, status)

    def evaluate_pr(pr: dict) -> Result:
        reviews = get_all(f"/repos/{repo}/pulls/{pr['number']}/reviews", token)
        return evaluate(pr, reviews, policy_from_env(), write_access(repo, token))

    # Where the status goes if this run fails before it knows better: the
    # commit that triggered it. Never leave an earlier success standing when an
    # evaluation could not finish, e.g. after an approval was dismissed; that
    # includes failing to find the PR, which takes API calls for fork PRs.
    fallback_sha = (
        (event.get("merge_group") or {}).get("head_sha")
        or (event.get("workflow_run") or {}).get("head_sha")
        or ((event.get("pull_request") or {}).get("head") or {}).get("sha")
    )
    try:
        number, group_sha = resolve_target(
            repo, token, event_name, event, os.environ.get("PR_NUMBER", "")
        )
        if number is None:
            print("No pull request for this event; nothing to evaluate.")
            return 0
        # Always re-read the PR: the triggering event may be stale by now, and the
        # status has to land on the head SHA that would actually be merged.
        pr, _ = request("GET", f"/repos/{repo}/pulls/{number}", token)
        fallback_sha = group_sha or pr["head"]["sha"]
        if pr.get("state") != "open":
            if not pr.get("merged_at") or not post_merge_label or group_sha:
                print(f"PR #{number} is closed; nothing to evaluate.")
                return 0
            result = evaluate_pr(pr)
            print(f"PR #{number} (merged): {result.state} - {result.description}")
            write_summary(pr, result)
            track_post_merge(repo, token, pr, result, post_merge_label)
            return 0
        result = evaluate_pr(pr)
    except Exception:
        if fallback_sha:
            post_status(
                fallback_sha, "error", "Could not evaluate review policy; re-run the workflow"
            )
        raise

    sha = group_sha or pr["head"]["sha"]
    where = f"merge group {sha}" if group_sha else f"head {sha}"
    print(f"PR #{number} {where}: {result.state} - {result.description}")
    for login, why in sorted(result.ignored.items()):
        print(f"  not counted: {login} ({why})")
    write_summary(pr, result)
    # The status is the verdict; this step only fails if it could not be reported.
    post_status(sha, result.state, result.description)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as exc:
        print(
            f"::error::GitHub API {exc.code} on {exc.url}: {exc.read().decode(errors='replace')[:500]}"
        )
        sys.exit(1)
