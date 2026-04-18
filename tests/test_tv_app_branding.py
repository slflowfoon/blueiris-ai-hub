from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ANDROID_APP_DIR = REPO_ROOT / "android-tv-overlay" / "app"


def test_tv_app_branding_uses_hub_logo_mark_and_android_identity():
    hub_logo_mark = (REPO_ROOT / "app" / "static" / "logo-mark.svg").read_text()
    build_gradle = (ANDROID_APP_DIR / "build.gradle").read_text()
    manifest = (ANDROID_APP_DIR / "src" / "main" / "AndroidManifest.xml").read_text()
    strings = (ANDROID_APP_DIR / "src" / "main" / "res" / "values" / "strings.xml").read_text()

    assert '<svg width="512" height="512" viewBox="0 0 512 512"' in hub_logo_mark
    assert hub_logo_mark.count("<path") >= 3
    assert hub_logo_mark.count("<circle") >= 3
    assert 'namespace "io.slflowfoon.blueirisaihub.tv"' in build_gradle
    assert 'applicationId "io.slflowfoon.blueirisaihub.tv"' in build_gradle
    assert 'package="io.slflowfoon.blueirisaihub.tv"' in manifest
    assert 'android:authorities="io.slflowfoon.blueirisaihub.tv.fileprovider"' in manifest
    assert "<string name=\"app_name\">Blue Iris AI Hub TV</string>" in strings
    assert "nl.rogro82.pipup" not in build_gradle
    assert "nl.rogro82.pipup" not in manifest
    assert "PiPup" not in strings
    assert "rogro82/pipup" not in strings
