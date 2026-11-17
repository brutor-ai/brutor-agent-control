# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
# The Unbound group and the platform service key

The control plane calls the core proxy with one platform key
(`system-health-check`, `BRUTOR_GATEWAY_SERVICE_API_KEY`): model health checks,
the assurance judge, knowledge-base embeddings, and the KB uploader. It was
bound to Default, so:
  * a health check of a model not in Default was refused (403), although
    the probe exists for a model that is in no group yet;
  * unbinding it instead would have billed judge and embedding spend to
    whichever group happened to hold the model.

Now the key is unbound and flagged `is_platform_service` (settable only by
this seed), and every tenant has an `Unbound` system group the core bills
the platform key's calls to. Any OTHER key with no group is refused by the
core: a call that is not bound to a resource group would escape every
group's limits and policies.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.constants.groups import ResourceGroupName
from app.database import (
    Base,
    _seed_system_health_check_key,
    ensure_unbound_resource_groups,
)
from app.database.models.api_key import APIKey
from app.database.models.resource_group import ResourceGroup
from app.database.models.tenant import Tenant
from app.services.tenant.tenant_provisioning import TenantProvisioner
from tests.core_repo import core_repo_path


class _Log:
    def __init__(self):
        self.lines = []

    def info(self, msg, context=None):
        self.lines.append(msg)

    warning = debug = error = info


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    for tid in ("default", "t-other"):
        s.add(Tenant(id=tid, name=tid, display_name=tid,
                     primary_contact_email=f"ops@{tid}.test"))
    s.add(ResourceGroup(id="group-default", tenant_id="default", name="Default",
                        is_system=True))
    s.commit()
    yield s
    s.close()


def _unbound(db, tenant_id):
    return db.query(ResourceGroup).filter(
        ResourceGroup.tenant_id == tenant_id,
        ResourceGroup.name == ResourceGroupName.UNBOUND.value,
        ResourceGroup.is_system.is_(True),
    ).all()


def test_provisioning_creates_one_unbound_system_group(db):
    p = TenantProvisioner(logger=_Log())
    first = p._create_unbound_resource_group(db, "default")
    again = p._create_unbound_resource_group(db, "default")
    db.commit()
    groups = _unbound(db, "default")
    assert len(groups) == 1 and first == again == groups[0].id
    g = groups[0]
    assert g.is_active
    assert not g.llm_global_limits, "seeded with no limits: platform calls are never refused for spend"


def test_startup_gives_every_existing_tenant_an_unbound_group(db):
    ensure_unbound_resource_groups(db)
    db.commit()
    assert len(_unbound(db, "default")) == 1
    assert len(_unbound(db, "t-other")) == 1
    ensure_unbound_resource_groups(db)
    db.commit()
    assert len(_unbound(db, "t-other")) == 1, "idempotent"


def test_a_tenant_group_that_merely_shares_the_name_is_not_taken_for_it(db):
    db.add(ResourceGroup(id="group-mine", tenant_id="t-other", name="Unbound",
                         is_system=False))
    db.commit()
    ensure_unbound_resource_groups(db)
    db.commit()
    assert [g.id for g in _unbound(db, "t-other")] != ["group-mine"]
    assert len(_unbound(db, "t-other")) == 1


def _key(db):
    return db.query(APIKey).filter(APIKey.name == "system-health-check").one()


def test_the_platform_key_is_created_unbound_and_flagged(db, monkeypatch):
    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "sk_brutor_api_" + "d" * 40)
    _seed_system_health_check_key(db, "default", _Log())
    db.commit()
    key = _key(db)
    assert key.group_id is None
    assert key.is_platform_service is True
    assert key.scope == "global"


def test_without_a_configured_key_no_secret_is_logged(db, monkeypatch):
    """an unset BRUTOR_GATEWAY_SERVICE_API_KEY used to make a key and
    write it, in full, to the log. Nothing secret is logged now: the log says
    which variable to set, and the key the variable holds is registered at
    the next start."""
    monkeypatch.delenv("BRUTOR_GATEWAY_SERVICE_API_KEY", raising=False)
    log = _Log()
    _seed_system_health_check_key(db, "default", log)
    db.commit()
    assert not any("sk_brutor_api_" in line for line in log.lines), log.lines
    assert any("BRUTOR_GATEWAY_SERVICE_API_KEY" in line for line in log.lines), log.lines

    monkeypatch.setenv("BRUTOR_GATEWAY_SERVICE_API_KEY", "sk_brutor_api_" + "c" * 40)
    _seed_system_health_check_key(db, "default", _Log())
    db.commit()
    import hashlib
    assert _key(db).key_hash == hashlib.sha256(("sk_brutor_api_" + "c" * 40).encode()).hexdigest()


def test_a_key_an_earlier_release_bound_to_default_is_converged(db):
    db.add(APIKey(id="k-old", tenant_id="default", name="system-health-check",
                  key_hash="h" * 64, key_prefix="sk_brutor_api_xxxxxx",
                  scope="global", group_id="group-default", enabled=True,
                  created_by="system"))
    db.commit()
    log = _Log()
    _seed_system_health_check_key(db, "default", log)
    db.commit()
    key = _key(db)
    assert key.group_id is None and key.is_platform_service is True
    assert key.key_hash == "h" * 64, "the deployed secret is kept"
    assert any("platform" in line for line in log.lines), log.lines


def test_an_admin_created_key_is_never_a_platform_key(db):
    # The flag has a database default of false and no admin API field.
    db.add(APIKey(id="k-admin", tenant_id="default", name="mine",
                  key_hash="a" * 64, key_prefix="sk_brutor_api_yyyyyy",
                  scope="global", group_id="group-default", enabled=True,
                  created_by="42"))
    db.commit()
    assert db.query(APIKey).get("k-admin").is_platform_service is False
    from app.api.v1.models.admin import resource_group as rg_models
    for name in dir(rg_models):
        model = getattr(rg_models, name)
        fields = getattr(model, "model_fields", None)
        if fields and "Key" in name:
            assert "is_platform_service" not in fields, f"{name} lets a caller set the flag"


def test_the_core_honours_only_the_platform_key():
    core = core_repo_path() / "src"
    auth = (core / "services" / "auth.rs").read_text()
    assert "api_key_is_platform" in auth, "the core does not read the platform flag"
    preflight = (core / "services" / "llm_preflight.rs").read_text()
    assert "auth_ctx.api_key_is_platform" in preflight, (
        "the health-probe exemption must require the platform key, not 'any key with no group'"
    )
    groups = (core / "constants.rs").read_text()
    assert f'"{ResourceGroupName.UNBOUND.value}"' in groups, "core and CP name the Unbound group differently"
