from backend.auth import hash_password, verify_password


def test_hash_and_verify():
    h = hash_password("correct horse")
    assert h != "correct horse"
    assert verify_password("correct horse", h)
    assert not verify_password("wrong", h)
