"""Literature Review 블루프린트."""

from flask import Blueprint
from flask_socketio import SocketIO

litreview_bp = Blueprint("litreview", __name__, url_prefix="/litreview")


def init_litreview(sio: SocketIO) -> None:
    """라우트 + SocketIO 이벤트 핸들러 등록. 앱 팩토리에서 호출."""
    from app.litreview import routes  # noqa: F401
    from app.litreview import socketio_handlers  # noqa: F401
