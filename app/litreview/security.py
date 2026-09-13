"""litreview 블루프린트 접근 제어.

배경
----
이 블루프린트에는 `before_request` 도 `login_required` 도 없어서, 인증 없이
연구원 삭제·배치 임의 실행·관리자 기본 저널 설정이 가능했다. 홈페이지와 DB를
공유하는 구조에서는 이게 홈페이지 데이터 사고로 이어진다.

정책
----
    GET             누구나            현행 유지 (대시보드가 로그인 없이 읽는다)
    POST/PUT/
    PATCH/DELETE    로그인 필요       세션의 researcher_id 로 판정
    관리자 엔드포인트 members.is_admin  ADMIN_ENDPOINTS 목록

GET 을 열어 두는 것은 지금 UI 동작을 깨지 않기 위한 선택이다. 연구실 내부
데이터라도 읽기까지 막아야 한다면 REQUIRE_LOGIN_FOR_READ 를 켜면 된다.

'본인 것만' 규칙
---------------
연구원별 자원(키워드·저널·설정)은 본인이거나 관리자만 바꿀 수 있다.
URL 의 <rid> 와 세션의 researcher_id 를 대조한다.
"""

import logging

from flask import jsonify, request, session

logger = logging.getLogger(__name__)

# 읽기에도 로그인을 요구할지. 현재 UI 가 비로그인 상태로 대시보드를 읽으므로 False.
REQUIRE_LOGIN_FOR_READ = False

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 관리자(members.is_admin)만 호출할 수 있는 엔드포인트.
# Blueprint 등록 이름 기준 (함수명과 같다).
ADMIN_ENDPOINTS = {
    "litreview.deactivate_researcher",
    "litreview.enable_researcher",
    "litreview.run_batch_manually",
    "litreview.set_admin_default_journals",
    "litreview.run_journal_discovery_api",
}

# 로그인 없이도 허용할 변경 엔드포인트 (있다면 여기 명시).
# 비워 두는 것이 기본이며, 추가할 때는 이유를 주석으로 남길 것.
PUBLIC_MUTATING_ENDPOINTS: set[str] = set()


def current_researcher_id() -> int | None:
    """로그인한 연구원 id. 비로그인이면 None."""
    rid = session.get("researcher_id")
    return int(rid) if rid else None


def current_researcher():
    """로그인한 연구원 객체. 비로그인이면 None."""
    from app import db
    from app.models import Researcher

    rid = current_researcher_id()
    return db.session.get(Researcher, rid) if rid else None


def is_admin() -> bool:
    """로그인한 사용자가 홈페이지 관리자인지 (members.is_admin)."""
    r = current_researcher()
    return bool(r and r.is_admin)


def _unauthorized(msg: str, code: int):
    return jsonify({"error": msg}), code


def install_guards(bp) -> None:
    """블루프린트에 접근 제어를 건다. init_litreview() 에서 호출한다."""

    @bp.before_request
    def _guard():
        endpoint = request.endpoint or ""

        # CORS 프리플라이트는 통과
        if request.method == "OPTIONS":
            return None

        is_mutating = request.method in MUTATING_METHODS

        if not is_mutating and not REQUIRE_LOGIN_FOR_READ:
            return None

        if endpoint in PUBLIC_MUTATING_ENDPOINTS:
            return None

        rid = current_researcher_id()
        if rid is None:
            logger.warning(
                "인증 없는 %s %s 차단", request.method, request.path
            )
            return _unauthorized("로그인이 필요합니다.", 401)

        if endpoint in ADMIN_ENDPOINTS and not is_admin():
            logger.warning(
                "관리자 아님 — %s %s (researcher_id=%s) 차단",
                request.method, request.path, rid,
            )
            return _unauthorized("관리자 권한이 필요합니다.", 403)

        # 연구원별 자원은 본인 또는 관리자만
        target = request.view_args.get("rid") if request.view_args else None
        if target is not None and int(target) != rid and not is_admin():
            logger.warning(
                "타인 자원 변경 시도 — %s %s (researcher_id=%s → rid=%s) 차단",
                request.method, request.path, rid, target,
            )
            return _unauthorized("본인 또는 관리자만 변경할 수 있습니다.", 403)

        return None

    logger.info(
        "litreview 접근 제어 적용 — 변경 요청은 로그인 필요, 관리자 전용 %d개",
        len(ADMIN_ENDPOINTS),
    )
