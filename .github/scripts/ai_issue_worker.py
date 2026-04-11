import json
import os
import pathlib
import sys

from ai_common import (
    REPO_ROOT,
    apply_patch_text,
    git_has_changes,
    issue_comment,
    load_text,
    openai_chat_json,
    read_files,
    repo_manifest,
    write_file,
    write_output,
)


def main():
    event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    issue = event["issue"]
    repo = os.environ["GITHUB_REPOSITORY"]
    issue_number = issue["number"]
    title = issue["title"]
    body = issue.get("body") or ""

    system_prompt = load_text(".github/ai/issue_system_prompt.md")
    manifest = "\n".join(repo_manifest()[:400])
    triage = openai_chat_json(
        system_prompt,
        f"Issue #{issue_number}: {title}\n\n{body}\n\nRepo manifest:\n{manifest}",
    )

    decision = triage.get("decision", "comment_only")
    comment_body = triage.get("comment_body", "").strip()
    if decision != "attempt_fix":
        issue_comment(repo, issue_number, comment_body or "AI triage completed. No safe automatic fix proposed.")
        write_output("decision", "comment_only")
        return

    relevant_files = triage.get("relevant_files") or []
    inspected = read_files(relevant_files)
    fix_prompt = f"""
Issue #{issue_number}: {title}

Issue body:
{body}

Relevant files:
{inspected}

Return strict JSON with:
- patch: unified diff patch to apply with git apply
- pr_title
- commit_message
- pr_body
- issue_comment

Constraints:
- keep the fix narrow
- edit only relevant files
- do not invent large refactors
- include tests if behavior changes
"""
    fix = openai_chat_json(system_prompt, fix_prompt)
    patch = (fix.get("patch") or "").strip()
    if not patch:
        issue_comment(
            repo,
            issue_number,
            comment_body or "AI triage marked this as fixable, but no patch was generated safely.",
        )
        write_output("decision", "comment_only")
        return

    try:
        apply_patch_text(patch)
    except Exception as exc:
        issue_comment(
            repo,
            issue_number,
            f"{comment_body}\n\nPatch application failed automatically: `{type(exc).__name__}`.",
        )
        write_output("decision", "comment_only")
        return

    if not git_has_changes():
        issue_comment(repo, issue_number, "AI attempted a fix but produced no file changes.")
        write_output("decision", "comment_only")
        return

    out_dir = REPO_ROOT / ".github" / "ai-output"
    out_dir.mkdir(parents=True, exist_ok=True)
    pr_body_path = out_dir / "pr_body.md"
    issue_comment_path = out_dir / "issue_comment.md"
    write_file(pr_body_path, fix.get("pr_body", f"AI-generated draft PR for issue #{issue_number}"))
    write_file(issue_comment_path, fix.get("issue_comment", comment_body or "AI generated a draft PR for review."))

    write_output("decision", "attempt_fix")
    write_output("pr_title", fix.get("pr_title", f"Fix issue #{issue_number}: {title}")[:240])
    write_output("commit_message", fix.get("commit_message", f"Fix issue #{issue_number}")[:240])
    write_output("pr_body_file", str(pr_body_path))
    write_output("issue_comment_file", str(issue_comment_path))


if __name__ == "__main__":
    sys.exit(main())
