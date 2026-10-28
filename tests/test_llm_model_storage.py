from pathlib import Path
from types import SimpleNamespace

import pytest

from rynmesh.llm_package import lifecycle
from rynmesh.llm_package.errors import LifecycleError
from rynmesh.llm_package.manifest import LLMPackageManifest, save_manifest


@pytest.mark.parametrize('owned', [True, False])
def test_model_removal_updates_actual_storage_and_preserves_manifest(tmp_path, monkeypatch, owned):
    model = tmp_path / 'models/local/model.gguf'
    model.parent.mkdir(parents=True)
    model.write_bytes(b'GGUF' + bytes(1024))
    manifest = LLMPackageManifest(package_id='local', mode='managed', public_model_alias='Local',
        runtime=lifecycle.RUNTIME_NATIVE, model_path=str(model), model_owned=owned)
    path = tmp_path / 'packages/local/manifest.json'
    save_manifest(manifest, path)
    before = path.read_bytes()
    monkeypatch.setattr(lifecycle, '_backend', lambda _: SimpleNamespace(remove=lambda _: None, state=lambda _: {'running': False}))
    monkeypatch.setattr(lifecycle, 'adapter_from_manifest', lambda _: SimpleNamespace(health=lambda: {'ok': False}))
    assert lifecycle.status(path)['storage']['model_bytes'] == 1028
    if owned:
        lifecycle.uninstall(path, delete_model=True, confirm_model_delete=True)
        assert lifecycle.status(path)['storage'] == {'model_owned': True, 'model_present': False, 'model_bytes': 0}
    else:
        with pytest.raises(LifecycleError, match='imported/user-owned'):
            lifecycle.uninstall(path, delete_model=True, confirm_model_delete=True)
        assert model.read_bytes() == b'GGUF' + bytes(1024)
    assert path.read_bytes() == before
    assert str(model) not in str(lifecycle.status(path)['storage'])


def test_storage_inspection_failure_is_unknown_not_removed(tmp_path, monkeypatch):
    model = tmp_path / 'unreadable.gguf'
    manifest = LLMPackageManifest(package_id='local', mode='managed', public_model_alias='Local', model_path=str(model))
    original_stat = Path.stat

    def denied(path, *args, **kwargs):
        if path == model:
            raise PermissionError('private path must not escape')
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'stat', denied)
    assert lifecycle.model_storage(manifest) == {'model_owned': False, 'model_present': None, 'model_bytes': None}
