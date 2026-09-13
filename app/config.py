"""Flask 애플리케이션 설정. .env 에서 로드."""

import os

from dotenv import load_dotenv

load_dotenv(override=True)


class Config:
    """Base configuration."""

    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "dev-secret-key")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///litreview.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # 추천 수집물 전용 DB (부피 큰 논문·임베딩). main DB 에 ATTACH 해서 붙이므로
    # 파일은 나뉘어도 SQL 조인은 그대로 된다. 자세한 배경은
    # migrations/split_reco_papers_db.py 참고.
    # 상대 경로면 Flask instance 폴더 기준으로 해석한다 (DATABASE_URL 과 동일 규칙).
    PAPERS_DATABASE_PATH = os.environ.get("PAPERS_DATABASE_PATH", "reco_papers.db")
    PAPERS_DATABASE_SCHEMA = "papers"

    # API Keys
    SCOPUS_API_KEY = os.environ.get("SCOPUS_API_KEY", "")
    SCOPUS_INST_TOKEN = os.environ.get("SCOPUS_INST_TOKEN", "")
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
    ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

    # Email (Flask-Mail)
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = os.environ.get("MAIL_USE_TLS", "True").lower() == "true"
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    # 배치 실패 알림 수신자 (미설정 시 MAIL_USERNAME으로 발송)
    ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

    # Scheduler
    SCHEDULER_TIMEZONE = os.environ.get("SCHEDULER_TIMEZONE", "Asia/Seoul")

    # Embedding
    EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
    EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", 1536))

    # Recommendation defaults
    DEFAULT_CORE_PERCENT = float(os.environ.get("DEFAULT_CORE_PERCENT", 5))
    DEFAULT_RELATED_PERCENT = float(os.environ.get("DEFAULT_RELATED_PERCENT", 15))
    DEFAULT_REFERENCE_PERCENT = float(os.environ.get("DEFAULT_REFERENCE_PERCENT", 25))
    DEFAULT_TARGET_YEAR_RANGE = int(os.environ.get("DEFAULT_TARGET_YEAR_RANGE", 3))
    DEFAULT_MAX_PAPERS_PER_QUERY = int(
        os.environ.get("DEFAULT_MAX_PAPERS_PER_QUERY", 200)
    )


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "TEST_DATABASE_URL", "sqlite:///litreview_test.db"
    )


config_by_name = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}
