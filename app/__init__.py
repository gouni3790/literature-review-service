"""Flask 앱 팩토리 + 확장 초기화."""

import logging
import os

from flask import Flask, redirect, render_template
from flask_mail import Mail
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()
mail = Mail()
socketio = SocketIO()

logger = logging.getLogger(__name__)


def create_app(config_name: str | None = None) -> Flask:
    """Flask 앱 생성. config_name: 'development' | 'production' | 'testing'."""
    from app.config import config_by_name

    if config_name is None:
        config_name = os.environ.get("FLASK_ENV", "development")

    app = Flask(__name__, template_folder="../templates")
    app.config.from_object(config_by_name[config_name])

    _init_logging(app)
    _init_extensions(app)
    _register_blueprints(app)

    return app


def _init_logging(app: Flask) -> None:
    log_level = logging.DEBUG if app.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _init_extensions(app: Flask) -> None:
    db.init_app(app)
    init_papers_db(app)
    migrate.init_app(app, db)
    mail.init_app(app)
    socketio.init_app(app, async_mode="threading", cors_allowed_origins="*")


def resolve_papers_db_path(app: Flask) -> str:
    """reco_papers.db 절대 경로. 상대 경로는 instance 폴더 기준으로 푼다."""
    raw = app.config.get("PAPERS_DATABASE_PATH") or "reco_papers.db"
    if not os.path.isabs(raw):
        raw = os.path.join(app.instance_path, raw)
    return os.path.abspath(raw)


def init_papers_db(app: Flask) -> None:
    """수집물 DB(reco_papers.db)를 main 커넥션에 ATTACH 한다.

    ★ 이 앱을 create_app() 으로 만들지 않는 호스트(운영 서버의 Web_server.py)는
      **db.init_app(app) 직후에 이 함수를 반드시 직접 호출해야 한다.**
      호출하지 않으면 papers 스키마가 붙지 않아 수집물 테이블 접근이 전부
      "no such table: papers.reco_collected_papers" 로 실패한다.

          from app import db, init_papers_db
          db.init_app(app)
          init_papers_db(app)      # <- 이 줄

      여러 번 불러도 안전하다 (중복 등록을 건너뛴다).

    왜 bind 가 아니라 ATTACH 인가
    ----------------------------
    Flask-SQLAlchemy 의 __bind_key__ 는 파일마다 **별도 엔진**을 만들기 때문에
    파일 경계를 넘는 SQL 조인이 아예 불가능하다. 그런데 이 앱에는 그런 조인이
    이미 있다 (예: journal_analyzer.get_feedback_journal_freq 의
    PaperRecommendation ⋈ CollectedPaper). ATTACH 는 커넥션 하나에 두 파일을
    붙이므로 `JOIN papers.reco_collected_papers` 가 SQLite 레벨에서 그대로
    처리되고, 앱의 조인 코드는 손댈 필요가 없다.

    주의 — 원자성
    -------------
    bist.db 가 WAL 모드라, 두 파일에 걸친 트랜잭션은 **원자적이지 않다**
    (SQLite 제약). 파이프라인이 CollectedPaper 저장과 PaperRecommendation
    저장 사이에서 프로세스가 죽으면 한쪽만 남을 수 있다. 데이터가 깨지는 것은
    아니고 재실행으로 복구되는 종류의 불일치다.
    """
    from sqlalchemy import event

    papers_path = resolve_papers_db_path(app)
    os.makedirs(os.path.dirname(papers_path), exist_ok=True)
    schema = app.config.get("PAPERS_DATABASE_SCHEMA", "papers")

    if not os.path.exists(papers_path):
        logger.warning(
            "수집물 DB가 없어 새로 만들어집니다: %s "
            "(기존 bist.db 에서 옮기려면 migrations/split_reco_papers_db.py 실행)",
            papers_path,
        )

    # SQLite 문자열 리터럴 이스케이프 — 경로에 작은따옴표가 있어도 안전하게
    escaped = papers_path.replace("'", "''")
    attach_sql = f"ATTACH DATABASE '{escaped}' AS {schema}"

    if app.config.get("PAPERS_DATABASE_ATTACHED"):
        logger.debug("papers DB 가 이미 붙어 있어 건너뜁니다")
        return

    with app.app_context():
        engine = db.engine

        @event.listens_for(engine, "connect")
        def _attach_papers_db(dbapi_connection, _connection_record):
            """새 DBAPI 커넥션마다 ATTACH. 스레드별 커넥션에도 모두 적용된다."""
            dbapi_connection.execute(attach_sql)

        # 리스너 등록 전에 열린 커넥션이 풀에 있으면 ATTACH 가 안 돼 있으므로 버린다
        engine.dispose()

    app.config["PAPERS_DATABASE_ATTACHED"] = True

    app.config["PAPERS_DATABASE_RESOLVED_PATH"] = papers_path
    logger.info("Attached papers DB as '%s': %s", schema, papers_path)


def _register_blueprints(app: Flask) -> None:
    from app.api_lr import api_lr_bp
    from app.litreview import litreview_bp, init_litreview

    init_litreview(socketio)
    app.register_blueprint(litreview_bp)
    app.register_blueprint(api_lr_bp)

    @app.route("/")
    def index():
        return redirect("/home")

    @app.route("/home")
    @app.route("/home3")  # 구 URL 호환 (승격 전 공유된 링크용)
    def home_page():
        """화이트 테마 3단 레이아웃 (home_v3)."""
        return render_template("home_v3.html")

    @app.route("/home2")
    def home_v2_page():
        """이전 다크 테마 UI (백업용)."""
        return render_template("home_v2.html")

    @app.route("/mypage")
    def mypage():
        """마이페이지 — 추천 주기·검색 키워드·연구 설명·관심 저자 관리."""
        return render_template("mypage.html")

    @app.route("/litreview/login")
    def login_page():
        return render_template("login.html")

    @app.route("/litreview/profile")
    def profile_page():
        return render_template("profile.html")
