import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, PngImagePlugin
import pytest
from portfolio_analyzer import screenshot_import as images


def image_bytes(fmt='PNG', size=(20, 20), metadata=False):
    image = Image.new('RGB', size, 'white')
    output = BytesIO()
    info = PngImagePlugin.PngInfo()
    info.add_text('private', 'account-owner')
    image.save(output, format=fmt, **({'pnginfo': info} if metadata else {}))
    return output.getvalue()


@pytest.mark.parametrize('fmt,mime', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')])
def test_supported_images_are_decoded_and_reencoded(fmt, mime):
    result = images._sanitize_image(image_bytes(fmt), mime)
    with Image.open(BytesIO(result)) as clean:
        assert clean.format == 'PNG'
        assert clean.size == (20, 20)
        assert clean.info == {}


def test_private_metadata_removed():
    raw = image_bytes(metadata=True)
    assert b'account-owner' in raw
    result = images._sanitize_image(raw, 'image/png')
    assert b'account-owner' not in result


@pytest.mark.parametrize('data,mime', [(b'garbage', 'image/png'), (b'', 'image/png'),
                                      (b'abc', 'text/plain'), (image_bytes(), 'image/jpeg')])
def test_invalid_images_rejected(data, mime):
    with pytest.raises(images.ImageImportError):
        images._sanitize_image(data, mime)


def test_byte_and_pixel_limits(monkeypatch):
    monkeypatch.setattr(images, 'MAX_IMAGE_BYTES', 5)
    with pytest.raises(images.ImageImportError, match='5 MB'):
        images._sanitize_image(image_bytes(), 'image/png')
    monkeypatch.setattr(images, 'MAX_IMAGE_BYTES', 5 * 1024 * 1024)
    monkeypatch.setattr(images, 'MAX_IMAGE_PIXELS', 100)
    with pytest.raises(images.ImageImportError, match='pixel'):
        images._sanitize_image(image_bytes(), 'image/png')


@pytest.mark.asyncio
async def test_extraction_is_multimodal_nullable_and_reviewable(monkeypatch):
    import langchain.agents
    calls = {}
    async def invoke(payload, config):
        calls['payload'] = payload
        assert config['recursion_limit'] == 4
        return {'structured_response': {'positions': [{'symbol': 'abc', 'quantity': None, 'price': '12.5'}], 'cash': None}}
    def create(**kwargs):
        calls['agent'] = kwargs
        return SimpleNamespace(ainvoke=invoke)
    monkeypatch.setattr(langchain.agents, 'create_agent', create)
    draft = await images.extract_portfolio_image(image_bytes(metadata=True), 'image/png', object())
    assert draft['cash'] is None
    assert draft['positions'][0]['quantity'] is None
    assert draft['positions'][0]['symbol'] == 'ABC'
    assert draft['positions'][0]['price'] == '12.5'
    assert draft['source'] == 'image'
    assert any('Review' in warning for warning in draft['warnings'])
    assert calls['agent']['tools'] == []
    block = calls['payload']['messages'][0]['content'][1]
    assert block['type'] == 'image'
    assert b'account-owner' not in base64.b64decode(block['base64'])


@pytest.mark.asyncio
async def test_timeout_and_provider_errors_are_safe(monkeypatch):
    import langchain.agents
    async def slow(*args, **kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(images, 'EXTRACTION_TIMEOUT', 0.001)
    monkeypatch.setattr(langchain.agents, 'create_agent', lambda **k: SimpleNamespace(ainvoke=slow))
    with pytest.raises(images.ImageImportError, match='timed out'):
        await images.extract_portfolio_image(image_bytes(), 'image/png', object())
    async def fail(*args, **kwargs):
        raise RuntimeError('private-key-sensitive-details')
    monkeypatch.setattr(langchain.agents, 'create_agent', lambda **k: SimpleNamespace(ainvoke=fail))
    with pytest.raises(images.ImageImportError) as exc:
        await images.extract_portfolio_image(image_bytes(), 'image/png', object())
    assert 'private-key' not in str(exc.value)


@pytest.mark.asyncio
async def test_invalid_model_values_rejected(monkeypatch):
    import langchain.agents
    async def invoke(*args, **kwargs):
        return {'structured_response': {'positions': [{'symbol': 'ABC', 'quantity': '-2'}]}}
    monkeypatch.setattr(langchain.agents, 'create_agent', lambda **k: SimpleNamespace(ainvoke=invoke))
    with pytest.raises(images.ImageImportError, match='failed'):
        await images.extract_portfolio_image(image_bytes(), 'image/png', object())

def test_model_image_dimensions_and_byte_budget(monkeypatch):
    monkeypatch.setattr(images, 'MAX_MODEL_DIMENSION', 10)
    result = images._sanitize_image(image_bytes(size=(40, 20)), 'image/png')
    with Image.open(BytesIO(result)) as clean:
        assert clean.size == (10, 5)
    monkeypatch.setattr(images, 'MAX_MODEL_IMAGE_BYTES', 1)
    with pytest.raises(images.ImageImportError, match='complex'):
        images._sanitize_image(image_bytes(), 'image/png')
