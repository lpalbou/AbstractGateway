"""Real browser coverage for the admin Core endpoint card at three screen sizes."""
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
from node_requirement import require_node
from test_gateway_console_browser_state_toggles import _call, _free_port, _playwright_modules, _start, _stop

pytestmark = pytest.mark.e2e


def test_core_endpoint_console(tmp_path):
    if os.getenv('ABSTRACTGATEWAY_BROWSER_TESTS') != '1':
        pytest.skip('set ABSTRACTGATEWAY_BROWSER_TESTS=1 for Chromium checks')
    port = _free_port()
    home, data = tmp_path / 'home', tmp_path / 'data'
    (home / 'tmp').mkdir(parents=True)
    data.mkdir()
    env = dict(HOME=str(home), TMPDIR=str(home / 'tmp'), PATH=os.environ['PATH'],
               PYTHONPATH=os.pathsep.join([str(Path(__file__).resolve().parents[2] / 'abstractcore'), os.environ.get('PYTHONPATH', '')]), PYTHONUNBUFFERED='1',
               ABSTRACTGATEWAY_DATA_DIR=str(data), ABSTRACTGATEWAY_USER_AUTH='1',
               ABSTRACTGATEWAY_ALLOWED_ORIGINS=f'http://127.0.0.1:{port}',
               PYTHON_KEYRING_BACKEND='keyring.backends.null.Keyring', HF_HUB_OFFLINE='1')
    log = tmp_path / 'gateway.log'
    proc = _start(port, env, log)
    try:
        base = f'http://127.0.0.1:{port}'
        admin = re.findall(r'Gateway admin token: (\S+)$', log.read_text(), flags=re.M)[-1]
        assert _call(base, 'POST', '/host/first-run', admin, {'outcome': 'skipped'})[0] == 200
        assert _call(base, 'POST', '/admin/users', admin, {'user_id': 'alice', 'roles': ['user'], 'token': 'endpoint-browser-user-001'})[0] == 200
        script = Path(__file__).parent / 'browser' / 'core_endpoint.mjs'
        run = subprocess.run([require_node(), str(script), base, admin, str(_playwright_modules()), str(tmp_path)], capture_output=True, text=True, timeout=180)
        assert run.returncode == 0, (run.stdout[-1500:], run.stderr[-3000:])
        assert json.loads(run.stdout.strip().splitlines()[-1])['ok'] is True
    finally:
        _stop(proc)
