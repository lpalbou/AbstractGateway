// Exercise the real served HTML and kit islands; every request is intercepted.
// No live gateway, provider, model or machine configuration is touched.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
const [HTML, PW, SHOTS] = process.argv.slice(2);
const require = createRequire(path.join(PW, '/'));
const { chromium } = require('playwright-core');
const html = fs.readFileSync(HTML, 'utf8'), GIB = 1024 ** 3;
const failures = []; let checks = 0;
function check(value, message) { checks++; if (!value) failures.push(message); }
function snapshot(backend = 'metal') {
  return {ts:Date.now()/1000,host:{host_name:'forge.home',host_id:'preview-host'},
    memory:{ram:{used_bytes:59.4*GIB,total_bytes:128*GIB,available_bytes:68.6*GIB,percent:46.4},process:{rss_bytes:4.9*GIB},
      device:backend==='metal'?{backend,allocated_bytes:16.4*GIB,total_bytes:128*GIB,wired_limit_bytes:105.5*GIB}:
      backend==='cuda'?{backend,unified_memory:false,allocated_bytes:2*GIB,process_held_bytes:3*GIB,total_bytes:24*GIB,free_bytes:4*GIB}:{}},
    gpu:{supported:true,source:backend==='cuda'?'nvidia-smi':'ioreg',utilization_gpu_pct:6},
    system:{cpu:{supported:true,utilization_pct:18,logical_cores:16,load_average:[2.1,1.9,1.8]},storage:{supported:true,used_bytes:712*GIB,total_bytes:1024*GIB,free_bytes:312*GIB},swap:{supported:true,used_bytes:0},disk_io:{supported:true,read_bytes_per_s:24*1024**2,write_bytes_per_s:4*1024**2,scope:'all host disks'},network:{supported:true,read_bytes_per_s:3*1024**2,write_bytes_per_s:1024**2,scope:'all host interfaces (includes loopback)'}},
    models:[{provider:'huggingface',model:'Qwen3.8-27B-oQ4e-mtp',resident:true,est_weights_bytes:15.7*GIB,context_length:32768,task:'text_generation',lockable:true,unloadable:true}],
    session_caches:[{session_id:'research',provider:'huggingface',model:'Qwen3.8',bytes:256*1024**2,token_count:12000}],totals:{models:1,models_resident:1,session_caches:1,cache_bytes_models:0},residency_diagnostics:{},degraded:[]};
}
const browser = await chromium.launch();
try {
  for (const theme of ['dark','light']) {
    let current=snapshot(), admin=true, paused=false, unavailable=false, rejectUnload=false;
    const writes=[];
    const ctx=await browser.newContext({viewport:{width:1600,height:1200}});
    await ctx.addInitScript(t=>localStorage.setItem('abstractgateway_ui_settings_v1',JSON.stringify({theme:t})),theme);
    const page=await ctx.newPage();
    const errors=[];page.on('pageerror',e=>errors.push(e.message));
    await page.route('**/*',async route=>{
      const req=route.request(), url=new URL(req.url()), p=url.pathname;
      if(url.hostname!=='resources.test') {failures.push('Unexpected external request: '+url.origin);return route.abort();}
      if(p==='/console')return route.fulfill({contentType:'text/html',body:html});
      let result={ok:true,items:[]};
      if(req.method()==='POST')writes.push({path:p,body:req.postDataJSON()});
      if(p==='/api/gateway/me')result={principal:{admin,user_id:admin?'admin':'reader',tenant_id:'default',roles:[admin?'admin':'user']},auth:{user_auth_enabled:true}};
      if(p.endsWith('/host/state')) {if(unavailable)return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"host probe unavailable"}'});result={...current,ts:Date.now()/1000};}
      if(p.endsWith('/host/runner'))result={ok:true,paused,inflight_ticks:paused?0:2,capabilities:{restart:true,shutdown:true}};
      if(p.endsWith('/host/pause')) {paused=true;result={ok:true,paused:true,inflight_ticks:0};}
      if(p.endsWith('/host/resume')) {paused=false;result={ok:true,paused:false};}
      if(p.endsWith('/host/tray'))result={running:true};
      if(p.endsWith('/host/update'))result={current:'0.18.0',update:{status:'not_checked',line:'0.18.0'}};
      if(p.endsWith('/host/first-run'))result={completed:true,needs_setup:false};
      if(p.endsWith('/discovery/providers'))result={providers:['huggingface']};
      if(p.includes('/discovery/providers/huggingface/models'))result={models:['Qwen3.8-27B-oQ4e-mtp']};
      if(p.endsWith('/models/unload')) {if(rejectUnload)return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"provider busy; retry later"}'});current.models=[];result={ok:true,supported:true};}
      if(p.endsWith('/models/lock')) {current.models[0].locked=true;result={ok:true,supported:true};}
      if(p.endsWith('/models/unlock')) {current.models[0].locked=false;result={ok:true,supported:true};}
      if(p.endsWith('/models/load')) {current.models=snapshot().models;result={ok:true,supported:true};}
      if(p.includes('/prompt_cache/clear_all')) {current.session_caches=[];result={ok:true,available:true};}
      return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
    });
    await page.goto('http://resources.test/console#models');
    await page.waitForSelector('.res-card-value');
    await page.locator('#af-topbar-root button[aria-label="Appearance"]').waitFor();
    check((await page.locator('#res-topology').innerText())==='Unified memory',`${theme}: unified topology`);
    check((await page.locator('#res-capacity').innerText()).includes('Overlaps unified RAM'),`${theme}: overlap scope visible`);
    check((await page.locator('[data-res-card="cpu"]').innerText()).includes('18%'),`${theme}: CPU reading`);
    check((await page.locator('[data-res-card="storage"]').innerText()).includes('712.0 GiB'),`${theme}: data storage reading`);
    check(await page.getByRole('button',{name:'Docs assistant',exact:true}).isVisible(),`${theme}: docs widget preserved`);
    check(await page.getByRole('button',{name:'About AbstractGateway',exact:true}).isVisible(),`${theme}: About preserved`);
    await page.getByRole('button',{name:'Appearance',exact:true}).click();
    check(await page.getByRole('dialog',{name:'Appearance',exact:true}).isVisible(),`${theme}: shared theme chooser opens`);
    await page.keyboard.press('Escape');
    const account=page.locator('#af-topbar-root button').filter({hasText:'admin'});
    await account.click();
    check(await page.getByText('Disconnect',{exact:true}).filter({visible:true}).first().isVisible(),`${theme}: account menu preserved`);
    await page.keyboard.press('Escape');
    await page.getByRole('button',{name:'About System memory',exact:true}).focus();
    await page.locator('.af-tooltip').filter({visible:true}).waitFor();
    check((await page.locator('.af-tooltip:visible').innerText()).includes('OS system RAM'),`${theme}: kit tooltip on keyboard focus`);
    await page.keyboard.press('Escape');
    await page.getByRole('button',{name:'Inspect memory counters',exact:false}).click();
    check(await page.locator('#resources-inspector-backdrop').isVisible(),`${theme}: kit inspector opens`);
    const memory=page.locator('#res-memory-details');
    check((await memory.innerText()).includes('Shares RAM')&&(await memory.innerText()).includes('May overlap'),`${theme}: compact visible scope distinction`);
    check(!(await page.locator('#models-meters').isVisible())&&await memory.locator('p').count()===0,`${theme}: legacy meters and paragraphs removed from the visible inspector`);
    check((await memory.innerText()).trim().split(/\s+/).length<70,`${theme}: visible memory copy stays concise`);
    check(await memory.locator('.res-mem-pool').count()===2&&await memory.locator('.res-mem-facts > div').count()===3,`${theme}: two primary cards and three allocation figures`);
    check((await page.locator('#res-mem-weights-value').innerText()).startsWith('~')&&await page.locator('#res-mem-cache-value').innerText()==='0 B',`${theme}: estimates and reported zero stay explicit`);
    if(SHOTS)await page.screenshot({path:path.join(SHOTS,`memory-inspector-desktop-${theme}.png`),animations:'disabled'});
    await page.getByRole('button',{name:'About model weights',exact:true}).focus();
    delete current.models[0].est_weights_bytes;
    await page.waitForFunction(()=>document.getElementById('res-mem-weights-value').textContent==='—');
    check(await page.getByRole('button',{name:'About model weights',exact:true}).evaluate(el=>el===document.activeElement),`${theme}: polling preserves focused info control`);
    check(await page.locator('#res-mem-model-count').innerText()==='1 model loaded',`${theme}: missing weight does not hide residency or invent zero bytes`);
    await page.locator('#resources-inspector-close').click();
    check(!(await page.locator('#resources-inspector-backdrop').isVisible()),`${theme}: Close dismisses inspector`);
    await page.getByRole('button',{name:'Manage gateway',exact:false}).click();
    check(await page.locator('#gateway-host-update-check').isVisible(),`${theme}: existing update operation in inspector`);
    check(await page.locator('#gateway-host-restart').isVisible(),`${theme}: existing restart operation in inspector`);
    await page.keyboard.press('Escape');
    current=snapshot('cuda');await page.locator('#models-refresh').click();
    await page.waitForFunction(()=>document.getElementById('res-topology').textContent.includes('dedicated'));
    const capacity=await page.locator('#res-capacity').innerText();
    check(capacity.includes('4.0 GiB available')&&capacity.includes('20.0 GiB used')&&!capacity.includes('Overlaps'),`${theme}: independent device-wide VRAM, not process allocation`);
    await page.getByRole('button',{name:'Inspect memory counters',exact:false}).click();
    check(await page.locator('#res-mem-gpu-label').innerText()==='Available VRAM'&&await page.locator('#res-mem-gpu-value').innerText()==='4.0 GiB',`${theme}: compact dedicated memory inspector uses device-wide available VRAM`);
    check(!(await memory.innerText()).includes('Shares RAM'),`${theme}: dedicated pool is not labeled shared`);
    if(SHOTS)await page.screenshot({path:path.join(SHOTS,`memory-inspector-vram-${theme}.png`),animations:'disabled'});
    await page.keyboard.press('Escape');
    if(SHOTS&&theme==='dark'){await page.locator('.shell_content').evaluate(el=>el.scrollTop=0);await page.screenshot({path:path.join(SHOTS,'resources-integrated-vram-dark.png'),animations:'disabled'});}
    current={...current,gpu:{supported:false,reason:'nvidia-smi unavailable'},memory:{...current.memory,device:{backend:null}}};await page.locator('#models-refresh').click();
    await page.waitForFunction(()=>document.querySelector('[data-res-card="gpu"] .res-card-value span').textContent==='—');
    check((await page.locator('[data-res-card="gpu"]').innerText()).includes('nvidia-smi unavailable'),`${theme}: failed probe not zero`);
    check((await page.locator('#res-topology').innerText()).includes('unreported'),`${theme}: unknown topology explicit`);
    current=snapshot();await page.locator('#models-refresh').click();
    await page.waitForFunction(()=>document.getElementById('res-topology').textContent==='Unified memory');
    await page.locator('#models-refresh').click();
    await page.locator('#res-chart').focus();await page.keyboard.press('ArrowLeft');
    check((await page.locator('#res-chart').getAttribute('aria-label')).includes('RAM used:'),`${theme}: keyboard history samples`);
    await page.locator('[data-res-chart="compute"]').click();
    check((await page.locator('#res-chart-caption').innerText()).startsWith('%'),`${theme}: independent percent axis`);
    await page.locator('[data-res-chart="io"]').click();
    check((await page.locator('#res-chart-caption').innerText()).startsWith('MiB/s'),`${theme}: independent I/O axis`);
    await page.locator('[data-res-series="net"]').click();
    check(await page.locator('[data-res-series="net"]').getAttribute('aria-pressed')==='false',`${theme}: series toggle`);
    unavailable=true;await page.locator('#models-refresh').click();
    await page.waitForFunction(()=>document.getElementById('res-freshness').textContent.includes('unavailable'));
    check((await page.locator('[data-res-card="ram"] .res-card-value').innerText()).includes('—'),`${theme}: total snapshot failure removes current figures`);
    check((await page.locator('#res-chart-caption').innerText()).includes('since this page opened'),`${theme}: collection scope retained on failure`);
    await page.getByRole('button',{name:'Inspect memory counters',exact:false}).click();
    check(await page.locator('#res-mem-ram-value').innerText()==='—'&&await page.locator('#res-mem-weights-value').innerText()==='—',`${theme}: unavailable memory is not fabricated zero`);
    await page.keyboard.press('Escape');
    unavailable=false;await page.locator('#models-refresh').click();
    await page.waitForFunction(()=>!document.getElementById('res-freshness').textContent.includes('unavailable'));
    await page.locator('[data-res-chart="memory"]').click();
    for(const width of [320,390,768,1024,1600]) {
      await page.setViewportSize({width,height:width<500?844:1200});
      check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),`${theme}: viewport ${width} no document overflow`);
      check(await page.locator('#res-cards').evaluate(el=>el.scrollWidth<=el.clientWidth+1),`${theme}: viewport ${width} cards fit`);
      if(SHOTS&&(width===390||width===1600)){await page.locator('.shell_content').evaluate(el=>el.scrollTop=0);await page.screenshot({path:path.join(SHOTS,`resources-integrated-${width}-${theme}.png`),animations:'disabled'});}
      if(width===390){
        await page.getByRole('button',{name:'Inspect memory counters',exact:false}).click();
        check(await memory.evaluate(el=>el.scrollWidth<=el.clientWidth+1),`${theme}: mobile memory summary fits`);
        if(SHOTS)await page.screenshot({path:path.join(SHOTS,`memory-inspector-mobile-${theme}.png`),animations:'disabled'});
        await page.keyboard.press('Escape');
      }
    }
    check(errors.length===0,`${theme}: no page exceptions: ${errors.join('; ')}`);
    check(writes.length===0,`${theme}: observation and inspectors issue no mutations`);
    // Existing operations keep the same handlers after the layout changes.
    const models=page.locator('#models-table');
    await models.getByRole('button',{name:'Keep loaded',exact:true}).click();
    await models.getByRole('button',{name:'Kept loaded',exact:true}).waitFor();
    check(writes.some(w=>w.path.endsWith('/models/lock')),`${theme}: keep loaded uses existing lock API`);
    await models.getByRole('button',{name:'Kept loaded',exact:true}).click();
    await models.getByRole('button',{name:'Keep loaded',exact:true}).waitFor();
    await models.getByRole('button',{name:'Unload',exact:true}).click();
    await page.locator('#confirm-cancel').click();
    check(!writes.some(w=>w.path.endsWith('/models/unload')),`${theme}: cancelled unload sends no mutation`);
    rejectUnload=true;
    await models.getByRole('button',{name:'Unload',exact:true}).click();await page.locator('#confirm-ok').click();
    await page.waitForFunction(()=>document.getElementById('models-loaded-message').textContent.includes('provider busy'));
    check(await models.getByRole('button',{name:'Unload',exact:true}).isVisible(),`${theme}: failed unload preserves inventory with recovery reason`);
    rejectUnload=false;
    await models.getByRole('button',{name:'Unload',exact:true}).click();await page.locator('#confirm-ok').click();
    await models.getByRole('button',{name:'Unload',exact:true}).waitFor({state:'hidden'});
    check(current.models.length===0,`${theme}: successful unload reads back new inventory`);
    await page.locator('#models-load-provider').selectOption('huggingface');
    await page.locator('#models-load-model').selectOption('Qwen3.8-27B-oQ4e-mtp');
    await page.locator('#models-load-button').click();
    await models.getByRole('button',{name:'Unload',exact:true}).waitFor();
    check(writes.some(w=>w.path.endsWith('/models/load')),`${theme}: load uses existing operation and refresh`);
    await page.locator('#models-caches-table').getByRole('button',{name:/Clear/}).click();await page.locator('#confirm-ok').click();
    await page.waitForFunction(()=>document.getElementById('models-caches-table').textContent.includes('No session'));
    check(current.models.length===1&&current.session_caches.length===0,`${theme}: cache clear leaves loaded models intact`);
    await page.locator('#gateway-host-pause').click();
    await page.waitForFunction(()=>document.getElementById('gateway-host-pause').getAttribute('aria-checked')==='true');
    check(paused,`${theme}: workflow pause still works`);
    await page.locator('#gateway-host-pause').click();
    await page.waitForFunction(()=>document.getElementById('gateway-host-pause').getAttribute('aria-checked')==='false');
    check(!paused,`${theme}: workflow resume still works`);
    current.models.push({provider:'mlx',model:'configured-model',resident:false});await page.locator('#models-refresh').click();
    await page.locator('#models-show-cached').waitFor();
    check((await page.locator('#models-loaded-title').innerText()).includes('1 resident'),`${theme}: configured model does not inflate residency`);
    await page.locator('#models-show-cached').check();
    check((await models.innerText()).includes('configured — not in memory'),`${theme}: configured model distinction stays visible`);
    admin=false;await page.reload();await page.waitForSelector('.res-card-value');
    check(!(await page.locator('#models-load-form').isVisible())&&!(await page.locator('#gateway-host-pause').isVisible()),`${theme}: read-only account sees monitoring without control mutations`);
    check(await models.getByRole('button',{name:'Unload',exact:true}).count()===0,`${theme}: read-only model rows have no unload action`);
    check(errors.length===0,`${theme}: no operation-flow page exceptions: ${errors.join('; ')}`);
    await ctx.close();
  }
} finally {await browser.close();}
const report={checks,failures};
if(SHOTS)fs.writeFileSync(path.join(SHOTS,'resources-validation.json'),JSON.stringify(report,null,2)+'\n');
console.log(JSON.stringify(report));
