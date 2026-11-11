import pytest
from marine.stack_recovery import snapshot, restore, verify, promotion_check, sign_acceptance


def test_system_recovery_integrity_and_no_implicit_promotion(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "sample").write_text("synthetic component state")
    key = tmp_path / "recovery.key"
    key.write_bytes(b"x" * 32)
    key.chmod(0o600)
    config = {
        "codeRevision": "test",
        "resources": [{"name": "application", "kind": "directory", "source": str(source)}],
    }
    assert snapshot(config, tmp_path / "backup", key)["phase"] == "SNAPSHOT_VERIFIED"
    assert (
        restore(tmp_path / "backup", tmp_path / "restored", key, "marine-dr-test")[
            "productionPromoted"
        ]
        is False
    )
    with pytest.raises(ValueError):
        promotion_check(config, tmp_path / "restored", {}, key)
    complete = {
        k: True
        for k in [
            "applicationReadiness",
            "ledgerReconciled",
            "streamCheckpointChecked",
            "ipfsEvidenceVerified",
            "operatorApproved",
        ]
    }
    assert (
        promotion_check(
            config,
            tmp_path / "restored",
            sign_acceptance(tmp_path / "restored", complete, key),
            key,
        )["phase"]
        == "READY_FOR_CONTROLLED_CUTOVER"
    )
    (tmp_path / "backup" / "application.tar").write_bytes(b"damaged")
    with pytest.raises(ValueError):
        verify(tmp_path / "backup", key)


def test_system_snapshot_refuses_symlink_escape(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape").symlink_to("/etc/passwd")
    key = tmp_path / "key"
    key.write_bytes(b"x" * 32)
    key.chmod(0o600)
    with pytest.raises(ValueError):
        snapshot(
            {"resources": [{"name": "source", "kind": "directory", "source": str(source)}]},
            tmp_path / "backup",
            key,
        )
    assert (tmp_path / "backup" / "INCOMPLETE").exists()


def test_encrypted_bundle_authentication_and_roundtrip(tmp_path):
    from marine.recovery_encryption import encrypt_bundle, decrypt_bundle

    source = tmp_path / "source"
    source.mkdir()
    (source / "private").write_bytes(b"private synthetic state")
    key = tmp_path / "key"
    key.write_bytes(b"x" * 32)
    key.chmod(0o600)
    snapshot(
        {"resources": [{"kind": "directory", "name": "application", "source": str(source)}]},
        tmp_path / "bundle",
        key,
    )
    encrypt_bundle(tmp_path / "bundle", tmp_path / "encrypted", key)
    assert b"private synthetic state" not in (tmp_path / "encrypted").read_bytes()
    assert decrypt_bundle(tmp_path / "encrypted", tmp_path / "opened", key)["decryptedAndVerified"]
    from cryptography.exceptions import InvalidTag

    content = bytearray((tmp_path / "encrypted").read_bytes())
    content[-1] ^= 1
    (tmp_path / "corrupt").write_bytes(content)
    with pytest.raises(InvalidTag):
        decrypt_bundle(tmp_path / "corrupt", tmp_path / "denied", key)
    assert not (tmp_path / "denied").exists()
