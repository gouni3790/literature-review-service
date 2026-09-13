"""web_server.py와 똑같이 설정해서 /litreview 라우트만 테스트."""
import os
import sys
import traceback

os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

from flask import Flask, render_template
from flask_socketio import SocketIO

from dotenv import load_dotenv
load_dotenv(override=True)

from app import db, migrate, mail
from app.litreview import litreview_bp, init_litreview
from app.api_lr import api_lr_bp

current_dir = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__,
            static_folder=os.path.join(current_dir, 'static', 'static'),
            template_folder=os.path.join(current_dir, 'templates'))
app.secret_key = "test"
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['TESTING'] = True
app.config['PROPAGATE_EXCEPTIONS'] = True

# 새 구조 설정
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///litreview.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SCOPUS_API_KEY'] = os.environ.get('SCOPUS_API_KEY', '')
app.config['SCOPUS_INST_TOKEN'] = os.environ.get('SCOPUS_INST_TOKEN', '')
app.config['OPENAI_API_KEY'] = os.environ.get('OPENAI_API_KEY', '')
app.config['ANTHROPIC_API_KEY'] = os.environ.get('ANTHROPIC_API_KEY', '')
app.config['EMBEDDING_MODEL'] = os.environ.get('EMBEDDING_MODEL', 'text-embedding-3-small')
app.config['EMBEDDING_DIM'] = int(os.environ.get('EMBEDDING_DIM', 1536))

db.init_app(app)
migrate.init_app(app, db)
mail.init_app(app)

socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")
init_litreview(socketio)
app.register_blueprint(litreview_bp)
app.register_blueprint(api_lr_bp)


@app.route("/litreview", methods=['GET'])
def serve_litreview():
    return render_template('home.html')


print("=== Registered routes ===")
for rule in app.url_map.iter_rules():
    print(f"  {rule.rule:50s} -> {rule.endpoint}")

print("\n=== Test /litreview ===")
try:
    with app.test_client() as client:
        resp = client.get('/litreview')
        print(f'Status: {resp.status_code}')
        if resp.status_code >= 400:
            print(resp.data.decode('utf-8', errors='replace')[:3000])
except Exception:
    traceback.print_exc()
