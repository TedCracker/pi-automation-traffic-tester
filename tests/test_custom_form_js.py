"""The custom-test form's own logic (validation messages, unit conversion, summary sentence) under Node, using the limits the server serves."""
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from traffic_tester import profiles as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_form_logic_in_node():
    d = tempfile.mkdtemp()
    for f in os.listdir(f"{ROOT}/traffic_tester/static"):
        if f.endswith(".js"):
            shutil.copy(f"{ROOT}/traffic_tester/static/{f}", d)
    shutil.copy(f"{ROOT}/tests/custom_form_logic.mjs", d)
    open(f"{d}/package.json", "w").write('{"type":"module"}')
    r = subprocess.run(["node", "custom_form_logic.mjs", json.dumps(P.form_limits())], cwd=d, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all assertions passed" in r.stdout
