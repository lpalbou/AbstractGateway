from abstractgateway.endpoint_vision_catalog import endpoint_vision_model


def test_relay_route_metadata_wins_over_name():
    def item(name, images):
        return {'id': name, 'raw': {'airelays': {'capabilities': {'routes': {'images': images}}}}}
    assert endpoint_vision_model(item('gpt-image-2', True), 'text_to_image', 'openai-compatible') == 'gpt-image-2'
    assert endpoint_vision_model(item('gpt-image-2', False), 'text_to_image', 'openai-compatible') is None
    assert endpoint_vision_model(item('gpt-6-astra', False), 'image_to_image', 'openai-compatible') is None


def test_generic_catalog_task_evidence_and_bare_model_ids():
    assert endpoint_vision_model({'id':'my-image', 'capabilities':['text_to_image']}, 'text_to_image', 'openai-compatible') == 'my-image'
    assert endpoint_vision_model({'id':'my-image', 'capabilities':['text_to_image']}, 'image_to_video', 'openai-compatible') is None
    assert endpoint_vision_model({'model':'openai-compatible/gpt-image-2'}, None, 'openai-compatible') == 'gpt-image-2'
    assert endpoint_vision_model({'id':'unknown'}, 'text_to_image', 'openai-compatible') is None
