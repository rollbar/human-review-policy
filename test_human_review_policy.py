"""Tests for the human-review-policy approval counting.

Run with: python3 -m unittest
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

import human_review_policy as hrp

HEAD = "h" * 40
OLD = "o" * 40


def user(login: str, kind: str = "User") -> dict:
    return {"login": login, "type": kind}


def bot(login: str) -> dict:
    return user(login, "Bot")


def pr(author: dict | None, head: str = HEAD, number: int = 7, base: str = "main") -> dict:
    return {
        "number": number,
        "state": "open",
        "user": author,
        "head": {"sha": head},
        "base": {"ref": base, "repo": {"default_branch": "main"}},
    }


_ids = iter(range(1, 10_000))


def review(
    who: dict | None, state: str = "APPROVED", commit: str = HEAD, at: str | None = None
) -> dict:
    rid = next(_ids)
    return {
        "id": rid,
        "user": who,
        "state": state,
        "commit_id": commit,
        "submitted_at": at or f"2026-10-18T10:{rid // 60 % 60:02d}:{rid % 60:02d}Z",
    }


ALICE, BOB, CAROL = user("alice"), user("bob"), user("carol")
AGENT = bot("devtools-agent[bot]")
CIRCLE = user("rollbar-circleci-machine")


def run(author, reviews, readers=(), **policy):
    """readers: reviewer logins without write access to the repository."""
    return hrp.evaluate(pr(author), reviews, hrp.Policy(**policy), lambda u: u not in readers)


class HumanAuthoredTest(unittest.TestCase):
    def test_no_reviews_fails(self):
        result = run(ALICE, [])
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.required, 1)
        self.assertIn("0/1", result.description)

    def test_one_other_human_passes(self):
        result = run(ALICE, [review(BOB)])
        self.assertEqual(result.state, "success")
        self.assertEqual(result.approvers, ["bob"])

    def test_author_self_approval_does_not_count(self):
        result = run(ALICE, [review(ALICE)])
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.ignored, {"alice": "PR author"})

    def test_author_match_is_case_insensitive(self):
        result = run(user("Alice"), [review(user("alice"))])
        self.assertEqual(result.state, "failure")

    def test_bot_approval_does_not_count(self):
        result = run(ALICE, [review(AGENT), review(bot("chatgpt-codex-connector[bot]"))])
        self.assertEqual(result.state, "failure")
        self.assertEqual(set(result.ignored.values()), {"bot or service account"})

    def test_service_account_approval_does_not_count(self):
        result = run(ALICE, [review(CIRCLE)])
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.ignored, {"rollbar-circleci-machine": "bot or service account"})

    def test_approval_without_write_access_does_not_count(self):
        result = run(ALICE, [review(user("drive-by"))], readers={"drive-by"})
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.ignored, {"drive-by": "no write access"})

    def test_extra_service_account_from_policy(self):
        result = run(ALICE, [review(BOB)], service_accounts=frozenset({"bob"}))
        self.assertEqual(result.state, "failure")


class BotAuthoredTest(unittest.TestCase):
    def test_one_human_is_not_enough(self):
        result = run(AGENT, [review(ALICE)])
        self.assertTrue(result.author_is_bot)
        self.assertEqual(result.required, 2)
        self.assertEqual(result.state, "failure")
        self.assertIn("1/2", result.description)
        self.assertIn("needs 1 more", result.description)

    def test_two_distinct_humans_pass(self):
        result = run(AGENT, [review(ALICE), review(BOB)])
        self.assertEqual(result.state, "success")
        self.assertEqual(result.approvers, ["alice", "bob"])

    def test_same_human_approving_twice_counts_once(self):
        result = run(AGENT, [review(ALICE), review(ALICE)])
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.approvers, ["alice"])

    def test_maintainer_plus_outsider_is_not_enough(self):
        # Anyone can approve on a public repo; the outsider must not be the second human.
        result = run(AGENT, [review(ALICE), review(user("throwaway"))], readers={"throwaway"})
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.approvers, ["alice"])

    def test_bot_plus_human_is_not_enough(self):
        result = run(AGENT, [review(ALICE), review(bot("other[bot]"))])
        self.assertEqual(result.state, "failure")

    def test_service_account_author_is_treated_as_bot(self):
        result = run(CIRCLE, [review(ALICE)])
        self.assertTrue(result.author_is_bot)
        self.assertEqual(result.state, "failure")

    def test_bot_suffix_without_bot_type(self):
        result = run(user("renovate[bot]"), [review(ALICE)])
        self.assertTrue(result.author_is_bot)

    def test_deleted_author_is_treated_as_bot(self):
        result = run(None, [review(ALICE)])
        self.assertTrue(result.author_is_bot)
        self.assertEqual(result.state, "failure")


class ReviewLifecycleTest(unittest.TestCase):
    def test_dismissed_approval_does_not_count(self):
        result = run(AGENT, [review(ALICE), review(BOB, "DISMISSED")])
        self.assertEqual(result.state, "failure")
        self.assertEqual(result.approvers, ["alice"])

    def test_dismissing_latest_approval_revokes_earlier_one(self):
        result = run(ALICE, [review(BOB), review(BOB, "DISMISSED")])
        self.assertEqual(result.state, "failure")

    def test_changes_requested_after_approval_revokes_it(self):
        result = run(ALICE, [review(BOB), review(BOB, "CHANGES_REQUESTED")])
        self.assertEqual(result.state, "failure")

    def test_approval_after_changes_requested_counts(self):
        result = run(ALICE, [review(BOB, "CHANGES_REQUESTED"), review(BOB)])
        self.assertEqual(result.state, "success")

    def test_comment_after_approval_keeps_it(self):
        result = run(ALICE, [review(BOB), review(BOB, "COMMENTED")])
        self.assertEqual(result.state, "success")

    def test_pending_review_is_ignored(self):
        pending = review(BOB, "PENDING")
        pending["submitted_at"] = None
        self.assertEqual(run(ALICE, [pending]).state, "failure")

    def test_reviews_are_ordered_by_submission_not_list_order(self):
        approve = review(BOB, at="2026-10-18T10:00:00Z")
        dismiss = review(BOB, "CHANGES_REQUESTED", at="2026-10-18T09:00:00Z")
        self.assertEqual(run(ALICE, [approve, dismiss]).state, "success")

    def test_approval_on_earlier_commit_still_counts(self):
        # New commits do not invalidate approvals; only a dismissal does.
        result = run(AGENT, [review(ALICE, commit=OLD), review(BOB)])
        self.assertEqual(result.state, "success")
        self.assertEqual(result.approvers, ["alice", "bob"])

    def test_ghost_reviewer_is_ignored(self):
        self.assertEqual(run(ALICE, [review(None)]).state, "failure")

    def test_thresholds_are_configurable(self):
        self.assertEqual(run(ALICE, [review(BOB)], human_approvals=2).state, "failure")
        self.assertEqual(run(AGENT, [review(BOB), review(CAROL)], bot_approvals=3).state, "failure")

    def test_description_fits_status_limit(self):
        result = run(AGENT, [])
        self.assertLessEqual(len(result.description), 140)


class PolicyFromEnvTest(unittest.TestCase):
    def test_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            policy = hrp.policy_from_env()
        self.assertEqual((policy.human_approvals, policy.bot_approvals), (1, 2))
        self.assertIn("rollbar-circleci-machine", policy.service_accounts)

    def test_overrides_extend_service_accounts(self):
        env = {
            "HUMAN_APPROVALS": "2",
            "BOT_APPROVALS": "3",
            "SERVICE_ACCOUNTS": "Deploy-Bot, other-machine\nthird",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            policy = hrp.policy_from_env()
        self.assertEqual((policy.human_approvals, policy.bot_approvals), (2, 3))
        self.assertTrue(
            {"deploy-bot", "other-machine", "third", "rollbar-circleci-machine"}
            <= policy.service_accounts
        )


class WriteAccessTest(unittest.TestCase):
    def check(self, response):
        with mock.patch.object(hrp, "request", side_effect=[response]) as request:
            allowed = hrp.write_access("rollbar/mox", "t")("Some-User")
        request.assert_called_once_with(
            "GET", "/repos/rollbar/mox/collaborators/Some-User/permission", "t"
        )
        return allowed

    def test_write_maintain_and_admin_may_approve(self):
        for permission in ("write", "maintain", "admin"):
            self.assertTrue(self.check(({"permission": permission}, "")), permission)

    def test_read_and_none_may_not(self):
        # Public repos report "read" for any GitHub account.
        for permission in ("read", "triage", "none", None):
            self.assertFalse(self.check(({"permission": permission}, "")), permission)

    def test_unknown_user_may_not(self):
        error = urllib.error.HTTPError("u", 404, "Not Found", {}, io.BytesIO())
        self.assertFalse(self.check(error))

    def test_other_errors_propagate(self):
        error = urllib.error.HTTPError("u", 403, "Forbidden", {}, io.BytesIO())
        with self.assertRaises(urllib.error.HTTPError):
            self.check(error)


class MainTest(unittest.TestCase):
    """main() against a faked GitHub API: what gets read and what status is written."""

    def setUp(self):
        self.posted = []
        self.head_queries = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def fake_api(self, the_pr, reviews, pulls_for_sha=(), pulls_for_head=(), permissions=None):
        """permissions: login -> repository permission; everyone else has write."""

        def fake_request(method, url, token, body=None):
            if method != "GET":
                self.posted.append((url, body) if method == "POST" else (method, url))
                return {}, ""
            if url.endswith(f"/pulls/{the_pr['number']}"):
                return the_pr, ""
            if url.endswith("/permission"):
                login = url.split("/")[-2]
                return {"permission": (permissions or {}).get(login, "write")}, ""
            if "/reviews" in url:
                return list(reviews), ""
            if "/pulls?state=all&head=" in url:
                self.head_queries.append(url)
                return list(pulls_for_head), ""
            if "/commits/" in url and url.endswith("/pulls"):
                return list(pulls_for_sha), ""
            raise AssertionError(f"unexpected {method} {url}")

        return mock.patch.object(hrp, "request", side_effect=fake_request)

    def run_main(self, event_name, event, **env):
        path = os.path.join(self.tmp.name, "event.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(event, fh)
        base = {
            "GITHUB_TOKEN": "t",
            "GITHUB_REPOSITORY": "rollbar/mox",
            "GITHUB_EVENT_NAME": event_name,
            "GITHUB_EVENT_PATH": path,
        }
        with mock.patch.dict(os.environ, {**base, **env}, clear=True):
            return hrp.main()

    def test_workflow_run_posts_status_on_current_head(self):
        the_pr = pr(AGENT, head=HEAD, number=42)
        event = {"workflow_run": {"head_sha": OLD, "pull_requests": [{"number": 42}]}}
        with self.fake_api(the_pr, [review(ALICE)]):
            self.assertEqual(self.run_main("workflow_run", event, TARGET_URL="https://run"), 0)
        [(url, body)] = self.posted
        self.assertEqual(url, f"/repos/rollbar/mox/statuses/{HEAD}")
        self.assertEqual(body["state"], "failure")
        self.assertEqual(body["context"], "human-review-policy")
        self.assertEqual(body["target_url"], "https://run")

    def test_workflow_run_without_pull_requests_looks_up_by_sha(self):
        the_pr = pr(ALICE, number=9)
        event = {"workflow_run": {"head_sha": HEAD, "pull_requests": []}}
        pulls = [{"number": 8, "state": "closed"}, {"number": 9, "state": "open"}]
        with self.fake_api(the_pr, [review(BOB)], pulls_for_sha=pulls):
            self.run_main("workflow_run", event)
        self.assertEqual(self.posted[0][1]["state"], "success")

    def test_fork_pr_is_found_by_head_branch(self):
        the_pr = pr(user("contributor"), number=11)
        event = {
            "workflow_run": {
                "head_sha": HEAD,
                "pull_requests": [],
                "head_branch": "fix/thing",
                "head_repository": {"owner": {"login": "contributor"}},
            }
        }
        older = {"number": 5, "state": "closed", "head": {"sha": OLD}}
        match = {"number": 11, "state": "open", "head": {"sha": HEAD}}
        with self.fake_api(the_pr, [review(BOB)], pulls_for_head=[older, match]):
            self.run_main("workflow_run", event)
        self.assertEqual(
            self.head_queries,
            ["/repos/rollbar/mox/pulls?state=all&head=contributor%3Afix%2Fthing"],
        )
        self.assertEqual(self.posted[0][0], f"/repos/rollbar/mox/statuses/{HEAD}")
        self.assertEqual(self.posted[0][1]["state"], "success")

    def test_outsider_approval_on_public_repo_does_not_count(self):
        event = {"workflow_run": {"head_sha": HEAD, "pull_requests": [{"number": 42}]}}
        reviews = [review(ALICE), review(user("throwaway"))]
        with self.fake_api(pr(AGENT, number=42), reviews, permissions={"throwaway": "read"}):
            self.run_main("workflow_run", event)
        self.assertEqual(self.posted[0][1]["state"], "failure")

    def test_pull_request_event_and_custom_context(self):
        the_pr = pr(ALICE, number=3)
        with self.fake_api(the_pr, []):
            self.run_main(
                "pull_request_review", {"pull_request": {"number": 3}}, STATUS_CONTEXT="custom"
            )
        self.assertEqual(self.posted[0][1]["context"], "custom")

    def merged(self, author, labels=(), number=3):
        return {
            **pr(author, number=number),
            "state": "closed",
            "merged_at": "2026-10-18T12:00:00Z",
            "labels": [{"name": name} for name in labels],
        }

    def test_merged_without_policy_is_labelled_and_commented(self):
        event = {"workflow_run": {"head_sha": HEAD, "pull_requests": [{"number": 3}]}}
        with self.fake_api(self.merged(AGENT), [review(ALICE)]):
            self.run_main("workflow_run", event)
        (label_url, label_body), (comment_url, comment_body) = self.posted
        self.assertEqual(label_url, "/repos/rollbar/mox/issues/3/labels")
        self.assertEqual(label_body, {"labels": ["needs-post-merge-review"]})
        self.assertEqual(comment_url, "/repos/rollbar/mox/issues/3/comments")
        self.assertIn("1 of the 2", comment_body["body"])
        self.assertIn("post-merge review is required", comment_body["body"])

    def test_already_flagged_merge_is_not_commented_twice(self):
        with self.fake_api(self.merged(AGENT, ["needs-post-merge-review"]), [review(ALICE)]):
            self.run_main("pull_request_review", {"pull_request": {"number": 3}})
        self.assertEqual(self.posted, [])

    def test_post_merge_review_clears_the_label(self):
        the_pr = self.merged(AGENT, ["needs-post-merge-review", "other"])
        with self.fake_api(the_pr, [review(ALICE), review(BOB)]):
            self.run_main("pull_request_review", {"pull_request": {"number": 3}})
        delete, (_, comment_body) = self.posted
        self.assertEqual(
            delete, ("DELETE", "/repos/rollbar/mox/issues/3/labels/needs-post-merge-review")
        )
        self.assertIn("Post-merge review complete", comment_body["body"])
        self.assertIn("@alice, @bob", comment_body["body"])

    def test_merge_into_other_branch_is_not_tracked(self):
        # Stacked PRs and release branches are outside the ruleset.
        the_pr = {
            **self.merged(AGENT),
            "base": {"ref": "feature", "repo": {"default_branch": "main"}},
        }
        with self.fake_api(the_pr, []):
            self.run_main("pull_request", {"pull_request": {"number": 3}})
        self.assertEqual(self.posted, [])

    def test_merged_with_policy_met_is_left_alone(self):
        with self.fake_api(self.merged(ALICE), [review(BOB)]):
            self.run_main("pull_request", {"pull_request": {"number": 3}})
        self.assertEqual(self.posted, [])

    def test_custom_post_merge_label(self):
        with self.fake_api(self.merged(ALICE), []):
            self.run_main(
                "pull_request", {"pull_request": {"number": 3}}, POST_MERGE_LABEL="break-glass"
            )
        self.assertEqual(self.posted[0][1], {"labels": ["break-glass"]})

    def test_empty_post_merge_label_disables_tracking(self):
        with self.fake_api(self.merged(ALICE), []):
            self.run_main("pull_request", {"pull_request": {"number": 3}}, POST_MERGE_LABEL="")
        self.assertEqual(self.posted, [])

    def test_merged_pr_found_by_commit_when_run_lists_no_prs(self):
        event = {"workflow_run": {"head_sha": HEAD, "pull_requests": []}}
        pulls = [
            {"number": 2, "state": "closed", "head": {"sha": OLD}},
            {"number": 3, "state": "closed", "head": {"sha": HEAD}},
        ]
        with self.fake_api(self.merged(ALICE), [], pulls_for_sha=pulls):
            self.run_main("workflow_run", event)
        self.assertEqual(self.posted[0][0], "/repos/rollbar/mox/issues/3/labels")

    def test_closed_unmerged_pr_is_skipped(self):
        the_pr = {**pr(ALICE, number=3), "state": "closed", "merged_at": None}
        with self.fake_api(the_pr, []):
            self.run_main("pull_request", {"pull_request": {"number": 3}})
        self.assertEqual(self.posted, [])

    def test_no_pr_is_a_noop(self):
        event = {"workflow_run": {"head_sha": HEAD, "pull_requests": []}}
        with self.fake_api(pr(ALICE), [], pulls_for_sha=[]):
            self.assertEqual(self.run_main("workflow_run", event), 0)
        self.assertEqual(self.posted, [])

    def test_evaluation_failure_posts_error_instead_of_leaving_old_status(self):
        the_pr = pr(ALICE, number=3)
        with (
            self.fake_api(the_pr, []),
            mock.patch.object(hrp, "get_all", side_effect=RuntimeError("boom")),
        ):
            with self.assertRaises(RuntimeError):
                self.run_main("pull_request", {"pull_request": {"number": 3}})
        self.assertEqual(self.posted[0][1]["state"], "error")

    def test_failure_reading_the_pr_posts_error_on_the_triggering_commit(self):
        event = {"workflow_run": {"head_sha": OLD, "pull_requests": [{"number": 3}]}}

        def fake_request(method, url, token, body=None):
            if method == "POST":
                self.posted.append((url, body))
                return {}, ""
            raise urllib.error.URLError("timed out")

        with mock.patch.object(hrp, "request", side_effect=fake_request):
            with self.assertRaises(urllib.error.URLError):
                self.run_main("workflow_run", event)
        [(url, body)] = self.posted
        self.assertEqual(url, f"/repos/rollbar/mox/statuses/{OLD}")
        self.assertEqual(body["state"], "error")

    def test_failure_finding_a_fork_pr_posts_error_on_the_triggering_commit(self):
        event = {
            "workflow_run": {
                "head_sha": HEAD,
                "pull_requests": [],
                "head_branch": "fix/thing",
                "head_repository": {"owner": {"login": "contributor"}},
            }
        }

        def fake_request(method, url, token, body=None):
            if method == "POST":
                self.posted.append((url, body))
                return {}, ""
            raise urllib.error.HTTPError(url, 403, "rate limited", {}, io.BytesIO())

        with mock.patch.object(hrp, "request", side_effect=fake_request):
            with self.assertRaises(urllib.error.HTTPError):
                self.run_main("workflow_run", event)
        [(url, body)] = self.posted
        self.assertEqual(url, f"/repos/rollbar/mox/statuses/{HEAD}")
        self.assertEqual(body["state"], "error")

    def test_failure_checking_write_access_posts_error(self):
        the_pr = pr(ALICE, number=3)

        def fake_request(method, url, token, body=None):
            if method == "POST":
                self.posted.append((url, body))
                return {}, ""
            if url.endswith("/pulls/3"):
                return the_pr, ""
            if "/reviews" in url:
                return [review(BOB)], ""
            raise urllib.error.HTTPError(url, 403, "rate limited", {}, io.BytesIO())

        with mock.patch.object(hrp, "request", side_effect=fake_request):
            with self.assertRaises(urllib.error.HTTPError):
                self.run_main("pull_request", {"pull_request": {"number": 3}})
        [(url, body)] = self.posted
        self.assertEqual(url, f"/repos/rollbar/mox/statuses/{HEAD}")
        self.assertEqual(body["state"], "error")

    def test_merge_group_reports_on_the_group_commit(self):
        group_sha = "g" * 40
        event = {
            "workflow_run": {
                "event": "merge_group",
                "head_sha": group_sha,
                "head_branch": f"gh-readonly-queue/main/pr-42-{'b' * 40}",
                "pull_requests": [],
            }
        }
        with self.fake_api(pr(AGENT, number=42), [review(ALICE), review(BOB)]):
            self.run_main("workflow_run", event)
        [(url, body)] = self.posted
        self.assertEqual(url, f"/repos/rollbar/mox/statuses/{group_sha}")
        self.assertEqual(body["state"], "success")

    def test_merge_group_event_directly(self):
        group_sha = "g" * 40
        event = {
            "merge_group": {
                "head_sha": group_sha,
                "head_ref": f"refs/heads/gh-readonly-queue/master/pr-42-{'b' * 40}",
            }
        }
        with self.fake_api(pr(AGENT, number=42), [review(ALICE)]):
            self.run_main("merge_group", event)
        [(url, body)] = self.posted
        self.assertEqual(url, f"/repos/rollbar/mox/statuses/{group_sha}")
        self.assertEqual(body["state"], "failure")

    def test_merge_group_without_pr_number_is_a_noop(self):
        event = {"merge_group": {"head_sha": "g" * 40, "head_ref": "refs/heads/elsewhere"}}
        with self.fake_api(pr(AGENT, number=42), []):
            self.assertEqual(self.run_main("merge_group", event), 0)
        self.assertEqual(self.posted, [])


class RequestRetryTest(unittest.TestCase):
    def http_error(self, code):
        return urllib.error.HTTPError("https://api.github.com/x", code, "err", {}, io.BytesIO())

    def ok(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.read.return_value = b'{"ok": true}'
        resp.headers = {}
        return resp

    def test_server_errors_and_timeouts_are_retried(self):
        responses = [self.http_error(502), urllib.error.URLError("timeout"), self.ok()]
        with (
            mock.patch.object(hrp.urllib.request, "urlopen", side_effect=responses) as urlopen,
            mock.patch.object(hrp.time, "sleep") as sleep,
        ):
            body, _ = hrp.request("GET", "/x", "t")
        self.assertEqual(body, {"ok": True})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2, 5])

    def test_client_errors_are_not_retried(self):
        with (
            mock.patch.object(hrp.urllib.request, "urlopen", side_effect=[self.http_error(404)]),
            mock.patch.object(hrp.time, "sleep") as sleep,
        ):
            with self.assertRaises(urllib.error.HTTPError):
                hrp.request("GET", "/x", "t")
        sleep.assert_not_called()

    def test_gives_up_after_the_last_retry(self):
        errors = [self.http_error(503)] * (len(hrp.RETRY_DELAYS) + 1)
        with (
            mock.patch.object(hrp.urllib.request, "urlopen", side_effect=errors),
            mock.patch.object(hrp.time, "sleep"),
        ):
            with self.assertRaises(urllib.error.HTTPError):
                hrp.request("GET", "/x", "t")


if __name__ == "__main__":
    unittest.main()
