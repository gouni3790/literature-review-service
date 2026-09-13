# ============================================================================
# Web_server.py 에 반영할 literature_v2 연동 코드 — 2026-09-14 기준
#
# 이 파일은 **실행 파일이 아니라 참조용**입니다. 운영 서버의
#   GPT-based-building-simulator-master/Web_server.py
# 에 아래 내용을 옮겨 넣으세요.
#
# 왜 패키지 복사만으로 안 되는가
# ------------------------------
# 운영 서버는 literature_v2/app/__init__.py 의 create_app() 을 쓰지 않고,
# Web_server.py 가 자체 Flask 앱에 블루프린트를 붙여 서빙합니다. 그래서
# create_app() 안에서만 하는 일(ATTACH 초기화, 페이지 라우트 등록,
# 템플릿 폴더 지정)은 Web_server.py 에 직접 옮겨야 합니다.
#
# 아래 [1]~[5] 중 [1] 과 [2] 는 빠뜨리면 서비스가 동작하지 않습니다.
# ============================================================================


# ---------------------------------------------------------------------------
# [1] import — init_papers_db 추가  ★필수
# ---------------------------------------------------------------------------
# 수집물 테이블(논문·임베딩)이 bist.db 에서 reco_papers.db 로 분리됐습니다.
# 런타임에 ATTACH DATABASE 로 두 파일을 한 커넥션에 붙이는데, 그 초기화가
# create_app() 안에 있습니다. 서버는 create_app() 을 쓰지 않으므로
# init_papers_db(app) 을 직접 불러야 합니다.
#
# 부르지 않으면 수집물 접근이 전부 이렇게 실패합니다:
#     no such table: papers.reco_collected_papers

# 변경 전
#   from app import db, migrate, mail
# 변경 후
from app import db, migrate, mail, init_papers_db          # noqa: F401


# ---------------------------------------------------------------------------
# [2] 확장 초기화 — init_papers_db(app) 호출  ★필수
# ---------------------------------------------------------------------------
# db.init_app(app) **바로 다음**에 넣습니다. 여러 번 불러도 안전합니다.

# 변경 전
#   db.init_app(app)
#   migrate.init_app(app, db)
#   mail.init_app(app)
# 변경 후
"""
db.init_app(app)
init_papers_db(app)          # <-- 추가
migrate.init_app(app, db)
mail.init_app(app)
"""


# ---------------------------------------------------------------------------
# [3] 템플릿 폴더 — literature_v2/templates 를 추가 로더로 붙인다  ★필수
# ---------------------------------------------------------------------------
# Web_server.py 는 Flask(__name__) 으로 앱을 만들므로 템플릿 폴더가
# **프로젝트 루트 templates/** 입니다. 반면 이번 UI(home_v3.html, mypage.html,
# _theme_css.html 등)는 literature_v2/templates/ 에 들어 있습니다.
#
# 파일을 루트로 복사하는 대신 로더를 하나 더 붙이는 쪽을 권합니다.
#   - 같은 파일이 두 곳에 존재하지 않습니다 (한쪽만 고쳐 어긋나는 사고 방지)
#   - literature_v2 를 git 으로 갱신하면 UI 도 함께 갱신됩니다
#   - 루트 templates/ 의 다른 모듈 템플릿(HVAC 등)은 그대로 동작합니다
#
# 앱 생성 직후, 블루프린트 등록 전에 넣습니다.
"""
from jinja2 import ChoiceLoader, FileSystemLoader

app.jinja_loader = ChoiceLoader([
    app.jinja_loader,                                        # 루트 templates/
    FileSystemLoader(os.path.join(_LIT_V2, 'templates')),    # literature_v2/templates/
])
"""


# ---------------------------------------------------------------------------
# [4] 페이지 라우트 — /home 템플릿 교체 + 신규 3개
# ---------------------------------------------------------------------------
# create_app() 이 등록하는 페이지 라우트 전체입니다. 서버에 없는 것을 추가하고,
# /home 이 렌더링하는 템플릿을 home_v3.html 로 바꿉니다.
#
# home_v3 로 바꾸지 않으면 이번 변경(저널 모니터링·API 사용 비용·관심 저자 탭)이
# 화면에 전혀 나타나지 않습니다. home_v2.html 도 함께 배포되므로 되돌리기는
# 언제든 가능합니다.
"""
# --- 변경: home_v2.html -> home_v3.html ---
@app.route("/literature", methods=['GET'])
@app.route("/litreview", methods=['GET'])
@app.route("/home", methods=['GET'])
@app.route("/home3", methods=['GET'])          # 승격 전 공유된 링크 호환
def serve_litreview():
    return render_template('home_v3.html')


# --- 신규: 이전 UI 를 남겨 두는 경로 (문제 시 여기로 안내) ---
@app.route("/home2", methods=['GET'])
def serve_litreview_legacy():
    return render_template('home_v2.html')


# --- 신규: 마이페이지 (추천 주기·검색 키워드·연구 설명·관심 저자 관리) ---
@app.route("/mypage", methods=['GET'])
def serve_mypage():
    return render_template('mypage.html')


# --- 변경: 로그인 화면이 tab 인자를 받는다 ---
@app.route("/literature/login", methods=['GET'])
@app.route("/litreview/login", methods=['GET'])
def serve_login():
    return render_template('login.html', tab='login')


# --- 신규: 회원가입 (같은 템플릿의 회원가입 탭) ---
@app.route("/litreview/register", methods=['GET'])
def serve_register():
    return render_template('login.html', tab='register')
"""

# 기존 /literature/profile, /litreview/profile, /litreview/recommendations 는
# 그대로 두면 됩니다.


# ---------------------------------------------------------------------------
# [5] .env — DATABASE_URL 을 bist.db 로 명시  ★필수
# ---------------------------------------------------------------------------
# Web_server.py 의 기본값은 구 스키마(litreview.db)입니다. 이번 코드는 신 스키마
# (bist.db) 전용이라, .env 에 DATABASE_URL 이 없으면 구 DB 에 붙어 동작하지
# 않습니다. **절대 경로**로 적으십시오.
#
#   DATABASE_URL=sqlite:///C:/Users/BIST_WORKSTATION/PycharmProjects/GPT-based-building-simulator-master/literature_v2/instance/bist.db
#
# 선택: 수집물 DB 위치를 옮기려면
#   PAPERS_DATABASE_PATH=<절대경로>/reco_papers.db
#   (생략하면 instance/reco_papers.db 를 씁니다 — 보통 생략해도 됩니다)
#
# 보안: FLASK_SECRET_KEY 를 랜덤 값으로 설정하십시오. 미설정 시 코드에 박힌
# "dev-secret-key" 가 쓰여 세션 쿠키 위조가 가능합니다.


# ---------------------------------------------------------------------------
# 건드리지 말 것
# ---------------------------------------------------------------------------
# - BIST_Office/ (Unity WebGL 빌드, 약 206MB) 는 배포 패키지에 없습니다.
#   폴더째 덮어쓰면 /bist-office/ 가 404 가 되어 VR 진입이 깨집니다.
# - instance/*.db 는 서버 것을 그대로 둡니다. 스키마 변경은 migrations/ 로만
#   전달됩니다.


# ---------------------------------------------------------------------------
# 반영 후 확인
# ---------------------------------------------------------------------------
# 서버에서 preflight_check.py 를 실행하면 [1][2][4][5] 반영 여부와 남은
# 마이그레이션을 한 번에 점검해 줍니다.
#
#     python preflight_check.py
#
# 그 다음 브라우저에서:
#     /home                      대시보드에 '저널 모니터링' / 'API 사용 비용' 카드
#     /mypage                    마이페이지
#     /litreview/register        회원가입 탭
#     /api/lr/journal-monitoring JSON 응답
#     /api/lr/api-usage          JSON 응답
