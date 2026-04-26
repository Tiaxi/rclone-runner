from app.auth import hash_password, verify_password_hash


def test_password_hash_verifies_matching_password():
    password_hash = hash_password("correct horse battery staple")

    assert verify_password_hash("correct horse battery staple", password_hash)
    assert not verify_password_hash("wrong", password_hash)


def test_password_hash_rejects_unknown_format():
    assert not verify_password_hash("password", "bcrypt$not-supported")
