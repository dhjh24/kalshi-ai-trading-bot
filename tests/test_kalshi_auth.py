from src.utils.kalshi_auth import resolve_private_key_path


def test_resolve_private_key_path_prefers_existing_explicit_path(tmp_path):
    explicit_key = tmp_path / "custom.pem"
    explicit_key.write_text("test")

    assert resolve_private_key_path(str(explicit_key)) == str(explicit_key)


def test_resolve_private_key_path_finds_pem_default(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KALSHI_PRIVATE_KEY_PATH", raising=False)
    pem_key = tmp_path / "kalshi_private_key.pem"
    pem_key.write_text("test")

    assert resolve_private_key_path() == "kalshi_private_key.pem"
