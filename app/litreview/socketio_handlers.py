"""SocketIO 이벤트 핸들러 — 룸 관리 + 진행 상태."""

import logging

from flask_socketio import emit, join_room, leave_room

from app import socketio

logger = logging.getLogger(__name__)


# =========================================================================
# 룸 관리
# =========================================================================


@socketio.on("join")
def handle_join(data):
    """클라이언트 룸 참가.

    data: {"room": "researcher:1"} 또는 {"room": "admin"}
    """
    room = data.get("room", "")
    if not room:
        return

    join_room(room)
    logger.debug("Client joined room: %s", room)
    emit("joined", {"room": room})


@socketio.on("leave")
def handle_leave(data):
    """클라이언트 룸 탈퇴."""
    room = data.get("room", "")
    if not room:
        return

    leave_room(room)
    logger.debug("Client left room: %s", room)


@socketio.on("connect")
def handle_connect():
    """연결 시 기본 admin 룸 참가."""
    logger.debug("Client connected")
    emit("connected", {"status": "ok"})


@socketio.on("disconnect")
def handle_disconnect():
    logger.debug("Client disconnected")


# =========================================================================
# 진행 상태 emit 헬퍼 (다른 모듈에서 import)
# =========================================================================


def emit_progress(event: str, payload: dict, room: str):
    """배치/수집 코드에서 호출. 실패해도 본 작업 영향 없음."""
    try:
        socketio.emit(event, payload, to=room)
    except Exception as e:
        logger.warning("socketio emit failed: %s", e)


def emit_scopus_progress(researcher_id: int, done: int, total: int):
    """Scopus 수집 진행률."""
    emit_progress(
        "scopus.collect.progress",
        {"researcher_id": researcher_id, "done": done, "total": total},
        room=f"researcher:{researcher_id}",
    )


def emit_cluster_progress(researcher_id: int, step: str, detail: dict | None = None):
    """클러스터링 진행 상태."""
    payload = {"researcher_id": researcher_id, "step": step}
    if detail:
        payload.update(detail)
    emit_progress("cluster.progress", payload, room=f"researcher:{researcher_id}")


def emit_batch_progress(job: str, step: str, message: str):
    """배치 작업 진행 상태."""
    emit_progress(
        "batch.progress",
        {"job": job, "step": step, "message": message},
        room="admin",
    )


def emit_new_recommendation(researcher_id: int, core: int, related: int, reference: int):
    """새 추천 도착 알림."""
    emit_progress(
        "recommend.new",
        {
            "researcher_id": researcher_id,
            "core": core,
            "related": related,
            "reference": reference,
        },
        room=f"researcher:{researcher_id}",
    )
