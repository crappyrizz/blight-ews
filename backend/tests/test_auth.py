"""Tests for registration, login, tokens and the ownership rule."""
import pytest

from backend import auth
from backend.models import Farmer, SensorNode
from backend.schemas import normalise_phone

PHONE = "0712345678"
PASSWORD = "shangi-2026"


# --- password hashing --------------------------------------------------


def test_hash_and_verify():
    hashed = auth.hash_password(PASSWORD)

    assert hashed != PASSWORD
    assert auth.verify_password(PASSWORD, hashed)
    assert not auth.verify_password("wrong", hashed)


def test_same_password_hashes_differently():
    """bcrypt salts each hash, so identical passwords do not look identical."""
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


# --- phone normalisation ----------------------------------------------


@pytest.mark.parametrize(
    "typed, expected",
    [
        ("0712345678", "+254712345678"),
        ("0112345678", "+254112345678"),
        ("254712345678", "+254712345678"),
        ("+254712345678", "+254712345678"),
        ("712345678", "+254712345678"),
        ("+254 712 345 678", "+254712345678"),
        ("0712-345-678", "+254712345678"),
        (" 0712345678 ", "+254712345678"),
    ],
)
def test_phone_numbers_are_normalised(typed, expected):
    assert normalise_phone(typed) == expected


@pytest.mark.parametrize(
    "typed",
    [
        "071234567",  # too short
        "07123456789",  # too long
        "0812345678",  # 8 is not a Kenyan mobile prefix
        "+255712345678",  # Tanzania
        "phone",
        "",
    ],
)
def test_invalid_phone_numbers_are_rejected(typed):
    with pytest.raises(ValueError):
        normalise_phone(typed)


# --- helpers for the API tests ----------------------------------------


def register(client, phone=PHONE, password=PASSWORD, name="Kinangop Farmer", farm_id="FARM-1"):
    return client.post(
        "/auth/register",
        json={"name": name, "phone_number": phone, "password": password, "farm_id": farm_id},
    )


def login(client, phone=PHONE, password=PASSWORD):
    return client.post("/auth/login", json={"phone_number": phone, "password": password})


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def make_farmer(session, phone, role=auth.ROLE_FARMER, name="Other"):
    farmer = Farmer(
        name=name,
        phone_number=phone,
        password_hash=auth.hash_password(PASSWORD),
        role=role,
    )
    session.add(farmer)
    session.flush()
    return farmer


# --- registration ------------------------------------------------------


def test_register_creates_a_farmer(client):
    response = register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["phone_number"] == "+254712345678"  # normalised on the way in
    assert body["farmer_id"].startswith("FRM-")
    assert body["role"] == "farmer"
    assert "password" not in body and "password_hash" not in body


def test_register_rejects_a_duplicate_phone_number(client):
    register(client)

    # Same number typed a different way: still the same farmer.
    response = register(client, phone="+254712345678")

    assert response.status_code == 409
    assert "already registered" in response.json()["detail"]


def test_register_rejects_a_short_password(client):
    response = register(client, password="short")

    assert response.status_code == 422


def test_register_rejects_a_non_kenyan_phone_number(client):
    response = register(client, phone="+255712345678")

    assert response.status_code == 422


# --- login -------------------------------------------------------------


def test_login_returns_a_token(client):
    register(client)

    response = login(client)

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] > 0
    assert auth.decode_access_token(body["access_token"])["sub"].startswith("FRM-")


def test_login_with_a_wrong_password_fails(client):
    register(client)

    response = login(client, password="not-my-password")

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect phone number or password"


def test_login_with_an_unknown_phone_number_gives_the_same_error(client):
    """Same wording as a wrong password, so registered numbers cannot be probed."""
    response = login(client, phone="0799999999")

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect phone number or password"


def test_login_accepts_any_accepted_phone_format(client):
    register(client, phone="0712345678")

    assert login(client, phone="254712345678").status_code == 200


# --- /me and tokens ----------------------------------------------------


def test_me_returns_the_logged_in_farmer(client):
    register(client)
    token = login(client).json()["access_token"]

    response = client.get("/me", headers=auth_header(token))

    assert response.status_code == 200
    assert response.json()["phone_number"] == "+254712345678"


def test_me_without_a_token_is_rejected(client):
    assert client.get("/me").status_code == 401


def test_expired_token_is_rejected(client, db_session):
    farmer = make_farmer(db_session, "+254700111222")
    expired = auth.create_access_token(farmer.farmer_id, expires_minutes=-1)

    response = client.get("/me", headers=auth_header(expired))

    assert response.status_code == 401
    assert "expired" in response.json()["detail"].lower()


def test_token_signed_with_another_secret_is_rejected(client, db_session):
    import jwt

    farmer = make_farmer(db_session, "+254700111333")
    forged = jwt.encode({"sub": farmer.farmer_id}, "not-the-real-secret", algorithm="HS256")

    assert client.get("/me", headers=auth_header(forged)).status_code == 401


def test_token_for_a_deleted_account_is_rejected(client):
    token = auth.create_access_token("FRM-DOESNOTEXIST")

    assert client.get("/me", headers=auth_header(token)).status_code == 401


# --- ownership ---------------------------------------------------------


def make_node(session, farmer, location="Kinangop plot"):
    node = SensorNode(
        farmer_id=farmer.farmer_id, location=location, latitude=-0.7, longitude=36.6
    )
    session.add(node)
    session.flush()
    return node


def test_farmer_can_read_their_own_node(client, db_session):
    register(client)
    farmer = db_session.query(Farmer).filter_by(phone_number="+254712345678").one()
    node = make_node(db_session, farmer)
    token = login(client).json()["access_token"]

    response = client.get(f"/nodes/{node.node_id}", headers=auth_header(token))

    assert response.status_code == 200
    assert response.json()["node_id"] == node.node_id


def test_farmer_a_cannot_read_farmer_bs_node(client, db_session):
    register(client)  # farmer A
    farmer_b = make_farmer(db_session, "+254700222333", name="Farmer B")
    node_b = make_node(db_session, farmer_b, location="B's plot")
    token_a = login(client).json()["access_token"]

    response = client.get(f"/nodes/{node_b.node_id}", headers=auth_header(token_a))

    # 404 rather than 403: we do not confirm that someone else's ID exists.
    assert response.status_code == 404


def test_node_list_only_shows_your_own_nodes(client, db_session):
    register(client)
    farmer_a = db_session.query(Farmer).filter_by(phone_number="+254712345678").one()
    node_a = make_node(db_session, farmer_a, location="A's plot")
    farmer_b = make_farmer(db_session, "+254700222444", name="Farmer B")
    make_node(db_session, farmer_b, location="B's plot")
    token_a = login(client).json()["access_token"]

    response = client.get("/nodes", headers=auth_header(token_a))

    assert [n["node_id"] for n in response.json()] == [node_a.node_id]


def test_ownership_rule_covers_records_that_reach_the_farmer_indirectly(db_session):
    """A reading belongs to a farmer through its node, an image directly."""
    from datetime import datetime, timezone

    from backend.models import LeafImage, SensorReading

    farmer_a = make_farmer(db_session, "+254700333555", name="A")
    farmer_b = make_farmer(db_session, "+254700333666", name="B")
    node_a = make_node(db_session, farmer_a)
    reading = SensorReading(
        node_id=node_a.node_id, temperature=12.0, humidity=95.0,
        timestamp=datetime(2026, 3, 1, 6, tzinfo=timezone.utc),
    )
    image = LeafImage(
        farmer_id=farmer_a.farmer_id, file_path="data/uploads/leaf.jpg",
        capture_date=datetime(2026, 3, 1, 9, tzinfo=timezone.utc),
    )
    db_session.add_all([reading, image])
    db_session.flush()

    for record in (node_a, reading, image):
        assert auth.owner_id(db_session, record) == farmer_a.farmer_id
        assert auth.owns(db_session, farmer_a, record)
        assert not auth.owns(db_session, farmer_b, record)


def test_admin_may_read_any_farmers_node(client, db_session):
    admin = make_farmer(db_session, "+254700444555", role=auth.ROLE_ADMIN, name="Admin")
    farmer_b = make_farmer(db_session, "+254700444666", name="Farmer B")
    node_b = make_node(db_session, farmer_b)
    token = auth.create_access_token(admin.farmer_id)

    response = client.get(f"/nodes/{node_b.node_id}", headers=auth_header(token))

    assert response.status_code == 200


def test_ownership_rule_refuses_records_it_has_no_rule_for(db_session):
    """A new table must be added to OWNER_LOOKUPS, not silently allowed."""
    farmer = make_farmer(db_session, "+254700555777")

    with pytest.raises(TypeError):
        auth.owner_id(db_session, object())


# --- admin-only endpoints ---------------------------------------------


def test_admin_can_list_farmers(client, db_session):
    admin = make_farmer(db_session, "+254700666777", role=auth.ROLE_ADMIN, name="Admin")
    token = auth.create_access_token(admin.farmer_id)

    response = client.get("/admin/farmers", headers=auth_header(token))

    assert response.status_code == 200
    assert any(f["role"] == "admin" for f in response.json())


def test_farmer_cannot_list_farmers(client):
    register(client)
    token = login(client).json()["access_token"]

    response = client.get("/admin/farmers", headers=auth_header(token))

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin access required"
