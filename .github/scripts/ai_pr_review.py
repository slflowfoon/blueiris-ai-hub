import json
import os
import pathlib
import sys

from ai_common import (
    OpenAIUnavailableError,
    github_request,
    load_text,
    openai_chat_json,
    pr_comment,
    shorten,
)


def main():
    event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    pr = event["pull_request"]
    repo = os.environ["GITHUB_REPOSITORY"]
    pr_number = pr["number"]

    files = github_request(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100") or []
    diff_chunks = []
    for item in files[:40]:
        patch = item.get("patch") or ""
        diff_chunks.append(f"FILE: {item['filename']}\n{patch}")
    diff_text = shorten("\n\n".join(diff_chunks), max_chars=60000)

    system_prompt = load_text(".github/ai/pr_review_system_prompt.md")
    try:
        review = openai_chat_json(
            system_prompt,
            f"PR #{pr_number}: {pr['title']}\n\n{pr.get('body') or ''}\n\nDiff:\n{diff_text}",
        )
    except OpenAIUnavailableError as exc:
        pr_comment(
            repo,
            pr_number,
            "AI review could not complete because the OpenAI API was unavailable "
            f"or rate limited. {exc} Re-run the workflow later if review output is needed.",
        )
        return
    body = review.get("comment_body", "").strip()
    if not body:
        findings = review.get("findings") or []
        summary = review.get("summary", "")
        if findings:
            body = "Findings:\n" + "\n".join(f"- {f}" for f in findings)
            if summary:
                body += f"\n\n{summary}"
        else:
            body = summary or "AI review completed with no findings."

    pr_comment(repo, pr_number, body)


if __name__ == "__main__":
    sys.exit(main())
