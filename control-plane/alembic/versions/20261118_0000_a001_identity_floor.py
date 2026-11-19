# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""The identity floor: identity policies, credential facts, one OIDC IdP per issuer.

* `resource_groups.identity_policy`: a group's explicit identity policy.
* `tenants.identity_policy_default`: the tenant's identity default.
* `proxy_logs.credential_kind`, `credential_assurance`, `credential_ref`,
  `credential_policy_outcome`: what authenticated each call, at which level,
  and the identity policy's verdict; sealed in the audit chain, whose
  format becomes `v5`.
* `ai_system_runs.credential_assurance_min`, `credential_mixed`,
  `credential_refs`: the same over a run's steps.
* `ix_proxy_logs_credential`: the lifecycle gate's look-back and the
  identity summary read through it.
* `uq_idp_servers_tenant_oidc_issuer`: one OIDC IdP per issuer and tenant.

No data is migrated.

Revision ID: a001_identity_floor
Revises: a000_baseline
Create Date: 2026-11-18
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a001_identity_floor"
down_revision: Union[str, None] = "a000_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_LEVELS = "'key', 'bound_key', 'workload', 'workload_pop'"
_KINDS = (
    "'api_key', 'workload_token', 'x509_svid', 'brutor_token', "
    "'human_session', 'platform_service', 'anonymous'"
)
_OUTCOMES = "'met', 'below_policy', 'refused', 'exempt'"


def upgrade() -> None:
    op.add_column(
        "resource_groups",
        sa.Column(
            "identity_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Explicit identity policy (six members) or NULL = no explicit policy",
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "identity_policy_default",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment=(
                "The tenant's identity default: an identity policy plus "
                "max_user_key_ttl_days. NULL = no tenant default"
            ),
        ),
    )

    op.add_column(
        "proxy_logs",
        sa.Column(
            "credential_kind",
            sa.String(length=16),
            nullable=True,
            comment="What authenticated the call; NULL only before authentication succeeded",
        ),
    )
    op.add_column(
        "proxy_logs",
        sa.Column(
            "credential_assurance",
            sa.String(length=16),
            nullable=True,
            comment="The credential's level on the ladder; NULL for kinds outside the ladder",
        ),
    )
    op.add_column(
        "proxy_logs",
        sa.Column(
            "credential_ref",
            sa.String(length=512),
            nullable=True,
            comment="Stable, non-secret reference to the credential (api_key:<id>, token:<idp>:<sub>, ...)",
        ),
    )
    op.add_column(
        "proxy_logs",
        sa.Column(
            "credential_policy_outcome",
            sa.String(length=16),
            nullable=True,
            comment="The identity policy's verdict: met | below_policy | refused | exempt",
        ),
    )
    op.drop_constraint("ck_proxy_logs_chain_version", "proxy_logs", type_="check")
    op.create_check_constraint(
        "ck_proxy_logs_chain_version", "proxy_logs", "chain_version IN ('v5')"
    )
    op.create_check_constraint(
        "ck_proxy_logs_credential_kind",
        "proxy_logs",
        f"credential_kind IS NULL OR credential_kind IN ({_KINDS})",
    )
    op.create_check_constraint(
        "ck_proxy_logs_credential_assurance",
        "proxy_logs",
        f"credential_assurance IS NULL OR credential_assurance IN ({_LEVELS})",
    )
    op.create_check_constraint(
        "ck_proxy_logs_credential_outcome",
        "proxy_logs",
        f"credential_policy_outcome IS NULL OR credential_policy_outcome IN ({_OUTCOMES})",
    )
    op.create_index(
        "ix_proxy_logs_credential",
        "proxy_logs",
        ["tenant_id", "resource_group_id", "credential_assurance", "timestamp"],
        unique=False,
    )

    op.add_column(
        "ai_system_runs",
        sa.Column(
            "credential_assurance_min",
            sa.String(length=16),
            nullable=True,
            comment="Lowest credential level over the run's steps on the ladder; NULL when none is, or any step is anonymous",
        ),
    )
    op.add_column(
        "ai_system_runs",
        sa.Column(
            "credential_mixed",
            sa.Boolean(),
            nullable=True,
            comment="True when two or more distinct credentials made the run's steps",
        ),
    )
    op.add_column(
        "ai_system_runs",
        sa.Column(
            "credential_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Distinct credential references of the run's steps, first-seen order, at most 8",
        ),
    )
    op.create_check_constraint(
        "ck_ai_system_runs_credential_assurance_min",
        "ai_system_runs",
        f"credential_assurance_min IS NULL OR credential_assurance_min IN ({_LEVELS})",
    )

    op.create_index(
        "uq_idp_servers_tenant_oidc_issuer",
        "idp_servers",
        ["tenant_id", "issuer"],
        unique=True,
        postgresql_where=sa.text("protocol = 'oidc'"),
    )


def downgrade() -> None:
    op.drop_index("uq_idp_servers_tenant_oidc_issuer", table_name="idp_servers")

    op.drop_constraint("ck_ai_system_runs_credential_assurance_min", "ai_system_runs", type_="check")
    op.drop_column("ai_system_runs", "credential_refs")
    op.drop_column("ai_system_runs", "credential_mixed")
    op.drop_column("ai_system_runs", "credential_assurance_min")

    op.drop_index("ix_proxy_logs_credential", table_name="proxy_logs")
    op.drop_constraint("ck_proxy_logs_credential_outcome", "proxy_logs", type_="check")
    op.drop_constraint("ck_proxy_logs_credential_assurance", "proxy_logs", type_="check")
    op.drop_constraint("ck_proxy_logs_credential_kind", "proxy_logs", type_="check")
    op.drop_constraint("ck_proxy_logs_chain_version", "proxy_logs", type_="check")
    op.create_check_constraint(
        "ck_proxy_logs_chain_version", "proxy_logs", "chain_version IN ('v1', 'v2', 'v3', 'v4')"
    )
    op.drop_column("proxy_logs", "credential_policy_outcome")
    op.drop_column("proxy_logs", "credential_ref")
    op.drop_column("proxy_logs", "credential_assurance")
    op.drop_column("proxy_logs", "credential_kind")

    op.drop_column("tenants", "identity_policy_default")
    op.drop_column("resource_groups", "identity_policy")
