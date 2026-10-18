"""Named automation snapshots survive HTTP persistence and reach real Agent effects."""
from test_iphone_assistant_tool_declarations import assistant_gateway, wait_for


def test_create_revise_snapshot_reaches_child_schemas_and_approval(assistant_gateway):
    client, calls, behavior = assistant_gateway
    policy = {"auto_approve_tools": ["list_files"], "require_approval_tools": ["read_file"]}
    response = client.post('/api/gateway/automations', json={
        'request_id': 'named-tools', 'title': 'Named tools',
        'target': {'bundle_ref': 'abstractassistant-orchestrator@0.0.0', 'flow_id': 'd5d4e5a1',
                   'input_data': {'prompt': 'Inspect', 'tools': ['read_file', 'list_files'],
                                  '_runtime': {'allowed_tools': ['read_file', 'list_files']}}},
        'trigger': {'source_id': 'manual', 'source_version': 1, 'config': {}},
        'policy': {'tool_approval': 'auto', 'tool_policy': policy}})
    assert response.status_code == 200, response.text
    aid = response.json()['automation_id']
    definition = client.get(f'/api/gateway/automations/{aid}').json()['definition']
    assert definition['policy']['tool_policy'] == policy
    behavior['request_read'] = True
    run = client.post(f'/api/gateway/automations/{aid}/commands', json={
        'command_id': 'first', 'type': 'automation.run_now'})
    assert run.status_code == 200, run.text
    wait_for(lambda: calls)
    schemas = {t['name']: t for t in calls[0]['payload']['tools']}
    assert set(schemas) == {'read_file', 'list_files'}
    assert schemas['read_file']['parameters']['file_path']['type'] == 'string'
    compiled = calls[0]['runtime']['tool_policy']
    assert compiled['auto_approve_tools'] == ['list_files']
    assert compiled['require_approval_tools'] == ['read_file']
    child = calls[0]['run_id']
    waiting = wait_for(lambda: (v if (v := client.get(f'/api/gateway/runs/{child}').json()).get('status') == 'waiting' else None))
    assert 'tool_approval' in str(waiting['waiting'])
    # Revise uses the existing definition command; the next occurrence freezes
    # the replacement snapshot, including an explicit deny-all tool ceiling.
    target = definition['target']
    target.pop('workflow_id', None)
    target['input_data']['tools'] = []
    target['input_data']['_runtime']['allowed_tools'] = []
    revised = client.patch(f'/api/gateway/automations/{aid}', json={
        'command_id': 'revise', 'expected_revision': definition['revision'],
        'changes': {'target': target, 'policy': {'tool_policy': {'auto_approve_tools': [], 'require_approval_tools': []}}}})
    assert revised.status_code == 200, revised.text
    saved = wait_for(lambda: (d if (d := client.get(f'/api/gateway/automations/{aid}').json()['definition'])['revision'] > definition['revision'] else None))
    assert saved['target']['input_data']['_runtime']['allowed_tools'] == []
    assert saved['policy']['tool_policy']['auto_approve_tools'] == []
    behavior['request_read'] = False
    resumed = client.post('/api/gateway/commands', json={
        'command_id': 'approve', 'run_id': child, 'type': 'resume',
        'payload': {'wait_key': waiting['waiting']['wait_key'], 'payload': {'approved': True}}})
    assert resumed.status_code == 200, resumed.text
    wait_for(lambda: client.get(f'/api/gateway/automations/{aid}/occurrences').json()['items'][0]['status'] == 'completed')
    count = len(calls)
    launched = client.post(f'/api/gateway/automations/{aid}/commands', json={
        'command_id': 'second', 'type': 'automation.run_now'})
    assert launched.status_code == 200, launched.text
    wait_for(lambda: len(calls) > count)
    assert calls[count]['payload'].get('tools', []) == []
    assert calls[count]['runtime']['allowed_tools'] == []
    assert calls[count]['runtime']['tool_policy']['auto_approve_tools'] == []


def test_named_auto_overrides_legacy_ask_and_executes_real_tool(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from automations_fixtures import HEADERS, gateway_env, write_shell_agent_bundle, wait_until
    from test_automations_tool_approval import _create, _run_now, _occurrence
    gateway_env(monkeypatch, tmp_path, runner=True)
    write_shell_agent_bundle(tmp_path / 'bundles')
    from abstractgateway.app import app
    with TestClient(app) as client:
        aid = _create(client, 'named-auto', policy={'tool_approval': 'ask', 'tool_policy': {
            'auto_approve_tools': ['execute_command'], 'require_approval_tools': ['read_file']}})
        _run_now(client, aid)
        row = wait_until(lambda: (o if (o := _occurrence(client, aid)) and o['status'] in ('completed', 'failed', 'waiting') else None))
        assert row['status'] == 'completed', row
        records = client.get(f"/api/gateway/runs/{row['run_id']}/ledger?after=0&limit=500", headers=HEADERS).json()['items']
        assert any('automation-shell-ok' in str(r.get('result')) for r in records)
