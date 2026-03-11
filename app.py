import os
import json
import base64
import re
from datetime import datetime
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'invoice-secret-key-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///invoices.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB
UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'pdf', 'webp'}

db = SQLAlchemy(app)


class Invoice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    invoice_number = db.Column(db.String(50), nullable=True)
    invoice_date = db.Column(db.String(20), nullable=True)
    issuer = db.Column(db.String(200), nullable=True)
    item_name = db.Column(db.Text, nullable=True)
    sales_amount = db.Column(db.Float, nullable=True)
    tax_amount = db.Column(db.Float, nullable=True)
    total_amount = db.Column(db.Float, nullable=True)
    notes = db.Column(db.Text, nullable=True)
    raw_response = db.Column(db.Text, nullable=True)
    image_filename = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='pending')  # pending, confirmed, rejected

    def to_dict(self):
        return {
            'id': self.id,
            'invoice_number': self.invoice_number or '',
            'invoice_date': self.invoice_date or '',
            'issuer': self.issuer or '',
            'item_name': self.item_name or '',
            'sales_amount': self.sales_amount,
            'tax_amount': self.tax_amount,
            'total_amount': self.total_amount,
            'notes': self.notes or '',
            'image_filename': self.image_filename or '',
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
            'status': self.status,
        }


with app.app_context():
    db.create_all()


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def encode_image(filepath):
    with open(filepath, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')


def analyze_invoice(filepath, api_key):
    client = OpenAI(api_key=api_key)
    ext = filepath.rsplit('.', 1)[-1].lower()
    media_type = 'image/jpeg' if ext in ['jpg', 'jpeg'] else f'image/{ext}'
    if ext == 'pdf':
        media_type = 'application/pdf'

    image_data = encode_image(filepath)

    prompt = """請分析這張發票圖片，提取以下資訊並以 JSON 格式回傳。
欄位說明：
- invoice_number: 發票編號（統一發票號碼或收據編號）
- invoice_date: 發票日期（格式 YYYY-MM-DD，若為民國年請轉換為西元年）
- issuer: 發票人 / 廠商名稱
- item_name: 品名（若多項請以逗號分隔）
- sales_amount: 銷售額（未稅金額，數字）
- tax_amount: 稅額（數字）
- total_amount: 總計金額（含稅，數字）
- notes: 備註（其他特殊資訊）

若某欄位無法辨識，請填入 null。
請只回傳 JSON，不要包含其他文字。
範例格式：
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

    if ext == 'pdf':
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_data}"
                            }
                        }
                    ]
                }
            ],
            max_tokens=1000
        )
    else:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{media_type};base64,{image_data}"
                            }
                        },
                        {
                            "type": "text",
                            "text": prompt
                        }
                    ]
                }
            ],
            max_tokens=1000
        )

    content = response.choices[0].message.content.strip()
    # Clean up markdown code blocks if present
    content = re.sub(r'^```(?:json)?\s*', '', content)
    content = re.sub(r'\s*```$', '', content)
    return content


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/upload', methods=['POST'])
def upload():
    api_key = request.form.get('api_key', '').strip()
    if not api_key:
        return jsonify({'success': False, 'error': '請輸入 OpenAI API Key'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'error': '請選擇至少一個檔案'}), 400

    results = []
    for file in files:
        if file and file.filename and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S%f')
            filename = f"{timestamp}_{filename}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
            file.save(filepath)

            try:
                raw = analyze_invoice(filepath, api_key)
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
                    status='pending'
                )
                db.session.add(invoice)
                db.session.commit()
                results.append({'filename': file.filename, 'success': True, 'id': invoice.id, 'data': invoice.to_dict()})
            except json.JSONDecodeError as e:
                results.append({'filename': file.filename, 'success': False, 'error': f'AI 回傳格式錯誤：{str(e)}'})
            except Exception as e:
                results.append({'filename': file.filename, 'success': False, 'error': str(e)})
        else:
            results.append({'filename': file.filename if file.filename else '未知', 'success': False, 'error': '不支援的檔案格式'})

    return jsonify({'success': True, 'results': results})


@app.route('/invoices')
def get_invoices():
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    status = request.args.get('status', '')
    search = request.args.get('search', '')

    query = Invoice.query
    if status:
        query = query.filter(Invoice.status == status)
    if search:
        query = query.filter(
            db.or_(
                Invoice.invoice_number.contains(search),
                Invoice.issuer.contains(search),
                Invoice.item_name.contains(search)
            )
        )
    query = query.order_by(Invoice.created_at.desc())
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'invoices': [inv.to_dict() for inv in pagination.items],
        'total': pagination.total,
        'pages': pagination.pages,
        'current_page': page
    })


@app.route('/invoice/<int:invoice_id>', methods=['GET'])
def get_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    return jsonify(invoice.to_dict())


@app.route('/invoice/<int:invoice_id>', methods=['PUT'])
def update_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    data = request.json
    invoice.invoice_number = data.get('invoice_number', invoice.invoice_number)
    invoice.invoice_date = data.get('invoice_date', invoice.invoice_date)
    invoice.issuer = data.get('issuer', invoice.issuer)
    invoice.item_name = data.get('item_name', invoice.item_name)
    invoice.sales_amount = data.get('sales_amount', invoice.sales_amount)
    invoice.tax_amount = data.get('tax_amount', invoice.tax_amount)
    invoice.total_amount = data.get('total_amount', invoice.total_amount)
    invoice.notes = data.get('notes', invoice.notes)
    invoice.status = data.get('status', invoice.status)
    db.session.commit()
    return jsonify({'success': True, 'invoice': invoice.to_dict()})


@app.route('/invoice/<int:invoice_id>', methods=['DELETE'])
def delete_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    db.session.delete(invoice)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/stats')
def get_stats():
    total = Invoice.query.count()
    pending = Invoice.query.filter_by(status='pending').count()
    confirmed = Invoice.query.filter_by(status='confirmed').count()
    total_amount = db.session.query(db.func.sum(Invoice.total_amount)).filter_by(status='confirmed').scalar() or 0
    return jsonify({
        'total': total,
        'pending': pending,
        'confirmed': confirmed,
        'total_amount': round(total_amount, 2)
    })


@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    from flask import send_from_directory
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
