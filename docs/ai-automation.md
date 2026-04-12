# AI Automation

This repo can automate two AI-assisted workflows through GitHub Actions:

1. **Issue triage and optional draft fix PR**
2. **PR review comments**

## What it does

### On every new issue

Workflow: `.github/workflows/ai-issue-triage-and-fix.yml`

- reads the issue
- decides whether to:
  - leave a triage comment only, or
  - attempt a narrow automated fix
- if it attempts a fix:
  - generates a patch
  - applies it on a new branch
  - runs tests
  - opens a **draft PR**
  - comments on the issue with the PR link

No AI-generated PR is merged automatically.

### On every PR

Workflow: `.github/workflows/ai-pr-review.yml`

- reads the PR diff
- generates a review comment
- posts findings or a no-findings summary

No PR is merged automatically.

## Required GitHub configuration

Add this repository secret:

- `GEMINI_API_KEY`

Optional repository variable:

- `GEMINI_MODEL`
  - default in workflows: `gemini-2.5-flash`

## Important boundary

The automation may create draft PRs, but you remain the merge gate.

Recommended policy:
- let AI comment or open draft PRs
- keep all merges human-reviewed
