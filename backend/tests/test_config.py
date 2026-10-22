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


def test_plain_postgres_urls_get_the_psycopg_driver():
    for given in ("postgres://u:p@host/db?sslmode=require", "postgresql://u:p@host/db?sslmode=require"):
        s = Settings(_env_file=None, **{**REQUIRED, "DATABASE_URL": given})
        assert s.DATABASE_URL == "postgresql+psycopg://u:p@host/db?sslmode=require"
    s = Settings(_env_file=None, **REQUIRED)
    assert s.DATABASE_URL == REQUIRED["DATABASE_URL"]  # already right: unchanged


def test_a_server_may_leave_the_test_database_unset(monkeypatch):
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    required = {k: v for k, v in REQUIRED.items() if k != "TEST_DATABASE_URL"}
    assert Settings(_env_file=None, **required).TEST_DATABASE_URL == ""
