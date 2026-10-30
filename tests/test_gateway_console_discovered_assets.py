"""Typed local catalogue cards preserve cache management without runnable-model claims."""
from test_gateway_console_catalog import _catalog, _run


def asset_rows():
    specs=[('adapter','style',True,True),('component','encoder',True,False),('unknown','opaque',True,False),('model','partial',False,True),('model','metadata',False,False),('model','chat',True,True)]
    rows=[]
    for kind,name,complete,text in specs:
        ref='assets/'+name
        rows.append({'id':'installed:huggingface:'+ref,'display_name':name.title(),'vendor':'assets','family':ref,'params_total':None,'params_active':None,'license':None,'source':'installed','asset_kind':kind,'starter':False,'tags':['cached',kind],
          'capabilities':{'text':text,'embedding':False},'artifacts':[{'provider':'huggingface','artifact':ref,'asset_kind':kind,'quant':None,'quant_class':'unknown','bits':None,'download_bytes':3000,'size_source':'engine','presence':{'status':'installed' if complete else 'absent','location':'cache/'+name},'supported_on_host':True,'downloadable':False,'recommended':False,'fit':{'verdict':'unknown','notes':[]},'inventory':{'present':True,'complete':complete,'kind':kind,'bytes':3000,'files':2,'tasks':[],'tasks_source':'config','incomplete_files':1 if name=='partial' else 0,'missing_shards':2 if name=='partial' else 0}}]})
    rows[0]['artifacts'][0]['inventory']['adapter']={'type':'lora','base_models':['Org/Base-8B'],'tasks':['text_generation'],'trigger_words':['portrait style']}
    return rows


def test_typed_assets_are_native_catalogue_cards_with_honest_actions():
    cat=_catalog(0);cat['rows']=asset_rows()
    installed={'schema':'models_installed_v1','rows':[{'provider':a['provider'],'artifact':a['artifact']} for r in cat['rows'] for a in r['artifacts']],'errors':{}}
    out=_run(cat,'unloadable','#catalog?kind=all&quant=all&fits=0&status=downloaded',installed=installed);html=out['html']
    assert out['initial']['cards']==6 and '6</b> of 6 assets' in html
    assert 'Not in the catalog' not in html
    for name in ['style','encoder','opaque','partial','metadata']:
        card=html.split('data-mc-model="installed:huggingface:assets/'+name+'"')[1].split('</article>')[0]
        assert 'data-mc-action="delete-ask"' in card
        assert 'Use as default text model' not in card and 'data-mc-action="download"' not in card
        assert 'tone-ok' not in card
    adapter=html.split('data-mc-model="installed:huggingface:assets/style"')[1].split('</article>')[0]
    assert '>Adapter</span>' in adapter and '>Needs base</span>' in adapter
    assert 'data-mc-cap="text"' not in adapter
    partial=html.split('data-mc-model="installed:huggingface:assets/partial"')[1].split('</article>')[0]
    assert 'aria-label="Cached files"' in partial and '>Partial</span>' in partial and 'Missing shards' in partial
    chat=html.split('data-mc-model="installed:huggingface:assets/chat"')[1].split('</article>')[0]
    assert 'Use as default text model' in chat


def test_ordinary_downloaded_view_contains_only_full_model_entries():
    cat=_catalog(0);cat['rows']=asset_rows()
    out=_run(cat,'asset-types','#catalog?quant=all&fits=0&status=downloaded',installed={'schema':'models_installed_v1','rows':[],'errors':{}})
    assert out['initial']['cards']==3 and out['initial']['count'][:2]==[3,3]
    assert out['adapters']['cards']==1 and 'kind=adapter' in out['adapters']['hash']
    assert 'Org/Base-8B' in out['adapterHtml'] and 'Trigger words' in out['adapterHtml'] and 'portrait style' in out['adapterHtml']
    assert out['components']['cards']==1 and out['unknown']['cards']==1
    assert out['allAssets']['cards']==6
    assert out['modelsAgain']['cards']==3 and out['modelsAgain']['count'][:2]==[3,3]
    assert out['cleared']['cards']==0  # default 4-bit/fit filters restored, unknown precision stays unknown
    assert 'kind=' not in out['cleared']['hash']


def test_legacy_inventory_fallback_does_not_reintroduce_adapters_or_unknowns():
    cat=_catalog(0)
    installed={'schema':'models_installed_v1','errors':{},'rows':[{'provider':'huggingface','artifact':'assets/'+str(kind),'kind':kind,'deletable':True,'size_bytes':1} for kind in ['adapter','encoder',None]]}
    ordinary=_run(cat,'unloadable','#catalog?quant=all&fits=0&status=downloaded',installed=installed)['html']
    assert 'data-mc-extra-row' not in ordinary
    advanced=_run(cat,'unloadable','#catalog?kind=all&quant=all&fits=0&status=downloaded',installed=installed)['html']
    assert advanced.count('data-mc-extra-row="1"')==3
