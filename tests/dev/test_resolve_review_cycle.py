"""Resolve must keep iterating until both reviews cover the final PR head."""

import copy
import importlib.util
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "dev/resolve-agent/skills/resolve-drive-pr/review_cycle.py"
)
spec = importlib.util.spec_from_file_location("resolve_review_cycle", SCRIPT)
cycle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cycle)


class Github:
    def __init__(self):
        self.pull = {
            "state": "open",
            "draft": False,
            "head": {"sha": "a" * 40},
            "base": {"repo": {"default_branch": "main"}},
        }
        self.comments = [
            self.comment(
                1,
                "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: "
                + "a" * 40
                + " -->\nNon-blocking: retries can lose data.",
            )
        ]
        self.comments.append(
            self.comment(
                2,
                "<!-- ocr-summary -->\n<!-- ocr-summary-run:10-1 -->\nReview complete.",
                "github-actions[bot]",
            )
        )
        self.reviews = [
            {
                "id": 3,
                "state": "COMMENTED",
                "body": "A race exists.",
                "user": {"login": "maintainer"},
            }
        ]
        self.inline = [
            {
                "id": 4,
                "body": "Optional: closes the wrong connection.",
                "user": {"login": "github-actions[bot]"},
                "commit_id": "a" * 40,
            }
        ]
        self.artifacts = [
            {
                "name": "ocr-completed-7-" + "a" * 40,
                "expired": False,
                "workflow_run": {"id": 10},
            }
        ]
        self.run = {
            "workflow_id": 20,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "status": "completed",
            "conclusion": "success",
        }
        self.calls = []

    @staticmethod
    def comment(id, body, login="omnigent-ci[bot]"):
        return {
            "id": id,
            "body": body,
            "user": {"login": login},
            "html_url": f"https://example.test/comment/{id}",
        }

    def __call__(self, args):
        self.calls.append(args)
        if "POST" in args:
            return None
        path = args[1]
        if path == "repos/o/r/pulls/7":
            return copy.deepcopy(self.pull)
        if path == "repos/o/r":
            return {"default_branch": "main"}
        if "/issues/7/comments?" in path:
            return copy.deepcopy(self.comments)
        if "/pulls/7/reviews?" in path:
            return copy.deepcopy(self.reviews)
        if "/pulls/7/comments?" in path:
            return copy.deepcopy(self.inline)
        if "/pulls/7/commits?" in path:
            return []
        if "/collaborators/" in path:
            return {"permission": "write"}
        if "/actions/artifacts?" in path:
            return {"artifacts": copy.deepcopy(self.artifacts)}
        if "/actions/workflows/open-code-review.yml" in path:
            return {"id": 20}
        if "/actions/runs/10" in path:
            return copy.deepcopy(self.run)
        raise AssertionError(args)


def handoff(state):
    return {
        "outcome": "fixed",
        "remaining_work": [],
        "review_cycle": {
            "head_sha": state["head_sha"],
            "fingerprint": state["fingerprint"],
            "dispositions": [
                {
                    "key": item["key"],
                    "status": "addressed",
                    "reason": "Fixed in the final commit; regression test passes.",
                }
                for item in state["feedback"]
            ],
        },
    }


def test_collects_nonblocking_approved_commented_and_inline_feedback():
    api = Github()
    api.reviews.append(
        {
            "id": 5,
            "state": "APPROVED",
            "body": "Nit: data races.",
            "user": {"login": "maintainer"},
        }
    )
    state = cycle.snapshot("o/r", 7, api)
    assert state["completed"] == {"polly": True, "ocr": True}
    assert {item["key"] for item in state["feedback"]} == {
        "comment:1",
        "comment:2",
        "review:3",
        "review:5",
        "inline:4",
    }
    cycle.validate(state, handoff(state))


@pytest.mark.parametrize(
    "mutation",
    [
        "expired",
        "failed",
        "unfinished",
        "untrusted_branch",
        "wrong_workflow",
        "wrong_head",
        "no_receipt",
    ],
)
def test_ocr_requires_a_successful_trusted_current_head_receipt(mutation):
    api = Github()
    if mutation == "expired":
        api.artifacts[0]["expired"] = True
    elif mutation == "failed":
        api.run["conclusion"] = "failure"
    elif mutation == "unfinished":
        api.run["status"] = "in_progress"
    elif mutation == "untrusted_branch":
        api.run["head_branch"] = "untrusted"
    elif mutation == "wrong_workflow":
        api.run["workflow_id"] = 99
    elif mutation == "wrong_head":
        api.artifacts[0]["name"] = "ocr-completed-7-" + "b" * 40
    else:
        api.artifacts.clear()
    state = cycle.snapshot("o/r", 7, api)
    with pytest.raises(RuntimeError, match="ocr"):
        cycle.validate(state, handoff(state))


@pytest.mark.parametrize(
    "login,body",
    [
        (
            "contributor",
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "a" * 40 + " -->",
        ),
        (
            "unrelated[bot]",
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "a" * 40 + " -->",
        ),
        ("omnigent-ci[bot]", "<!-- polly-skipped-sha: " + "a" * 40 + " -->"),
        (
            "omnigent-ci[bot]",
            "<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: " + "b" * 40 + " -->",
        ),
    ],
)
def test_polly_requires_a_real_current_head_review(login, body):
    api = Github()
    api.comments[0] = api.comment(1, body, login)
    assert not cycle.snapshot("o/r", 7, api)["completed"]["polly"]


@pytest.mark.parametrize(
    "mutation",
    [
        "partial",
        "missing",
        "stale",
        "pending",
        "empty_reason",
        "non_string_reason",
        "duplicate",
        "remaining",
    ],
)
def test_invalid_handoffs_cannot_claim_ready(mutation):
    state = cycle.snapshot("o/r", 7, Github())
    result = handoff(state)
    if mutation == "partial":
        result["outcome"] = "partially_fixed"
    elif mutation == "missing":
        result["review_cycle"]["dispositions"].pop()
    elif mutation == "stale":
        result["review_cycle"]["head_sha"] = "b" * 40
    elif mutation == "pending":
        result["review_cycle"]["dispositions"][0]["status"] = "non_blocking"
    elif mutation == "empty_reason":
        result["review_cycle"]["dispositions"][0]["reason"] = " "
    elif mutation == "non_string_reason":
        result["review_cycle"]["dispositions"][0]["reason"] = ["unstructured"]
    elif mutation == "duplicate":
        result["review_cycle"]["dispositions"].append(result["review_cycle"]["dispositions"][0])
    else:
        result["remaining_work"] = ["Fix the retry race."]
    with pytest.raises(RuntimeError):
        cycle.validate(state, result)


def test_do_not_dispatch_completed_reviews():
    api = Github()
    cycle.request_reviews("o/r", 7, cycle.snapshot("o/r", 7, api), api)
    assert not any("POST" in args for args in api.calls)


def test_snapshot_fails_if_head_changes_during_collection():
    api = Github()
    reads = 0

    def request(args):
        nonlocal reads
        if args[1] == "repos/o/r/pulls/7":
            reads += 1
            if reads == 2:
                api.pull["head"]["sha"] = "b" * 40
        return api(args)

    with pytest.raises(RuntimeError, match="changed while collecting"):
        cycle.snapshot("o/r", 7, request)


def test_paginates_reviews_instead_of_dropping_older_findings():
    calls = []

    def request(args):
        calls.append(args)
        return [{"id": i} for i in range(100)] if args[1].endswith("page=1") else [{"id": 100}]

    assert len(cycle.pages("reviews", request)) == 101
    assert len(calls) == 2


def test_ocr_coverage_is_observed_before_collecting_its_findings():
    api = Github()
    cycle.snapshot("o/r", 7, api)
    paths = [args[1] for args in api.calls]
    receipt = paths.index("repos/o/r/actions/runs/10")
    for suffix in ("issues/7/comments", "pulls/7/comments", "pulls/7/reviews"):
        assert receipt < next(i for i, path in enumerate(paths) if suffix in path)


@pytest.mark.parametrize("change", ["edit", "new_comment", "push", "other_pr"])
def test_live_feedback_changes_invalidate_a_handoff(change):
    api = Github()
    first = cycle.snapshot("o/r", 7, api)
    if change == "edit":
        api.inline[0]["body"] += " Also leaks."
    elif change == "new_comment":
        api.comments.append(api.comment(8, "<!-- ocr-summary -->\nMissed failure."))
    elif change == "push":
        api.pull["head"]["sha"] = "b" * 40
    else:
        first["fingerprint"] = "receipt from a different PR"
    refreshed = cycle.snapshot("o/r", 7, api)
    with pytest.raises(RuntimeError):
        cycle.validate(refreshed, handoff(first))


def test_each_push_requires_both_reviewers_even_after_six_rounds():
    api = Github()
    for round_number in range(1, 9):
        head = f"{round_number:040x}"
        api.pull["head"]["sha"] = head
        pending = cycle.snapshot("o/r", 7, api)
        assert pending["completed"] == {"polly": False, "ocr": False}
        with pytest.raises(RuntimeError, match="missing or incomplete"):
            cycle.validate(pending, handoff(pending))
        api.calls.clear()
        cycle.request_reviews("o/r", 7, pending, api)
        dispatches = [args for args in api.calls if "POST" in args]
        assert len(dispatches) == 2
        assert all("ref=main" in args and "inputs[pr]=7" in args for args in dispatches)
        assert {args[3].split("/")[-2] for args in dispatches} == set(cycle.REVIEWERS.values())
        api.comments.append(
            api.comment(
                100 + round_number,
                f"<!-- polly-review-bot -->\n<!-- polly-reviewed-sha: {head} -->\nNo findings.",
            )
        )
        api.artifacts[0]["name"] = f"ocr-completed-7-{head}"
        complete = cycle.snapshot("o/r", 7, api)
        cycle.validate(complete, handoff(complete))


def test_failed_review_collection_cannot_produce_a_receipt():
    api = Github()

    def request(args):
        if "/pulls/7/comments?" in args[1]:
            raise RuntimeError("GitHub unavailable")
        return api(args)

    with pytest.raises(RuntimeError, match="unavailable"):
        cycle.snapshot("o/r", 7, request)


def test_human_notes_and_dismissed_reviews_still_need_dispositions():
    api = Github()
    api.comments.append(api.comment(8, "Non-blocking: the retry still loses data.", "maintainer"))
    api.comments.append(api.comment(9, "/review", "maintainer"))
    api.comments.append(api.comment(10, "Deploy succeeded.", "github-actions[bot]"))
    api.reviews[0]["state"] = "DISMISSED"
    state = cycle.snapshot("o/r", 7, api)
    keys = {item["key"] for item in state["feedback"]}
    assert "comment:8" in keys
    assert "review:3" in keys
    assert not keys & {"comment:9", "comment:10"}
    result = handoff(state)
    result["review_cycle"]["dispositions"] = [
        item for item in result["review_cycle"]["dispositions"] if item["key"] != "comment:8"
    ]
    with pytest.raises(RuntimeError, match="comment:8"):
        cycle.validate(state, result)


@pytest.mark.parametrize("mutation", ["deleted", "wrong_run", "wrong_author", "wrong_attempt"])
def test_ocr_completion_requires_its_published_summary(mutation):
    api = Github()
    if mutation == "deleted":
        api.comments.pop(1)
    elif mutation == "wrong_run":
        api.comments[1]["body"] = api.comments[1]["body"].replace("10-1", "11-1")
    elif mutation == "wrong_author":
        api.comments[1]["user"]["login"] = "maintainer"
    else:
        api.run["run_attempt"] = 2
    state = cycle.snapshot("o/r", 7, api)
    with pytest.raises(RuntimeError, match="ocr"):
        cycle.validate(state, handoff(state))


def test_handoff_requires_an_explicit_no_remaining_work_verdict():
    state = cycle.snapshot("o/r", 7, Github())
    result = handoff(state)
    del result["remaining_work"]
    with pytest.raises(RuntimeError, match="remaining_work"):
        cycle.validate(state, result)
