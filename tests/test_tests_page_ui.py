"""Tests page regressions (1.12.1). The browser behaviour is in scripts/e2e_testspage.py (real Chromium, desktop + phone); this runs it when a Playwright Python is available
and always checks the source rules that caused the two bugs."""
import os, re, shutil, subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ST = os.path.join(ROOT, "traffic_tester", "static")
read = lambda f: open(os.path.join(ST, f), encoding="utf-8").read()


def test_list_stands_down_only_for_a_test_page_that_is_still_on_screen():
    """Bug 1: a draft left behind by leaving a test page (Home, Results, the Tests tab) made the list ignore every tap until a reload."""
    nt = read("newtest.js")
    m = re.search(r"export const newTestOpen = \(\) => (.+);", nt)
    assert m and "N.root.isConnected" in m.group(1), "newTestOpen must also require the page to be on screen"
    assert "export function closeNewTest" in nt
    setup = read("setup.js")
    body = setup[setup.index("export async function renderSetup"):setup.index("function listClick")]
    assert "closeNewTest()" in body, "drawing the Tests list must drop a draft left by another tab"
    assert body.index("closeNewTest()") < body.index("bindOnce"), "the draft is dropped before the list takes its taps"


def test_a_test_page_shows_its_own_run_button():
    """Bug 2: the page's Start was hidden by `.tb-src{display:none}` (only the small START in the bar was left) and was not called Run."""
    css = read("instrument.css")
    assert re.search(r"\.tb-src\[data-nt=start\]\s*\{[^}]*display:\s*inline-flex\s*!important", css), "the test page's Start must be shown"
    assert re.search(r"\.tb-src\{display:none!important\}", css), "generator / sniffer keep the bar's START as their only start"
    assert "▶ Run test" in read("newtest.js") and 'data-nt="start"' in read("newtest.js")
    assert "▶ Run test" in read("testset.js")
    assert "position:sticky" in css[css.index(".tsp > .tb-src[data-nt=start]"):][:200]


def _py_with_playwright():
    for py in ("/usr/bin/python3", shutil.which("python3")):
        if py and subprocess.run([py, "-c", "import playwright.sync_api"], capture_output=True).returncode == 0:
            return py
    return None


@pytest.mark.skipif(_py_with_playwright() is None or not os.path.exists(os.path.join(ROOT, "venv/bin/python")), reason="needs Playwright for Python and the venv")
def test_tests_page_in_a_real_browser():
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "e2e-testspage.sh")], capture_output=True, text=True, timeout=900, env=dict(os.environ, PATH=os.path.dirname(_py_with_playwright()) + ":" + os.environ["PATH"]))
    fails = [l for l in r.stdout.splitlines() if "| FAIL |" in l]
    assert r.returncode == 0 and not fails, "\n".join(fails) or r.stdout[-1500:]
