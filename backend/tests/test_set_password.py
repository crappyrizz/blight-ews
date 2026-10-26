"""Tests for tools/set_password.py: one account changes, the new password works, nothing is shown."""
from contextlib import nullcontext

import pytest

import backend.db
from backend import auth
from backend.models import Farmer
from tools import set_password as sp

NEW = "a-new-admin-password"


@pytest.fixture
def accounts(db_session):
    admin = Farmer(name="Admin", phone_number="+254712000055", password_hash=auth.hash_password("old-admin-pw"),
                   role="admin")
    farmer = Farmer(name="Farmer", phone_number="+254712000056", password_hash=auth.hash_password("farmer-pw-1"),
                    role="farmer")
    db_session.add_all([admin, farmer])
    db_session.flush()
    return admin, farmer


def test_the_new_password_works_and_the_old_one_does_not(db_session, accounts):
    admin, _ = accounts
    sp.set_password(db_session, "0712000055", NEW)
    assert auth.verify_password(NEW, admin.password_hash)
    assert not auth.verify_password("old-admin-pw", admin.password_hash)


def test_only_that_account_changes(db_session, accounts):
    _, farmer = accounts
    before = farmer.password_hash
    sp.set_password(db_session, "0712000055", NEW)
    assert farmer.password_hash == before


def test_login_works_with_the_new_password(client, db_session, accounts):
    sp.set_password(db_session, "0712000055", NEW)
    response = client.post("/auth/login", json={"phone_number": "0712000055", "password": NEW})
    assert response.status_code == 200


@pytest.mark.parametrize("password", ["short", "x" * 73])
def test_password_length_follows_registration(db_session, accounts, password):
    with pytest.raises(sp.PasswordError, match="8 to 72"):
        sp.set_password(db_session, "0712000055", password)


def test_an_unknown_or_invalid_number_is_refused(db_session, accounts):
    with pytest.raises(sp.PasswordError, match="no account"):
        sp.set_password(db_session, "0712999999", NEW)
    with pytest.raises(sp.PasswordError):
        sp.set_password(db_session, "12345", NEW)


def test_the_command_never_prints_the_password(db_session, accounts, monkeypatch, capsys):
    monkeypatch.setattr(backend.db, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(sp.getpass, "getpass", lambda prompt="": NEW)
    assert sp.main(["0712000055"]) == 0
    out = capsys.readouterr().out
    assert NEW not in out
    assert "Password changed for +254712000055 (admin" in out


def test_the_command_refuses_two_different_passwords(db_session, accounts, monkeypatch, capsys):
    answers = iter([NEW, "something-else"])
    monkeypatch.setattr(sp.getpass, "getpass", lambda prompt="": next(answers))
    admin, _ = accounts
    before = admin.password_hash
    assert sp.main(["0712000055"]) == 1
    assert "differ" in capsys.readouterr().out
    assert admin.password_hash == before


def test_hosted_needs_the_neon_file(tmp_path):
    with pytest.raises(sp.PasswordError, match="no DATABASE_URL"):
        sp.hosted_session(tmp_path / "missing.local.env")
