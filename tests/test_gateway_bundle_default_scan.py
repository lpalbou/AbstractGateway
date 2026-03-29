from abstractgateway.hosts.bundle_host import _scan_flows_for_llm_defaults


def test_scan_flows_for_llm_defaults_reads_on_flow_start_pin_defaults() -> None:
    flows = {
        "basic-agent@0.0.0:root": {
            "nodes": [
                {
                    "id": "start",
                    "type": "on_flow_start",
                    "data": {
                        "pinDefaults": {
                            "provider": "lmstudio",
                            "model": "qwen/qwen3.5-35b-a3b",
                        }
                    },
                }
            ]
        }
    }

    assert _scan_flows_for_llm_defaults(flows) == ("lmstudio", "qwen/qwen3.5-35b-a3b")
