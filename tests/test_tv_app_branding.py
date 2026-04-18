from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ANDROID_APP_DIR = REPO_ROOT / "android-tv-overlay" / "app"


def test_tv_app_uses_blue_iris_ai_hub_identity():
    build_gradle = (ANDROID_APP_DIR / "build.gradle").read_text()
    manifest = (ANDROID_APP_DIR / "src" / "main" / "AndroidManifest.xml").read_text()
    strings = (ANDROID_APP_DIR / "src" / "main" / "res" / "values" / "strings.xml").read_text()

    assert 'namespace "io.slflowfoon.blueirisaihub.tv"' in build_gradle
    assert 'applicationId "io.slflowfoon.blueirisaihub.tv"' in build_gradle
    assert 'package="io.slflowfoon.blueirisaihub.tv"' in manifest
    assert 'android:authorities="io.slflowfoon.blueirisaihub.tv.fileprovider"' in manifest
    assert "<string name=\"app_name\">Blue Iris AI Hub TV</string>" in strings


def test_tv_app_no_longer_mentions_pipup_branding():
    build_gradle = (ANDROID_APP_DIR / "build.gradle").read_text()
    manifest = (ANDROID_APP_DIR / "src" / "main" / "AndroidManifest.xml").read_text()
    strings = (ANDROID_APP_DIR / "src" / "main" / "res" / "values" / "strings.xml").read_text()

    assert "nl.rogro82.pipup" not in build_gradle
    assert "nl.rogro82.pipup" not in manifest
    assert "PiPup" not in strings
    assert "rogro82/pipup" not in strings
