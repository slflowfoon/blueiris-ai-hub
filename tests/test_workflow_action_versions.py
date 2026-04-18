from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_node24_ready_action_versions_are_pinned():
    build_apk = (REPO_ROOT / ".github" / "workflows" / "build-apk.yml").read_text()
    ci_cd = (REPO_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()

    assert "actions/checkout@v6" in build_apk
    assert "actions/setup-java@v5" in build_apk
    assert "actions/cache@v5" in build_apk
    assert "actions/upload-artifact@v6" in build_apk
    assert "softprops/action-gh-release@v3" in build_apk

    assert "actions/checkout@v6" in ci_cd
