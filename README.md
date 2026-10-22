# human-review-policy

Reports a `human-review-policy` commit status on every PR and makes it a required
check on the default branch, so that every production PR has two distinct humans
on it ("four eyes"):

- a **human-authored** PR needs **1** approval from a human other than the author;
- a **bot-authored** PR needs **2** approvals from distinct humans.

This repository is public so that Rollbar's public SDK repositories can use it
as well as the private ones (GitHub doesn't let public repositories call
workflows or actions from a private repository). It holds no secrets: every run
uses the calling repository's own `GITHUB_TOKEN`.

Rollbar's private repositories call it through the wrapper workflow in
`rollbar/github-workflows` (`.github/workflows/human-review-policy.yaml`), which
runs this action; public repositories call this repository's own reusable
workflow, as below.

Logic: [`human_review_policy.py`](human_review_policy.py) (standard library only;
tests: `python3 -m unittest`). Composite action: [`action.yml`](action.yml).
Reusable workflow: [`.github/workflows/human-review-policy.yaml`](.github/workflows/human-review-policy.yaml).

## What counts

An approval counts when the reviewer's **latest** decisive review (approve, request
changes, or a dismissal) is an approval, by someone who is **not**:

| Not counted | How it is recognised |
|---|---|
| The PR author | login match |
| Bots | GitHub `type: Bot` / `<name>[bot]` — every GitHub App (`devtools-agent[bot]`, `dependabot[bot]`, `chatgpt-codex-connector[bot]`, …) |
| Service accounts | machine users are `type: User`, so they are listed: `rollbar-circleci-machine`, `web-flow` and `ghost` built in, plus the `service-accounts` input |
| People without write access | the repository permission API reports less than `write`. Any GitHub account can approve a PR on a public repository, and so can an org member with read access on a private one |

A PR whose author is a bot, a listed service account or a deleted account is
"bot-authored". "Comment" reviews never add or remove an approval. Someone who
pushed commits to the PR can still approve it, and approvals stay valid after
new commits unless they are dismissed (by hand, or by the repository's "dismiss
stale approvals" setting where it is on).

Re-evaluated on: PR opened / reopened / synchronized (new commits) / ready for review /
edited / closed, and review submitted / edited / dismissed. In repositories with a merge
queue, each merge group's commit gets the status too: the queued PR is
re-evaluated and the result written on the merge group commit, so the queue
isn't left waiting for a required status. Transient GitHub API errors are
retried; if an evaluation still fails, the status is set to `error` rather
than leaving an earlier `success` in place. Every evaluation re-reads the PR,
so the status always reflects the state at the time of the last event.

## 1. Add two workflows to the repository

Both files are needed. The first is a no-op that exists only to be the trigger;
the second does the work.

`.github/workflows/human-review-policy-trigger.yaml`:

```yaml
name: Human review policy trigger
# No-op: its completion triggers human-review-policy.yaml, which runs from the
# default branch so that a PR cannot change the policy it is judged by.

on:
  pull_request:
    types: [opened, reopened, synchronize, ready_for_review, edited, closed]
  pull_request_review:
    types: [submitted, edited, dismissed]
  # Repos with a merge queue: the merge group's commit needs the status too.
  merge_group:
    types: [checks_requested]

permissions: {}

jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - run: echo "PR ${{ github.event.pull_request.number }} ${{ github.event_name }}"
```

`.github/workflows/human-review-policy.yaml`:

```yaml
name: Human review policy

on:
  workflow_run:
    workflows: [Human review policy trigger]
    types: [completed]

jobs:
  policy:
    permissions:
      contents: read
      pull-requests: write
      statuses: write
    uses: rollbar/human-review-policy/.github/workflows/human-review-policy.yaml@main
    # with:
    #   service-accounts: some-machine-user, another-one
    #   human-approvals: 1
    #   bot-approvals: 2
    #   post-merge-label: needs-post-merge-review   # "" disables it
```

### Why `workflow_run` and not `pull_request` directly

`pull_request` and `pull_request_review` workflows run the workflow file **from the
PR's own branch**: a PR could edit the evaluator into `exit 0` and pass its own
check. `workflow_run` workflows always run the copy on the **default branch**, so
the policy that judges a PR is the one already merged. If a PR deletes or breaks
the trigger, the status is simply never written or updated and the PR stays
blocked (fail closed).

`workflow_run` only fires once `human-review-policy.yaml` is on the default branch,
so the check starts reporting after the PR adding it is merged.

The reusable workflow always runs the action from this repository's `main`,
whatever ref the caller pins it to (`uses:` cannot take an expression), so
pinning `@<sha>` above does not pin the evaluator.

## 2. Require the check with a ruleset

Only after step 1 is merged and the status shows up on PRs, otherwise every open
PR is blocked. Needs repository admin:

```bash
gh api -X POST repos/rollbar/<repo>/rulesets --input ruleset.json
```

[`ruleset.json`](ruleset.json) targets the
default branch and:

- requires the `human-review-policy` status, **only from GitHub Actions**
  (`integration_id: 15368`), so a status posted with someone's personal token does
  not satisfy it;
- requires a pull request with 1 approval, as a backstop that also gives the
  merge box its usual "review required" state (stale approvals are not
  dismissed, and the last pusher may approve);
- blocks force pushes and deletion of the default branch;
- lets **organization admins and repository admins bypass it, through a pull
  request only** (`bypass_mode: pull_request`). This is the break-glass path for
  incidents: the admin ticks "Merge without waiting for requirements to be met".
  Every bypass is therefore a merged PR, and **a bypassed PR must get its review
  after merge**. Admins cannot push straight to the default branch.

### Post-merge review after a bypass

When a PR is merged into the default branch without meeting the policy (an
admin bypass, or any merge before the ruleset is in place), the workflow labels it
**`needs-post-merge-review`** and comments how many more human approvals it
needs. Approving a merged PR still works (*Files changed* → *Review changes*);
once the approvals meet the policy, the label is removed with a closing comment.
PRs merged into other branches (stacked PRs, release branches) are outside the
ruleset and are not tracked.
Everything still owed across the org:

```
is:pr is:merged label:needs-post-merge-review org:rollbar
```

Only admins should be bypass actors: anyone else on that list would quietly undo
the 1-human / 2-human rule. Classic branch protection on the same branch keeps
applying alongside the ruleset (the stricter rule wins), so check that it doesn't
grant bypass to anyone beyond admins either.

## Known limit: other workflows can write the same status

Any workflow in the repository with `statuses: write` acts as the same GitHub
Actions app. A PR that adds a workflow posting `human-review-policy: success`
would satisfy the check until the next evaluation overwrites it. Closing that
needs the status to come from a dedicated GitHub App whose key only
default-branch workflows can read; until then, protect `.github/workflows/` with
CODEOWNERS + "Require review from Code Owners", and keep GitHub Apps that open
PRs (e.g. `devtools-agent`) without the `workflows` permission, so they cannot
push workflow files at all.

## Pull requests from forks

Public repositories get PRs from forks. That works: the trigger runs in the fork
PR's context, but the evaluation is a `workflow_run` in the base repository with
its own token, and finds the PR by its head branch. If the repository requires
approval before running workflows from first-time contributors, the status
appears once a maintainer approves the trigger run; until then the PR stays
blocked.

## Troubleshooting

- **Status missing on a PR**: the trigger workflow did not run (e.g. the PR was
  opened before step 1 merged). Push a commit, or close and reopen the PR.
- The run summary of each evaluation lists every approval and why it was or was
  not counted.
