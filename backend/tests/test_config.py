from backend.config import Settings

REQUIRED = dict(
    DATABASE_URL="postgresql+psycopg://u:p@localhost/a",
    TEST_DATABASE_URL="postgresql+psycopg://u:p@localhost/b",
    JWT_SECRET="s",
    JWT_EXPIRE_MINUTES=60,
    AT_USERNAME="sandbox",
    AT_API_KEY="k",
    AT_SENDER_ID="",
)


def test_site_defaults(monkeypatch):
    for name in ("SITE_LAT", "SITE_LON", "TIMEZONE"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None, **REQUIRED)  # ignore the real .env
    assert s.SITE_LAT == -0.70
    assert s.SITE_LON == 36.60
    assert s.TIMEZONE == "Africa/Nairobi"
