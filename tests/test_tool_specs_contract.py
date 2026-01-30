from abstractcore.tools import ToolDefinition


def test_runtime_default_tool_specs_satisfy_tooldefinition_contract() -> None:
    """Prevent gateway boot regressions from invalid tool metadata."""
    from abstractruntime.integrations.abstractcore.default_tools import list_default_tool_specs

    specs = list_default_tool_specs()
    assert isinstance(specs, list)
    assert specs, "Expected at least one default tool spec"

    for spec in specs:
        assert isinstance(spec, dict)
        name = spec.get("name")
        assert isinstance(name, str) and name.strip()

        desc = spec.get("description")
        params = spec.get("parameters")
        when_to_use = spec.get("when_to_use")
        examples = spec.get("examples")
        tags = spec.get("tags")

        ToolDefinition(
            name=name.strip(),
            description=str(desc or ""),
            parameters=dict(params) if isinstance(params, dict) else {},
            when_to_use=str(when_to_use) if when_to_use is not None else None,
            examples=list(examples) if isinstance(examples, list) else [],
            tags=list(tags) if isinstance(tags, list) else [],
        )

