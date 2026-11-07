"""The HTML entity chat delegates field/keyboard UI to the shared composer island."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
from node_requirement import require_node

from abstractgateway import console_islands_sync
from abstractgateway.console import gateway_console_html
from abstractgateway.console_islands import ISLANDS_JS, ISLANDS_PROVENANCE
from test_gateway_console_offline import _node, _slice_function

pytestmark = pytest.mark.basic


def test_entity_chat_shared_composer_draft_busy_retry_and_scope() -> None:
    html = gateway_console_html()
    source = "\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))
    functions = "\n".join(_slice_function(source, name) for name in (
        "renderEntityChatComposer", "chatUiState", "entityChatSend", "_resetChatUi", "entityChatClose",
    ))
    script = r"""
const state = {chatId:'',chatEntity:'Castor',chatDraft:'',chatBusy:false,manageName:'Castor',manageToken:1};
const elements = new Map();function $(id){if(!elements.has(id))elements.set(id,{id,textContent:'',disabled:false,classList:{toggle(){}}});return elements.get(id);}
const messages=[],notices=[],requests=[],snapshots=[];let activeProps;let mounts=0;let entityChatComposer=null;
const pendingApi=[];const resolveApi=(value,index=0)=>pendingApi.splice(index,1)[0].resolve(value);const rejectApi=(error,index=0)=>pendingApi.splice(index,1)[0].reject(error);
async function refreshEntityLive(){}
const islands={lib:{mountChatComposer(el,props){mounts++;activeProps=props;return{update(next){activeProps=next;}};}}};
function chatLine(who,text){messages.push({who,text});}
function _entOut(id,text){notices.push(text);}
function api(path,init){requests.push({path,text:JSON.parse(init.body).text});return new Promise((resolve,reject)=>{pendingApi.push({resolve,reject});});}
function mark(label){snapshots.push({label,draft:state.chatDraft,busy:state.chatBusy,disabled:activeProps.disabled,requests:requests.length,messages:[...messages]});}
FUNCTIONS
(async()=>{
 chatUiState();mark('closed');
 state.chatId='visit-A';chatUiState();activeProps.onChange('First line\nSecond line');
 const pending=entityChatSend();void entityChatSend();mark('pending');
 activeProps.onChange('A newer draft');resolveApi({reply:'A reply'});await pending;mark('newdraft');
 state.chatDraft='Keep these words';const failing=entityChatSend();rejectApi(new Error('temporary refusal'));await failing;mark('failed');
 const success=entityChatSend();resolveApi({reply:'Accepted'});await success;mark('success');
 state.chatDraft='Old visit words';const stale=entityChatSend();_resetChatUi();state.manageToken++;state.chatId='visit-B';state.chatEntity='Pollux';activeProps.onChange('Pollux draft');resolveApi({reply:'Late old reply'});await stale;mark('stale');
 // Close while a send is in flight: the composer must not stay busy for the next visit.
 state.chatId='visit-C';state.chatEntity='Castor';chatUiState();activeProps.onChange('Mid-flight words');const midflight=entityChatSend();
 const closing=entityChatClose();resolveApi({ok:true},1);await closing;mark('closed-midflight');
 resolveApi({reply:'Late reply for the closed visit'},0);await midflight;mark('after-late');
 state.chatId='visit-D';chatUiState();mark('next-visit');
 console.log(JSON.stringify({snapshots,mounts,textareaId:activeProps.textareaId,describedBy:activeProps.ariaDescribedBy,requests,notices}));
})();
""".replace("FUNCTIONS", functions)
    result = _node(script)
    if isinstance(result, list):
        result = result[0]
    states = {row["label"]: row for row in result["snapshots"]}
    assert result["mounts"] == 1
    assert result["textareaId"] == "entity-chat-input"
    assert result["describedBy"] == "entity-chat-input-help"
    assert states["closed"]["disabled"] is True
    assert states["pending"]["busy"] is True and states["pending"]["requests"] == 1
    assert states["newdraft"]["draft"] == "A newer draft" and not states["newdraft"]["busy"]
    assert states["failed"]["draft"] == "Keep these words" and not states["failed"]["busy"]
    assert states["success"]["draft"] == ""
    assert states["stale"]["draft"] == "Pollux draft" and not states["stale"]["busy"]
    assert not any(row["text"] == "Late old reply" for row in states["stale"]["messages"])
    assert states["closed-midflight"]["disabled"] is True and not states["closed-midflight"]["busy"]
    assert not states["after-late"]["busy"]
    assert not any(row["text"] == "Late reply for the closed visit" for row in states["after-late"]["messages"])
    assert states["next-visit"]["disabled"] is False and not states["next-visit"]["busy"]
    assert result["requests"][0]["path"].endswith("/Castor/chat/visit-A/turn")
    assert result["requests"][0]["text"] == "First line\nSecond line"
    assert "temporary refusal" in result["notices"]
    assert 'id="entity-chat-composer-root"' in html
    assert 'id="entity-chat-send"' not in html
    assert '$("entity-chat-input").onkeydown' not in source


def test_vendored_bundle_really_exports_mount_chat_composer() -> None:
    """The test above stubs lib.mountChatComposer, so it cannot see a stale
    vendored bundle. This one evaluates the REAL generated ISLANDS_JS and
    requires the export the console's renderEntityChatComposer() calls, and
    pins the vendored kit version to the kit checkout's package.json."""
    assert "mountChatComposer" in ISLANDS_JS, "vendored islands bundle lacks mountChatComposer: re-run console_islands_sync"
    probe = (
        "const vm = require('node:vm'); const fs = require('node:fs');"
        "const sb = { console }; sb.globalThis = sb; sb.window = sb; vm.createContext(sb);"
        "vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), sb);"
        "const a = sb.AfConsoleIslands;"
        "console.log(JSON.stringify({ kit: a.kitVersion, composer: typeof a.mountChatComposer }));"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as f:
        f.write(ISLANDS_JS)
        bundle = Path(f.name)
    try:
        out = subprocess.run([require_node(), "-e", probe, str(bundle)], capture_output=True, text=True, check=True, timeout=60)
    finally:
        bundle.unlink(missing_ok=True)
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["composer"] == "function"
    assert got["kit"] == ISLANDS_PROVENANCE["kit_version"]

    kit = console_islands_sync.locate_kit()
    if kit is None:
        pytest.skip("abstractuic ui-kit not present (non-monorepo checkout): kit version pin not checkable")
    # The console vendors a RELEASED kit: an abstractuic tag must carry that ui-kit version. The live
    # checkout may have moved on (abstractuic main at a newer ui-kit) without any drift here; the
    # drift itself is pinned against that tag in test_gateway_console_islands_sync.py.
    from kit_release import release_tag

    if os.getenv("ABSTRACTUIC_SRC", "").strip():
        kit_version = json.loads((kit / "package.json").read_text(encoding="utf-8"))["version"]
        assert ISLANDS_PROVENANCE["kit_version"] == kit_version, (
            f"console_islands.py vendors kit {ISLANDS_PROVENANCE['kit_version']} but ABSTRACTUIC_SRC is {kit_version}"
        )
    else:
        assert release_tag(kit.parent, ISLANDS_PROVENANCE["kit_version"]) is not None, (
            f"no abstractuic tag released the vendored ui-kit {ISLANDS_PROVENANCE['kit_version']}: "
            "re-sync the console from a released kit"
        )
