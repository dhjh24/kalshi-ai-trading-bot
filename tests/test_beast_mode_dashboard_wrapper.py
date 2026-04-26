import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_wrapper_module():
    wrapper_path = (
        Path(__file__).resolve().parent.parent / "scripts" / "beast_mode_dashboard.py"
    )
    spec = importlib.util.spec_from_file_location(
        "test_beast_mode_dashboard_wrapper_module",
        wrapper_path,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_wrapper_loads_repo_root_dashboard_main(monkeypatch):
    wrapper_module = _load_wrapper_module()
    loaded = {}

    async def fake_main():
        return None

    class FakeLoader:
        def exec_module(self, module):
            module.main = fake_main
            loaded["module"] = module

    fake_spec = SimpleNamespace(name="repo_beast_mode_dashboard_fake", loader=FakeLoader())
    fake_module = SimpleNamespace()

    monkeypatch.setattr(
        wrapper_module.importlib.util,
        "spec_from_file_location",
        lambda name, path: fake_spec,
    )
    monkeypatch.setattr(
        wrapper_module.importlib.util,
        "module_from_spec",
        lambda spec: fake_module,
    )

    main = wrapper_module._load_repo_dashboard_main()

    assert main is fake_main
    assert loaded["module"] is fake_module
