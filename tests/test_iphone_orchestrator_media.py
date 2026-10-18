"""HTTP → actual orchestrator → real LLM handler/normalizer → artifact HTTP.

The provider generate seam alone is deterministic. 0.0.0 is the wheel asset;
0.0.12 is the additive canonical revision6 bundle. Both are mandatory assets.
"""
import base64
import json
import io
import wave
from pathlib import Path
import shutil
import tempfile
import zipfile

import pytest
from fastapi.testclient import TestClient
from test_iphone_assistant_tool_declarations import wait_for

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII=')


def wav_bytes():
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
        audio.writeframes(b'\0\0' * 24000)
    return buffer.getvalue()

WAV = wav_bytes()

@pytest.fixture(params=['0.0.0', '0.0.12'])
def media_gateway(request, tmp_path, monkeypatch):
    from abstractruntime.integrations.abstractcore import factory
    from abstractruntime.integrations.abstractcore.llm_client import _normalize_multimodal_response, _output_runtime_metadata, _resolve_media_artifacts
    from abstractcore.core.multimodal_generation import GeneratedItem, MultimodalGenerateResponse
    version = request.param
    source = Path(__file__).resolve().parents[1] / 'flows' / 'bundles' / f'abstractassistant-orchestrator@{version}.flow'
    assert source.exists(), f'Missing mandatory shipped bundle {version}'
    with zipfile.ZipFile(source) as archive:
        manifest = json.loads(archive.read('manifest.json'))
    flow = manifest['entrypoints'][0]['flow_id']
    calls, behavior = [], {'mode': 'image', 'fail': False}
    original = factory.build_effect_handlers
    def handlers(*, llm, tools, artifact_store=None, run_store=None):
        class Provider:
            def generate(self, **kwargs):
                calls.append(json.loads(json.dumps(kwargs, default=str)))
                output = kwargs.get('params', {}).get('output')
                if output is None:
                    data = {'mode': behavior['mode'], 'media_prompt': 'A red dot', 'assistant_message': 'Here is the result.', 'seconds': 3}
                    return {'content': json.dumps(data), 'data': data, 'tool_calls': [], 'finish_reason': 'stop'}
                if kwargs.get('media'):
                    with tempfile.TemporaryDirectory() as directory:
                        resolved = _resolve_media_artifacts(kwargs['media'], artifact_store=artifact_store, temp_dir=directory)
                        assert any(Path(item['file_path']).read_bytes() == PNG for item in resolved if isinstance(item, dict) and item.get('file_path'))
                if behavior['fail']:
                    raise RuntimeError('Media provider is not configured')
                spec = output[0] if isinstance(output, list) else output
                modality = spec['modality']
                run_id, tags = _output_runtime_metadata(output)
                content_type = 'image/png' if modality == 'image' else 'audio/wav'
                generated = GeneratedItem(modality=modality, task=spec.get('task', ''), data=PNG if modality == 'image' else WAV,
                    content_type=content_type, format='png' if modality == 'image' else 'wav', provider='test', model='deterministic')
                return _normalize_multimodal_response(MultimodalGenerateResponse(outputs={modality: [generated]}),
                    artifact_store=artifact_store, run_id=run_id, default_tags=tags)
        return original(llm=Provider(), tools=tools, artifact_store=artifact_store, run_store=run_store)
    monkeypatch.setattr(factory, 'build_effect_handlers', handlers)
    bundles = tmp_path / 'bundles'; bundles.mkdir(); shutil.copyfile(source, bundles / source.name)
    for key, value in {'ABSTRACTGATEWAY_DATA_DIR': str(tmp_path / 'runtime'), 'ABSTRACTGATEWAY_FLOWS_DIR': str(bundles),
        'ABSTRACTGATEWAY_WORKFLOW_SOURCE': 'bundle', 'ABSTRACTGATEWAY_AUTH_TOKEN': 'test-media',
        'ABSTRACTGATEWAY_ALLOWED_ORIGINS': '*', 'ABSTRACTGATEWAY_POLL_S': '0.02', 'ABSTRACTGATEWAY_TICK_WORKERS': '1'}.items():
        monkeypatch.setenv(key, value)
    from abstractgateway.app import app
    with TestClient(app, headers={'Authorization': 'Bearer test-media'}) as client:
        yield client, calls, behavior, version, flow


def start(env, **inputs):
    client, _, _, version, flow = env
    response = client.post('/api/gateway/runs/start', json={'bundle_id': 'abstractassistant-orchestrator',
        'bundle_version': version, 'flow_id': flow, 'session_id': 'phone-media', 'input_data': {'prompt': 'Draw or edit a red dot', **inputs}})
    assert response.status_code == 200, response.text
    run_id = response.json()['run_id']
    final = wait_for(lambda: (r if (r := client.get(f'/api/gateway/runs/{run_id}').json()).get('status') in ('completed', 'failed') else None))
    return run_id, final


def assert_artifact(client, run_id, *, expected=PNG, mime='image/png'):
    rows = client.get('/api/gateway/sessions/phone-media/artifacts').json()['items']
    generated = [r for r in rows if r['run_id'] == run_id and r.get('tags', {}).get('kind') == 'generated_media']
    assert len(generated) == 1, rows
    row = generated[0]
    response = client.get(f"/api/gateway/runs/{run_id}/artifacts/{row['artifact_id']}/content")
    assert response.status_code == 200 and response.content == expected
    assert row['content_type'] == mime


def test_text_to_image_stores_and_serves_session_artifact(media_gateway):
    client, calls, _, _, _ = media_gateway
    run_id, final = start(media_gateway)
    assert final['status'] == 'completed', final
    assert calls[-1]['params']['output']['modality'] == 'image'
    assert final['output']['artifact']['$artifact']
    assert_artifact(client, run_id)


def test_uploaded_image_is_the_edit_source_not_just_router_text(media_gateway):
    client, calls, behavior, _, _ = media_gateway
    upload = client.post('/api/gateway/attachments/upload', data={'session_id': 'phone-media', 'filename': 'source.png', 'content_type': 'image/png'}, files={'file': ('source.png', PNG, 'image/png')})
    assert upload.status_code == 200, upload.text
    body = upload.json(); ref = body.get('artifact') or body.get('attachment')
    assert ref, body
    behavior['mode'] = 'edit_image'
    run_id, final = start(media_gateway, attachments=[ref], primary_image_artifact=ref, has_primary_image_context=True)
    assert final['status'] == 'completed', final
    assert 'Primary image available: True' in json.dumps(calls[0]) or 'Primary image available: true' in json.dumps(calls[0])
    assert ref['$artifact'] in json.dumps(calls[-1]['media'])
    assert calls[-1]['params']['output']['task'] in ('image_edit', 'image_to_image')
    assert final['output']['artifact']['$artifact']
    assert_artifact(client, run_id)


@pytest.mark.parametrize('mode', ['music', 'sound'])
def test_audio_duration_contract_matches_actual_selected_bundle(media_gateway, mode):
    client, calls, behavior, version, _ = media_gateway
    behavior['mode'] = mode
    run_id, final = start(media_gateway)
    assert final['status'] == 'completed', final
    spec = calls[-1]['params']['output']
    if version == '0.0.0':
        # Explicit regression evidence: the wheel bundle discards requested seconds
        # and its SFX branch requests generic audio, unlike the current source graph.
        assert spec.get('duration_s') != 3
        if mode == 'sound': assert spec['modality'] == 'voice'  # audio alias normalizes to voice, not SFX
    else:
        assert spec['duration_s'] == 3
        assert spec['modality'] == 'music'
        assert spec['task'] == ('text_to_audio' if mode == 'sound' else 'music_generation')
    if mode == 'sound':
        assert final['output']['outputs'][spec['modality']][0]['artifact_ref']['$artifact']
    else:
        assert final['output']['audio_artifact']['$artifact']
    assert_artifact(client, run_id, expected=WAV, mime='audio/wav')


def test_missing_provider_fails_run_instead_of_reporting_router_confirmation(media_gateway):
    _, calls, behavior, _, _ = media_gateway
    behavior['fail'] = True
    _, final = start(media_gateway)
    assert calls[-1]['params']['output']['modality'] == 'image'
    assert final['status'] == 'failed', final
    assert 'not configured' in str(final.get('error'))


def test_explicit_voice_route_stores_spoken_audio(media_gateway):
    client, calls, behavior, version, _ = media_gateway
    if version == '0.0.0':
        # The immutable legacy graph never advertised a voice mode.
        with zipfile.ZipFile(Path(__file__).resolve().parents[1] / 'flows/bundles/abstractassistant-orchestrator@0.0.0.flow') as archive:
            graph = json.loads(archive.read('flows/d5d4e5a1.json'))
        assert not any(n['id'] == 'generate_voice' for n in graph['nodes'])
        return
    behavior['mode'] = 'voice'
    run_id, final = start(media_gateway)
    assert final['status'] == 'completed', final
    spec = calls[-1]['params']['output']
    assert spec['modality'] == 'voice' and spec['task'] == 'tts'
    assert 'A red dot' in json.dumps(calls[-1]['messages']) or calls[-1]['prompt'] == 'A red dot'
    assert final['output']['audio_artifact']['$artifact']
    assert_artifact(client, run_id, expected=WAV, mime='audio/wav')


def test_bundled_revision6_is_reproducible_and_contains_closed_subflows():
    import importlib.util
    script = Path(__file__).resolve().parents[1] / 'scripts/build_assistant_orchestrator_bundle.py'
    spec = importlib.util.spec_from_file_location('assistant_bundle_builder', script)
    builder = importlib.util.module_from_spec(spec); spec.loader.exec_module(builder)
    artifact = script.parents[1] / 'flows/bundles/abstractassistant-orchestrator@0.0.12.flow'
    assert builder.bundle_bytes() == builder.bundle_bytes() == artifact.read_bytes()
    with zipfile.ZipFile(artifact) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        assert manifest['metadata']['managed_workflow_revision'] == 6
        flows = {fid: json.loads(archive.read(path)) for fid, path in manifest['flows'].items()}
    root = flows[manifest['default_entrypoint']]
    assert 'workflow-revision=6' in root['description']
    for flow in flows.values():
        for node in flow['nodes']:
            if node['type'] == 'subflow': assert node['data']['subflowId'] in flows
    router = next(n for n in root['nodes'] if n['id'] == 'route_call')['data']['pinDefaults']
    assert 'voice' in router['resp_schema']['properties']['mode']['enum']
    assert 'exact words to speak' in router['system']


def test_missing_image_source_fails_instead_of_generating_unrelated_image(media_gateway):
    client, calls, behavior, version, flow = media_gateway
    behavior['mode'] = 'edit_image'
    response = client.post('/api/gateway/runs/start', json={
        'bundle_id': 'abstractassistant-orchestrator', 'bundle_version': version, 'flow_id': flow,
        'session_id': 'phone-media', 'input_data': {'prompt': 'Edit this image', 'has_primary_image_context': True,
            'primary_image_artifact': {'$artifact': 'missing-source', 'run_id': 'phone-media', 'filename': 'missing.png', 'content_type': 'image/png'}}})
    # Gateway rejects missing sources before any provider/router can run.
    assert response.status_code == 404, response.text
    assert 'missing-source' in response.text
    assert calls == []


def test_versionless_account_choices_survive_entrypoint_upgrade(media_gateway):
    client, calls, _, version, _ = media_gateway
    if version == '0.0.0':
        return  # This immutable bundle predates the desktop catalog entrypoint.
    interface = 'abstractassistant.agent.v1'
    for flow in ('d5d4e5a1', 'c53b1579'):
        saved = client.put('/api/gateway/accounts/me/preferences', json={
            'default_workflow': {interface: f'abstractassistant-orchestrator:{flow}'}})
        assert saved.status_code == 200, saved.text
        preferences = client.get('/api/gateway/accounts/me/preferences').json()
        effective = next(row for row in preferences['apps'] if row['interface'] == interface)['effective']
        assert effective['available'] is True and effective['flow_id'] == flow
        assert effective['bundle_version'] == '0.0.12'
        # Account preferences are resolved by thin clients; @default means the
        # gateway-wide setting, not this account's client preference.
        response = client.post('/api/gateway/runs/start', json={
            'bundle_id': effective['bundle_id'], 'bundle_version': effective['bundle_version'],
            'flow_id': effective['flow_id'], 'session_id': 'phone-media',
            'input_data': {'prompt': 'Draw a red dot'}})
        assert response.status_code == 200, response.text
        run_id = response.json()['run_id']
        final = wait_for(lambda: (r if (r := client.get(f'/api/gateway/runs/{run_id}').json()).get('status') in ('completed', 'failed') else None))
        assert final['status'] == 'completed', final
        assert final['workflow_id'] == f'abstractassistant-orchestrator@0.0.12:{flow}'


def test_packaged_chat_subflow_still_runs(media_gateway):
    _, calls, behavior, _, _ = media_gateway
    behavior['mode'] = 'chat'
    _, final = start(media_gateway, tools=[])
    assert final['status'] == 'completed', final
    assert len(calls) == 2, 'Router and real embedded agent must both reach the provider'
    assert all(call.get('params', {}).get('output') is None for call in calls)
    assert final['output']['response']
