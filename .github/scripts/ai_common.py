import json
import os
import pathlib
import textwrap
import time
import urllib.error
import urllib.request


REPO_ROOT = pathlib.Path(os.getcwd())
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
API_RETRY_STATUSES = {429, 500, 502, 503, 504}
API_BACKOFF_SECONDS = [2, 5, 10, 20]


class LLMUnavailableError(RuntimeError):
    pass


def github_request(path, method="GET", data=None):
    token = os.environ["GITHUB_TOKEN"]
    url = f"https://api.github.com{path}"
    body = None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "blueiris-ai-hub-ai-bot",
    }
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=60) as resp:
        raw = resp.read().decode("utf-8")
        if not raw:
            return None
        return json.loads(raw)


def llm_chat_json(system_prompt, user_prompt, schema=None):
    api_key = os.environ["GEMINI_API_KEY"]
    payload = {
        "systemInstruction": {
            "parts": [{"text": system_prompt}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    if schema:
        payload["generationConfig"]["responseSchema"] = schema
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={api_key}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_error = None
    total_attempts = len(API_BACKOFF_SECONDS) + 1
    for attempt, delay in enumerate([0] + API_BACKOFF_SECONDS, start=1):
        if delay:
            print(
                f"Gemini request retrying after {delay}s backoff "
                f"(attempt {attempt}/{total_attempts})"
            )
            time.sleep(delay)
        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(part.get("text", "") for part in parts)
            return json.loads(content)
        except urllib.error.HTTPError as exc:
            last_error = exc
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            if exc.code in API_RETRY_STATUSES and attempt < len(API_BACKOFF_SECONDS) + 1:
                if retry_after:
                    try:
                        retry_delay = max(1, int(retry_after))
                        print(f"Gemini returned {exc.code}; honoring Retry-After={retry_delay}s")
                        time.sleep(retry_delay)
                    except ValueError:
                        pass
                continue
            break
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt < len(API_BACKOFF_SECONDS) + 1:
                continue
            break

    if isinstance(last_error, urllib.error.HTTPError):
        raise LLMUnavailableError(
            f"Gemini API unavailable after {total_attempts} attempts "
            f"(HTTP {last_error.code})."
        ) from last_error
    if isinstance(last_error, urllib.error.URLError):
        raise LLMUnavailableError(
            f"Gemini API unavailable after {total_attempts} attempts "
            f"({type(last_error.reason).__name__ if getattr(last_error, 'reason', None) else 'network error'})."
        ) from last_error
    raise LLMUnavailableError(
        f"Gemini API unavailable after {total_attempts} attempts."
    ) from last_error


def repo_manifest():
    allowed = {".py", ".md", ".yml", ".yaml", ".toml", ".txt"}
    paths = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        if ".git" in path.parts or ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix.lower() not in allowed:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        paths.append(rel)
    return paths


def read_files(paths, max_files=8, max_chars=12000):
    chunks = []
    for rel in paths[:max_files]:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        chunks.append(f"FILE: {rel}\n```\n{content[:max_chars]}\n```")
    return "\n\n".join(chunks)


def load_text(path):
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def write_output(name, value):
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as f:
        f.write(f"{name}<<__EOF__\n{value}\n__EOF__\n")


def write_file(path, content):
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def run(cmd, check=True):
    import subprocess

    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def apply_file_edits(file_edits):
    changed = []
    for item in file_edits:
        path = (item.get("path") or "").strip()
        if not path:
            continue
        target = REPO_ROOT / path
        if not str(target.resolve()).startswith(str(REPO_ROOT.resolve())):
            raise ValueError(f"Refusing to write outside repo: {path}")
        content = item.get("content")
        if content is None:
            raise ValueError(f"Missing content for file edit: {path}")
        write_file(target, content)
        changed.append(path)
    return changed


def git_has_changes():
    result = run(["git", "status", "--porcelain"], check=False)
    return bool(result.stdout.strip())


def issue_comment(repo, issue_number, body):
    github_request(
        f"/repos/{repo}/issues/{issue_number}/comments",
        method="POST",
        data={"body": body},
    )


def pr_comment(repo, pr_number, body):
    github_request(
        f"/repos/{repo}/issues/{pr_number}/comments",
        method="POST",
        data={"body": body},
    )


def shorten(text, max_chars=40000):
    return text if len(text) <= max_chars else text[:max_chars]


def fence(text):
    return textwrap.dedent(text).strip()
