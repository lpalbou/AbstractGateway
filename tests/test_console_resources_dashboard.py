from __future__ import annotations

import json
import subprocess

import pytest

from abstractgateway.console import gateway_console_html
from node_requirement import require_node
from test_gateway_console_offline import _slice_function

pytestmark = pytest.mark.basic


def test_memory_pool_selection_never_confuses_process_usage_with_vram():
    source = gateway_console_html()
    functions = "\n".join(_slice_function(source, name) for name in ["resourceNumber", "resourceMemoryView", "deviceMeterView", "processHeldBasisWords", "_fmtBytes"])
    js = functions + """
const cases = [
 {memory:{device:{backend:'metal',allocated_bytes:16,total_bytes:128,wired_limit_bytes:105}}},
 {memory:{device:{backend:'cuda',allocated_bytes:2,process_held_bytes:3,total_bytes:24,free_bytes:4}}},
 {memory:{device:{backend:'cuda',unified_memory:true,allocated_bytes:2,total_bytes:128}}},
 {memory:{device:{backend:'cuda',allocated_bytes:0,total_bytes:24,free_bytes:0}}},
 {memory:{device:{backend:'cuda',allocated_bytes:2,total_bytes:24}}},
 {memory:{device:{backend:null,total_bytes:null,allocated_bytes:null}}},
 {host:{host_name:'mac'},system:{hardware:{platform:'darwin',machine:'arm64',host_name:'mac'}},memory:{device:{backend:'mps',allocated_bytes:4}}},
 {host:{host_name:'mac'},system:{hardware:{platform:'darwin',machine:'x86_64',host_name:'mac'}},memory:{device:{backend:'mps',allocated_bytes:4}}},
 {host:{host_name:'remote'},system:{hardware:{platform:'darwin',machine:'arm64',host_name:'local'}},memory:{device:{backend:'mps',allocated_bytes:4}}}
];
console.log(JSON.stringify(cases.map(resourceMemoryView)));
"""
    result = subprocess.run([require_node(), "-e", js], capture_output=True, text=True, check=True)
    metal, cuda, integrated, full, partial, unknown, mps_arm, mps_intel, remote = json.loads(result.stdout)
    assert metal["topology"] == "unified"
    assert metal["device"]["used"] == 16
    assert metal["device"]["free"] is None  # heap headroom is not allocatable RAM
    assert "Overlaps" in metal["device"]["note"]
    assert cuda["device"]["used"] == 20  # not process-local 2 or 3
    assert cuda["device"]["free"] == 4
    assert cuda["device"]["ceiling"] == 24
    assert cuda["device"]["scope"] == "Device-wide"
    assert integrated["topology"] == "unified"
    assert full["device"]["used"] == 24 and full["device"]["free"] == 0
    assert partial["device"]["ceiling"] is None
    assert partial["device"]["scope"] == "This process only"
    assert unknown["topology"] == "unknown" and unknown["device"]["used"] is None
    assert mps_arm["topology"] == "unified"
    assert mps_intel["topology"] == "unknown"
    assert remote["topology"] == "unknown"  # never use the browser/other host's hardware


def test_history_is_real_bounded_and_splits_when_measurement_scope_changes():
    html = gateway_console_html()
    fn = _slice_function(html, "resourcePushSample")
    js = "const resUi={samples:[]};\n" + fn + """
const now = Date.now();
for(let i=0;i<1000;i++) resourcePushSample({at:now-1000+i,topology:'unified',deviceScope:'this process only',ram:i});
const bound=resUi.samples.length;
resourcePushSample({at:now+1,topology:'gpu',deviceScope:'Device-wide',ram:5});
console.log(JSON.stringify({bound,split:resUi.samples.length,ram:resUi.samples[0].ram}));
"""
    result = subprocess.run([require_node(), "-e", js], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"bound": 720, "split": 1, "ram": 5}
    assert "since this page opened" in html
    assert "Math.sin" not in _slice_function(html, "resourcesRenderChart")


def test_resources_reuses_shared_widgets_and_tooltip_modal_primitives():
    html = gateway_console_html()
    assert 'id="resources-inspector-backdrop" class="af-modal-backdrop"' in html
    assert "bindAccountModal($(\"resources-inspector-backdrop\"),resourcesCloseInspector)" in html
    assert 'data-af-tip="Real samples collected' in html
    assert 'id="af-topbar-root"' in html
    for token in ["mountTopBar", "mountAppearance", 'identity: "menu"', 'label: "Docs assistant"', 'label: "About AbstractGateway"']:
        assert token in html
