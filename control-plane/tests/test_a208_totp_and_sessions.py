# Copyright 2026 Brutor AI Ltd.
# SPDX-License-Identifier: Apache-2.0

"""
a208_totp_and_sessions: stored TOTP secrets are encrypted, and existing ones keep working.
"""


import pytest
import sqlalchemy as sa

from app.database.models.admin import SystemAdmin, TenantAdmin
from app.database.models.end_user import EndUser


@pytest.mark.parametrize("model", [EndUser, TenantAdmin, SystemAdmin])
def test_every_account_table_has_room_for_ciphertext(model):
    assert isinstance(model.__table__.columns["totp_secret"].type, sa.Text)


def test_the_model_declares_the_session_cutoff():
    column = EndUser.__table__.columns["sessions_valid_after"]
    assert column.nullable, "NULL means no password change has ended any session"
