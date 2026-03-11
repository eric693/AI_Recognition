import os
import json
import base64
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.utils import secure_filename
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

# OpenAI API Key 從環境變數讀取，啟動時驗證
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY', '')
if not OPENAI_API_KEY:
    import warnings
    warnings.warn('OPENAI_API_KEY 環境變數未設定，AI 辨識功能將無法使用')

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'invoice-secret-key-change-me')
from datetime import timedelta
app.permanent_session_lifetime = timedelta(hours=8)

# ── DATABASE URL ───────────────────────────────────────────────────────────────
# Render provides DATABASE_URL starting with "postgres://" (legacy).
# SQLAlchemy 1.4+ requires "postgresql://". Fix automatically.
raw_db_url = os.environ.get('DATABASE_URL', 'sqlite:///invoices.db')
if raw_db_url.startswith('postgres://'):
    raw_db_url = raw_db_url.replace('postgres://', 'postgresql://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = raw_db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Connection pool — PostgreSQL on Render free tier closes idle connections at 5 min
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,
    'pool_recycle': 280,
    'pool_size': 5,
    'max_overflow': 10,
    **(
        {'connect_args': {'sslmode': 'require'}}
        if raw_db_url.startswith('postgresql') else {}
    ),
}

app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'webp'}

db = SQLAlchemy(app)


# ── MODEL ──────────────────────────────────────────────────────────────────────
class Invoice(db.Model):
    __tablename__ = 'invoices'

    id             = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50),     nullable=True, index=True)
    invoice_date   = db.Column(db.String(20),     nullable=True)
    issuer         = db.Column(db.String(200),    nullable=True, index=True)
    item_name      = db.Column(db.Text,           nullable=True)
    sales_amount   = db.Column(db.Numeric(14, 2), nullable=True)
    tax_amount     = db.Column(db.Numeric(14, 2), nullable=True)
    total_amount   = db.Column(db.Numeric(14, 2), nullable=True)
    notes          = db.Column(db.Text,           nullable=True)
    raw_response   = db.Column(db.Text,           nullable=True)
    image_filename = db.Column(db.String(300),    nullable=True)
    created_at     = db.Column(db.DateTime,       default=datetime.utcnow, index=True)
    status         = db.Column(db.String(20),     default='pending', index=True)

    def to_dict(self):
        return {
            'id':             self.id,
            'invoice_number': self.invoice_number or '',
            'invoice_date':   self.invoice_date   or '',
            'issuer':         self.issuer         or '',
            'item_name':      self.item_name      or '',
            'sales_amount':   float(self.sales_amount) if self.sales_amount is not None else None,
            'tax_amount':     float(self.tax_amount)   if self.tax_amount   is not None else None,
            'total_amount':   float(self.total_amount) if self.total_amount is not None else None,
            'notes':          self.notes          or '',
            'image_filename': self.image_filename or '',
            'created_at':     self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
            'status':         self.status,
        }


with app.app_context():
    db.create_all()


# ── HELPERS ────────────────────────────────────────────────────────────────────
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def encode_image(filepath):
    with open(filepath, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def analyze_invoice(filepath):
    client = OpenAI(api_key=OPENAI_API_KEY)
    ext = filepath.rsplit('.', 1)[-1].lower()

    media_map = {'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
                 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}
    media_type = media_map.get(ext, 'image/jpeg')
    image_data = encode_image(filepath)

    prompt = """請分析這張發票圖片，提取以下資訊並以 JSON 格式回傳。
欄位說明：
- invoice_number: 發票編號（統一發票號碼或收據編號）
- invoice_date: 發票日期（格式 YYYY-MM-DD，若為民國年請轉換為西元年）
- issuer: 發票人 / 廠商名稱
- item_name: 品名（若多項請以逗號分隔）
- sales_amount: 銷售額（未稅金額，純數字）
- tax_amount: 稅額（純數字）
- total_amount: 總計金額（含稅，純數字）
- notes: 備註

若某欄位無法辨識，請填入 null。
請只回傳 JSON，不包含任何其他文字或 markdown。
範例：
{
  "invoice_number": "AB12345678",
  "invoice_date": "2024-03-11",
  "issuer": "某某股份有限公司",
  "item_name": "商品A, 商品B",
  "sales_amount": 1000,
  "tax_amount": 50,
  "total_amount": 1050,
  "notes": null
}"""

    response = client.chat.completions.create(
        model='gpt-4o',
        messages=[{
            'role': 'user',
            'content': [
                {'type': 'image_url', 'image_url': {
                    'url': f'data:{media_type};base64,{image_data}',
                    'detail': 'high'
                }},
                {'type': 'text', 'text': prompt}
            ]
        }],
        max_tokens=1000,
        temperature=0,
    )

    content = response.choices[0].message.content.strip()
    content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
    content = re.sub(r'\s*```$', '', content, flags=re.MULTILINE)
    return content.strip()


# ── AUTH ──────────────────────────────────────────────────────────────────────
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

from functools import wraps
from flask import session, redirect, url_for

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


# ── ROUTES ─────────────────────────────────────────────────────────────────────
@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('index'))
        error = '密碼錯誤，請再試一次'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/health')
@login_required
def health():
    try:
        db.session.execute(text('SELECT 1'))
        db_ok = True
    except Exception:
        db_ok = False
    return jsonify({'status': 'ok', 'db': db_ok}), 200


@app.route('/upload', methods=['POST'])
@login_required
def upload():
    if not OPENAI_API_KEY:
        return jsonify({'success': False, 'error': '伺服器未設定 OPENAI_API_KEY，請聯絡管理員'}), 500

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'error': '請選擇至少一個檔案'}), 400

    results = []
    for file in files:
        if not (file and file.filename and allowed_file(file.filename)):
            results.append({'filename': file.filename or '未知', 'success': False,
                            'error': '不支援的檔案格式'})
            continue

        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
        filename = f'{timestamp}_{filename}'
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)

        try:
            raw = analyze_invoice(filepath)
            data = json.loads(raw)

            invoice = Invoice(
                invoice_number=data.get('invoice_number'),
                invoice_date=data.get('invoice_date'),
                issuer=data.get('issuer'),
                item_name=data.get('item_name'),
                sales_amount=data.get('sales_amount'),
                tax_amount=data.get('tax_amount'),
                total_amount=data.get('total_amount'),
                notes=data.get('notes'),
                raw_response=raw,
                image_filename=filename,
                status='pending',
            )
            db.session.add(invoice)
            db.session.commit()
            results.append({'filename': file.filename, 'success': True,
                            'id': invoice.id, 'data': invoice.to_dict()})
        except json.JSONDecodeError as e:
            db.session.rollback()
            results.append({'filename': file.filename, 'success': False,
                            'error': f'AI 回傳格式錯誤：{e}'})
        except Exception as e:
            db.session.rollback()
            results.append({'filename': file.filename, 'success': False, 'error': str(e)})

    return jsonify({'success': True, 'results': results})


@app.route('/invoices')
@login_required
def get_invoices():
    page     = request.args.get('page',     1,  type=int)
    per_page = request.args.get('per_page', 20, type=int)
    status   = request.args.get('status',   '')
    search   = request.args.get('search',   '').strip()

    query = Invoice.query
    if status:
        query = query.filter(Invoice.status == status)
    if search:
        like = f'%{search}%'
        query = query.filter(db.or_(
            Invoice.invoice_number.ilike(like),
            Invoice.issuer.ilike(like),
            Invoice.item_name.ilike(like),
        ))
    query = query.order_by(Invoice.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'invoices':     [inv.to_dict() for inv in pagination.items],
        'total':        pagination.total,
        'pages':        pagination.pages,
        'current_page': page,
    })


@app.route('/invoice/<int:invoice_id>', methods=['GET'])
@login_required
def get_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return jsonify(invoice.to_dict())


@app.route('/invoice/<int:invoice_id>', methods=['PUT'])
@login_required
def update_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    data = request.get_json(force=True)

    invoice.invoice_number = data.get('invoice_number', invoice.invoice_number)
    invoice.invoice_date   = data.get('invoice_date',   invoice.invoice_date)
    invoice.issuer         = data.get('issuer',         invoice.issuer)
    invoice.item_name      = data.get('item_name',      invoice.item_name)
    invoice.notes          = data.get('notes',          invoice.notes)
    invoice.status         = data.get('status',         invoice.status)

    for field in ('sales_amount', 'tax_amount', 'total_amount'):
        if field in data:
            val = data[field]
            setattr(invoice, field, float(val) if val not in (None, '') else None)

    db.session.commit()
    return jsonify({'success': True, 'invoice': invoice.to_dict()})


@app.route('/invoice/<int:invoice_id>', methods=['DELETE'])
@login_required
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    if invoice.image_filename:
        path = os.path.join(app.config['UPLOAD_FOLDER'], invoice.image_filename)
        if os.path.exists(path):
            os.remove(path)
    db.session.delete(invoice)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/stats')
@login_required
def get_stats():
    total     = Invoice.query.count()
    pending   = Invoice.query.filter_by(status='pending').count()
    confirmed = Invoice.query.filter_by(status='confirmed').count()
    rejected  = Invoice.query.filter_by(status='rejected').count()
    total_amount = (
        db.session.query(db.func.sum(Invoice.total_amount))
        .filter(Invoice.status == 'confirmed').scalar() or 0
    )
    return jsonify({
        'total':        total,
        'pending':      pending,
        'confirmed':    confirmed,
        'rejected':     rejected,
        'total_amount': round(float(total_amount), 2),
    })


@app.route('/uploads/<path:filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)