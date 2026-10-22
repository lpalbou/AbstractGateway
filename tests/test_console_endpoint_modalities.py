"""Run the shipped picker functions: named endpoints must survive modality catalogs."""
from test_gateway_console_offline import _console_script, _slice_function, _node


def test_named_endpoints_are_offered_and_discovered_by_modality():
    source = _console_script()
    functions = '\n'.join(_slice_function(source, name) for name in (
        'fetchDefaultProviders', 'fetchDefaultModels', 'withQuery', 'providerValueForEndpointProfile'))
    result = _node('''
const state = {endpointProfiles: [
 {id: 'airelays', display_name:'AI Relays', provider_family:'openai-compatible', enabled:true},
 {id:'disabled', provider_family:'openai-compatible', enabled:false}],
 providerModels: new Map(), providerLabels: new Map(), providerStateLabels: new Map()};
const defaultRowKey = row => row.key;
const defaultCatalogForRow = row => ({scope:row.key, providerPath:()=>'/vision/providers',
 modelPath: p => row.key.startsWith('output.image') ? '/vision/provider_models?provider='+p : '/audio/models?provider='+p});
const providerOptionsFromCatalog = p => p.providers;
const catalogProviderStateLabels = () => new Map();
const modelOptionsFromCatalog = p => p.models;
const calls=[];
async function api(path) {calls.push(path);return path==='/vision/providers'?{providers:['huggingface']}:{models:['gpt-image-2']};}
''' + functions + '''
const row={key:'output.image.text_to_image'};
const providers=await fetchDefaultProviders(row);
const models=await fetchDefaultModels('endpoint:airelays',row);
await fetchDefaultModels('endpoint:airelays',{key:'output.voice'});
console.log(JSON.stringify([{providers,models,calls}]));
''')[0]
    assert result['providers'] == ['huggingface', 'endpoint:airelays']
    assert result['models'] == ['gpt-image-2']
    assert result['calls'][-1] == '/api/gateway/discovery/providers/endpoint%3Aairelays/models?capability_route=output.voice'
    assert result['calls'][1] == '/vision/provider_models?provider=endpoint:airelays'
