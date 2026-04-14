"""PR review workflow — M0 exit criterion.

Implements the workflow from 02-api.md §6: a real workflow that calls
live agents and performs real GitHub actions on the godel-lang repository.
"""
from pydantic import BaseModel
from godel import workflow, step, retry, WorkflowFail
from godel import print, input   # shadow builtins
from godel.agents import claude_code


class QualityReport(BaseModel):
    passed: bool
    errors: list[str]


class ShaResult(BaseModel):
    sha: str


class PRInfo(BaseModel):
    number: int


class ReviewBatch(BaseModel):
    comments: list[dict]


class Feedback(BaseModel):
    fixes: list[str]
    has_unclear: bool
    comment_ids: list[int]


FEATURE_PROMPT = """\
Create and checkout a new branch called feat/version-helper from the current HEAD.

Then add a version() function to py-library/godel/__init__.py that returns
the package version string by reading it from pyproject.toml using tomllib
(or tomli for Python <3.11). The function should be: def version() -> str.
Also add "version" to __all__.

Commit the change with message "feat: add version() helper" and push the branch.
"""

TEST_PROMPT = """\
Write a test in py-library/tests/test_version.py that imports godel.version
and asserts it returns a string matching a semver-like pattern (digits.digits.digits).
Commit with message "test: add version() test" and push.
"""


@step
@retry(3)
async def quality_gates(engineer):
    await print('[quality_gates] Running quality checks...')
    result = await engineer('Run all quality gates (tests, lint:fix, lint)', schema=QualityReport)
    if not result.passed:
        await print(f'[quality_gates] Failed: {result.errors}')
        await engineer(f'Fix the failing issues: {result.errors}')
        raise WorkflowFail(f'quality gates not green: {result.errors}')
    await print('[quality_gates] All checks passed')


@step
async def wait_for_review(reviewer, pr_number: int, latest_sha: str) -> ReviewBatch:
    await print(f'[wait_for_review] Requesting review on PR #{pr_number} (sha: {latest_sha[:8]})')
    await reviewer(f'Request code review from Copilot on PR #{pr_number}')
    max_polls = 10
    for i in range(max_polls):
        review = await reviewer(
            f'Poll PR #{pr_number} for new reviews on {latest_sha}. '
            f'Check BOTH /pulls/:id/reviews and /pulls/:id/comments for '
            f'unreplied bot comments where commit_id == {latest_sha}',
            schema=ReviewBatch,
        )
        if review.comments:
            await print(f'[wait_for_review] Got {len(review.comments)} comments')
            return review
        await print(f'[wait_for_review] No comments yet, poll {i+1}/{max_polls}...')
    await print('[wait_for_review] Max polls reached, no comments found')
    return ReviewBatch(comments=[])


@step
async def handle_feedback(engineer, comments: list[dict]) -> Feedback:
    await print(f'[handle_feedback] Processing {len(comments)} comments...')
    feedback = await engineer(
        f'Here are the review comments to address: {comments}\n\n'
        'Categorize each comment into: Valid (fix it), OutOfScope (add TODO), '
        'Invalid (won\'t fix), Unclear (escalate). Before marking Invalid, '
        'verify against current code — reviewers see a past commit. Reply to ALL.',
        schema=Feedback,
    )
    if feedback.fixes:
        await print(f'[handle_feedback] Implementing {len(feedback.fixes)} fixes...')
        await engineer(f'Implement these fixes: {feedback.fixes}')
        await quality_gates(engineer)
    else:
        await print('[handle_feedback] No fixes needed')
    return feedback


@workflow
async def pr_review():
    engineer = claude_code(model='sonnet', skip_permissions=True)
    reviewer = claude_code(model='sonnet', skip_permissions=True)
    replied_to: list[int] = []
    pr = None
    try:
        await print('[workflow] Starting PR review workflow')

        await print('[workflow] Step 1: Implementing feature on new branch...')
        await engineer(FEATURE_PROMPT, schema=ShaResult)

        await print('[workflow] Step 2: Writing tests...')
        tests = await engineer(TEST_PROMPT, schema=ShaResult)
        latest_sha = tests.sha
        await print(f'[workflow] Tests committed (sha: {latest_sha[:8]})')

        await print('[workflow] Step 3: Running quality gates...')
        await quality_gates(engineer)

        await print('[workflow] Step 4: Opening draft PR...')
        pr = await engineer(
            'Open a draft PR targeting master with title "feat: add version() helper" '
            'and a short description. Use `gh pr create --draft --base master`.',
            schema=PRInfo,
        )
        await print(f'[workflow] Draft PR #{pr.number} opened')

        while True:
            review = await wait_for_review(reviewer, pr.number, latest_sha)
            if not review.comments:
                break
            fb = await handle_feedback(engineer, review.comments)
            replied_to.extend(fb.comment_ids)
            if fb.has_unclear:
                await input(f'Unclear feedback on PR #{pr.number} needs your decision')
            await print('[workflow] Pushing fixes...')
            push = await engineer('Commit and push fixes', schema=ShaResult)
            latest_sha = push.sha
            await print(f'[workflow] Fixes pushed (sha: {latest_sha[:8]})')

        await print(f'[workflow] PR #{pr.number} is ready for acceptance review')
    finally:
        if pr is not None:
            await print(f'[workflow] Cleaning up — closing draft PR #{pr.number}')
            await engineer(f'Close PR #{pr.number} — this was a test run, do not merge. Use `gh pr close {pr.number}`.')
            await print(f'[workflow] PR #{pr.number} closed')
        await print('[workflow] Done')
