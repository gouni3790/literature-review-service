"""Literature Review 블루프린트."""

from flask import Blueprint
from flask_socketio import SocketIO

litreview_bp = Blueprint("litreview", __name__, url_prefix="/litreview")


def init_litreview(sio: SocketIO) -> None:
    """라우트 + 접근 제어 + SocketIO 이벤트 핸들러 등록. 앱 팩토리에서 호출.

    운영 서버의 Web_server.py 도 이 함수를 부르므로, 여기서 접근 제어를 걸면
    create_app() 경로와 Web_server.py 경로 양쪽에 동일하게 적용된다.
    """
    from app.litreview import routes  # noqa: F401
    from app.litreview import socketio_handlers  # noqa: F401
    from app.litreview.security import install_guards

    install_guards(litreview_bp)
