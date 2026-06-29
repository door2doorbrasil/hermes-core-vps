from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_cli import profiles as profiles_mod


def test_profile_meta_persists_acl(tmp_path: Path) -> None:
    profile_dir = tmp_path / "compras"
    profile_dir.mkdir()

    profiles_mod.write_profile_meta(
        profile_dir,
        description="Compras",
        acl={
            "public": False,
            "allowed_groups": ["compras"],
            "allowed_user_ids": ["buyer-1"],
            "allowed_emails": ["buyer@example.com"],
        },
    )

    meta = profiles_mod.read_profile_meta(profile_dir)
    assert meta["description"] == "Compras"
    assert meta["acl"] is not None
    assert meta["acl"]["public"] is False
    assert meta["acl"]["allowed_groups"] == ["compras"]
    assert meta["acl"]["allowed_user_ids"] == ["buyer-1"]
    assert meta["acl"]["allowed_emails"] == ["buyer@example.com"]


def test_profile_acl_filters_accessible_profiles_by_group(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    profiles_root = home / "profiles"
    default_home = home
    compras = profiles_root / "compras"
    vendas = profiles_root / "vendas"
    financeiro = profiles_root / "financeiro"

    for profile_dir in (default_home, compras, vendas, financeiro):
        (profile_dir / "skills").mkdir(parents=True, exist_ok=True)

    profiles_mod.write_profile_meta(
        compras,
        acl={
            "public": False,
            "allowed_groups": ["compras"],
        },
    )
    profiles_mod.write_profile_meta(
        financeiro,
        acl={
            "public": False,
            "allowed_groups": ["financeiro"],
        },
    )
    profiles_mod.write_profile_meta(vendas)

    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(profiles_mod, "_read_config_model", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(profiles_mod, "_read_distribution_meta", lambda *_args, **_kwargs: (None, None, None))
    monkeypatch.setattr(profiles_mod, "_check_gateway_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(profiles_mod, "find_alias_for_profile", lambda *_args, **_kwargs: None)

    buyer = SimpleNamespace(
        user_id="buyer-1",
        email="buyer@example.com",
        org_id="compras,financeiro",
        provider="basic",
        groups=("compras", "financeiro"),
    )
    assert profiles_mod.profile_allows_session(compras, session=buyer)
    assert profiles_mod.profile_allows_session(financeiro, session=buyer)
    assert profiles_mod.profile_allows_session(vendas, session=buyer)

    allowed = profiles_mod.list_accessible_profiles(session=buyer)
    assert [profile.name for profile in allowed] == ["default", "compras", "financeiro", "vendas"]

    sales = SimpleNamespace(
        user_id="sales-1",
        email="sales@example.com",
        org_id="vendas",
        provider="basic",
        groups=("vendas",),
    )
    assert not profiles_mod.profile_allows_session(compras, session=sales)
    assert profiles_mod.profile_allows_session(vendas, session=sales)
    assert not profiles_mod.profile_allows_session(financeiro, session=sales)


def test_profile_acl_falls_back_to_org_id_groups(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / ".hermes"
    profiles_root = home / "profiles"
    default_home = home
    compras = profiles_root / "compras"
    for profile_dir in (default_home, compras):
        (profile_dir / "skills").mkdir(parents=True, exist_ok=True)

    profiles_mod.write_profile_meta(
        compras,
        acl={
            "public": False,
            "groups": ["compras"],
        },
    )

    monkeypatch.setattr(profiles_mod, "_get_default_hermes_home", lambda: default_home)
    monkeypatch.setattr(profiles_mod, "_get_profiles_root", lambda: profiles_root)
    monkeypatch.setattr(profiles_mod, "_read_config_model", lambda *_args, **_kwargs: (None, None))
    monkeypatch.setattr(profiles_mod, "_read_distribution_meta", lambda *_args, **_kwargs: (None, None, None))
    monkeypatch.setattr(profiles_mod, "_check_gateway_running", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(profiles_mod, "find_alias_for_profile", lambda *_args, **_kwargs: None)

    legacy_session = SimpleNamespace(
        user_id="buyer-2",
        email="buyer2@example.com",
        org_id="compras,backoffice",
        provider="basic",
    )
    assert profiles_mod.profile_allows_session(compras, session=legacy_session)
