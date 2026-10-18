"""Host-default and explicit endpoint routes reach router and chat child.

Only pooled provider transport is replaced. HTTP auth, principal host lookup,
shipped graph compilation and the real LLM effect's profile resolution remain.
"""
import json
from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient
from test_iphone_assistant_tool_declarations import wait_for


@pytest.mark.parametrize('scope', ['user', 'gateway'])
def test_shared_endpoint_resolves_for_router_and_embedded_agent(tmp_path, monkeypatch, scope):
    from abstractruntime.integrations.abstractcore.llm_client import MultiLocalAbstractCoreLLMClient
    from abstractgateway.provider_endpoint_profiles import ProviderEndpointProfileStore
    from abstractgateway.users import GatewayUserRegistry

    calls = []

    def generate(self, **kwargs):
        params = kwargs.get('params', {})
        calls.append(dict(params))
        # Fail here if the actual handler ever hands transport an unresolved
        # virtual provider; returning canned success would hide the regression.
        assert params.get('_provider', self._default_provider) == 'openai-compatible'
        assert params.get('_model', self._default_model) == 'phone-test-model'
        assert params.get('base_url', self._llm_kwargs.get('base_url')) == 'https://provider.example/v1'
        assert params.get('provider_api_key', self._llm_kwargs.get('api_key')) == 'fixture-secret'
        if len(calls) == 1:
            data = {'mode': 'chat', 'media_prompt': '', 'assistant_message': ''}
            return {'content': json.dumps(data), 'data': data, 'tool_calls': []}
        return {'content': 'Endpoint routing works.', 'tool_calls': [], 'finish_reason': 'stop'}

    monkeypatch.setattr(MultiLocalAbstractCoreLLMClient, 'generate', generate)
    bundles = tmp_path / 'bundles'
    bundles.mkdir()
    source = Path(__file__).resolve().parents[1] / 'flows/bundles/abstractassistant-orchestrator@0.0.12.flow'
    shutil.copyfile(source, bundles / source.name)
    for key, value in {
        'ABSTRACTGATEWAY_DATA_DIR': str(tmp_path / 'runtime'),
        'ABSTRACTGATEWAY_FLOWS_DIR': str(bundles),
        'ABSTRACTGATEWAY_WORKFLOW_SOURCE': 'bundle',
        'ABSTRACTGATEWAY_AUTH_TOKEN': 'admin-fixture',
        'ABSTRACTGATEWAY_USER_AUTH': '1',
        'ABSTRACTGATEWAY_PROVIDER': 'endpoint:airelays',
        'ABSTRACTGATEWAY_MODEL': 'phone-test-model',
        'ABSTRACTGATEWAY_RUNNER': '1',
        'ABSTRACTGATEWAY_POLL_S': '0.02',
        'ABSTRACTGATEWAY_TICK_WORKERS': '1',
    }.items():
        monkeypatch.setenv(key, value)
    ProviderEndpointProfileStore(base_dir=tmp_path / 'runtime').upsert_profile(
            profile_id='airelays', provider_family='openai-compatible',
            base_url='https://provider.example/v1', api_key='fixture-secret', scope=scope)
    from abstractgateway.app import app
    with TestClient(app) as client:
        _, token = GatewayUserRegistry().create_user(user_id='phone-user', roles=['user'])
        client.headers['Authorization'] = f'Bearer {token}'
        inputs = {'prompt': 'Hello', 'tools': []}
        # Router has no model input edges and therefore uses the host default;
        # the embedded agent receives the same selected route explicitly.
        inputs.update(provider='endpoint:airelays', model='phone-test-model',
                      _runtime={'provider': 'endpoint:airelays', 'model': 'phone-test-model', 'allowed_tools': []})
        response = client.post('/api/gateway/runs/start', json={
            'bundle_id': 'abstractassistant-orchestrator', 'bundle_version': '0.0.12',
            'flow_id': 'c53b1579', 'session_id': 'phone-endpoint-routing',
            'input_data': inputs,
        })
        assert response.status_code == 200, response.text
        root = response.json()['run_id']
        final = wait_for(lambda: (r if (r := client.get(f'/api/gateway/runs/{root}').json()).get('status') in ('completed', 'failed') else None))
        assert final['status'] == 'completed', final
        assert final['output']['response'] == 'Endpoint routing works.'
        assert len(calls) == 2, 'Both router and real embedded agent must reach resolved provider transport'
        assert calls[0]['trace_metadata']['run_id'] == root
        assert calls[1]['trace_metadata']['run_id'] != root
        assert 'fixture-secret' not in json.dumps(final)
