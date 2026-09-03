import os
import logging

from flask import Flask, request, jsonify, render_template
from flask_jwt_extended import (
    JWTManager, create_access_token, jwt_required, get_jwt_identity,
)
from cryptography.fernet import Fernet
from werkzeug.utils import secure_filename

from models import db, DocumentRecord, User
from ocr_engine import SecureOCRPlatform

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("SecureOCR.API")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'ocr_platform.db')

app = Flask(__name__)
app.config['SECRET_KEY'] = 'CHANGE_ME_super_long_secret_key_0123456789'
app.config['JWT_SECRET_KEY'] = 'CHANGE_ME_jwt_secret_key_0123456789abcdef'
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024

db.init_app(app)
jwt = JWTManager(app)

ENCRYPTION_KEY = os.environ.get('OCR_ENCRYPTION_KEY') or Fernet.generate_key().decode()
fernet = Fernet(ENCRYPTION_KEY.encode())

DEFAULT_LANGUAGES = ['en', 'ar']


def get_platform():
    return SecureOCRPlatform(languages=DEFAULT_LANGUAGES)


with app.app_context():
    db.create_all()


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/register', methods=['POST'])
def register():
    data = request.get_json()
    username = (data or {}).get('username')
    password = (data or {}).get('password')
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "username already exists"}), 409
    user = User(username=username, password=password)
    db.session.add(user)
    db.session.commit()
    return jsonify({"message": "user created"}), 201


@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json()
    username = (data or {}).get('username')
    password = (data or {}).get('password')
    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({"error": "invalid credentials"}), 401
    token = create_access_token(identity=str(user.id))
    return jsonify({"access_token": token}), 200


@app.route('/api/ocr', methods=['POST'])
@jwt_required()
def ocr_upload():
    user_id = get_jwt_identity()
    if 'file' not in request.files:
        return jsonify({"error": "no file provided"}), 400

    file = request.files['file']
    file_bytes = file.read()

    try:
        platform = get_platform()
        blocks = platform.extract_text(file_bytes)
        response = platform.compile_and_extract_entities(blocks)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.error(f"OCR processing error: {e}")
        return jsonify({"error": "internal OCR processing failure"}), 500

    encrypted_raw = fernet.encrypt(response.raw_text.encode('utf-8'))

    filename = secure_filename(file.filename or 'upload')
    record = DocumentRecord(
        user_id=user_id,
        filename=filename,
        encrypted_content_hex=encrypted_raw.hex(),
        raw_text_secure=False,
        average_confidence=response.average_confidence,
        entities_json=str(response.structured_entities),
    )
    db.session.add(record)
    db.session.commit()

    return jsonify({
        "document_id": record.id,
        "raw_text": response.raw_text,
        "structured_entities": response.structured_entities,
        "average_confidence": response.average_confidence,
        "review_warning": response.review_warning,
    }), 200


@app.route('/api/documents', methods=['GET'])
@jwt_required()
def list_documents():
    user_id = get_jwt_identity()
    docs = DocumentRecord.query.filter_by(user_id=user_id).all()
    result = []
    for d in docs:
        try:
            decrypted = fernet.decrypt(bytes.fromhex(d.encrypted_content_hex)).decode('utf-8')
        except Exception:
            decrypted = "[cannot decrypt]"
        result.append({
            "id": d.id,
            "filename": d.filename,
            "average_confidence": d.average_confidence,
            "created_at": d.created_at.strftime('%Y-%m-%d %H:%M') if d.created_at else None,
            "extracted_text": decrypted,
        })
    return jsonify({"documents": result}), 200


@app.route('/api/documents/<int:doc_id>', methods=['DELETE'])
@jwt_required()
def delete_document(doc_id):
    user_id = get_jwt_identity()
    doc = DocumentRecord.query.filter_by(id=doc_id, user_id=user_id).first()
    if not doc:
        return jsonify({"error": "document not found"}), 404
    db.session.delete(doc)
    db.session.commit()
    return jsonify({"message": "deleted"}), 200


@app.route('/api/account/change-password', methods=['POST'])
@jwt_required()
def change_password():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404

    data = request.get_json() or {}
    old_password = data.get('old_password')
    new_password = data.get('new_password')

    if not old_password or not new_password:
        return jsonify({"error": "old_password and new_password are required"}), 400
    if not user.check_password(old_password):
        return jsonify({"error": "current password is incorrect"}), 401
    if len(new_password) < 6:
        return jsonify({"error": "new password must be at least 6 characters"}), 400
    if new_password == old_password:
        return jsonify({"error": "new password must be different from current password"}), 400

    user.set_password(new_password)
    db.session.commit()
    return jsonify({"message": "password updated successfully"}), 200


@app.route('/api/account', methods=['DELETE'])
@jwt_required()
def delete_account():
    user_id = get_jwt_identity()
    user = User.query.get(user_id)
    if not user:
        return jsonify({"error": "user not found"}), 404

    DocumentRecord.query.filter_by(user_id=user_id).delete()
    db.session.delete(user)
    db.session.commit()
    return jsonify({"message": "account deleted"}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
