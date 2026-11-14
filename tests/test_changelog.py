import os, subprocess, tempfile
import pytest
from fastapi.testclient import TestClient
from traffic_tester import __version__, changelog as C
from traffic_tester.server import create_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SAMPLE = """# Title

## 1.2.0 — 2026-02-01
- **Bold** thing that
  continues here.
- Second thing

## 1.10.0 - 2026-03-01
- ten

## 1.1.9
- nine, no date
Not a bullet line.
"""


def test_parse_orders_by_version_not_text_and_handles_continuations():
    e = C.parse(SAMPLE)
    assert [x["version"] for x in e] == ["1.10.0", "1.2.0", "1.1.9"]            # 1.10 is newer than 1.9
    assert e[1]["notes"] == ["**Bold** thing that continues here.", "Second thing"] and e[1]["date"] == "2026-02-01"
    assert e[2]["date"] == "" and e[2]["notes"] == ["nine, no date"]


def test_duplicate_version_is_rejected():
    with pytest.raises(ValueError):
        C.parse("## 1.0.0\n- a\n## 1.0.0\n- b\n")


def test_newer_than_and_notes_text():
    e = C.parse(SAMPLE)
    assert [x["version"] for x in C.newer_than(e, "1.2.0")] == ["1.10.0"]
    assert [x["version"] for x in C.newer_than(e, "1.1.0", upto="1.2.0")] == ["1.2.0", "1.1.9"]
    assert C.notes_text(e[2]) == "- nine, no date"


def test_the_real_changelog_covers_every_version_and_this_one():
    e = C.load(ROOT)
    vs = [x["version"] for x in e]
    for v in ("1.0", "1.1.0", "1.1.1", "1.1.2", __version__):
        assert v in vs, f"CHANGELOG.md has no entry for {v}"
    assert all(x["notes"] and x["date"] for x in e)
    assert vs[0] == __version__ or C.entry(e, __version__)


def test_about_endpoint():
    with TestClient(create_app(tempfile.mkdtemp(), "A")) as c:
        d = c.get("/api/about").json()
        assert d["version"] == __version__ and "role" not in d and d["pi"]["name"] and d["hostname"] and d["changelog"][0]["notes"]
        assert "update" in d


def test_publish_refuses_without_a_changelog_entry(tmp_path):
    repo = tmp_path / "r"
    subprocess.run(["rsync", "-a", "--exclude", "venv", "--exclude", ".git", "--exclude", "__pycache__", "--exclude", "*.log*", ROOT + "/", str(repo) + "/"], check=True)
    os.symlink(ROOT + "/venv", repo / "venv")
    env = dict(os.environ, TT_RELEASES_REMOTE=str(tmp_path / "bare.git"), TT_RELEASE_USB="")
    subprocess.run(["git", "init", "-q", "--bare", "-b", "stable", str(tmp_path / "bare.git")], check=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x"], cwd=repo, check=True)
    r = subprocess.run(["bash", "scripts/publish-update.sh", "--no-tests", "--version", "9.9.9"], cwd=repo, env=env, capture_output=True, text=True)
    assert r.returncode == 1 and "no entry" in r.stderr and "9.9.9" in r.stderr
    assert f'__version__ = "{__version__}"' in (repo / "traffic_tester/__init__.py").read_text()       # version untouched
    ls = subprocess.run(["git", "ls-remote", str(tmp_path / "bare.git")], capture_output=True, text=True).stdout
    assert ls.strip() == ""                                                                            # nothing pushed
