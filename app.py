import os, re, json, base64, uuid, io
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfminer.high_level as pdfminer
import requests

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://kdyidnkuvxpwmwenobio.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_6RIEGiGcOLcTl5XXT5C0Lg_rEEIG7l6')
BOT_TOKEN    = os.environ.get('BOT_TOKEN', '8527446489:AAGvmThn6RloE2TgcCNF0JtuXNvsnR5wETA')

def to_num(s):
    if not s: return 0.0
    try: return float(str(s).strip().replace('.','').replace(',','.'))
    except: return 0.0

def normalize_date(raw):
    if not raw: return None
    m = re.search(r'(\d{1,2})[\/\.\-](\d{1,2})[\/\.\-](\d{2,4})', raw)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2: y = '20' + y
        return f"{y}-{mo.zfill(2)}-{d.zfill(2)}"
    return None

UNITS_RX = r'(?:KWH|kWh|ADET|Adet|SAAT|GÜN|KG|MT|LT)'

def parse_items(text):
    items = []

    def add_item(qty, unit, unit_price, line_total, desc, kdv_rate=20):
        u = 'kWh' if unit.upper() == 'KWH' else unit
        if qty <= 0 or unit_price <= 0 or line_total <= 0: return
        if any(abs(i['total'] - line_total) < 0.01 and abs(i['quantity'] - qty) < 0.01 for i in items): return
        items.append({
            'description': desc.strip() or 'Şarj Hizmet Bedeli',
            'quantity': round(qty, 4),
            'unit': u,
            'unitPrice': round(unit_price, 5),
            'total': round(line_total, 2),
            'kdvRate': kdv_rate
        })

    flat = re.sub(r'[\t\n\r\xa0]', ' ', text)
    flat = re.sub(r' {2,}', ' ', flat)

    # Strateji A
    rx_a = re.compile(rf'(\d+[,.]\d+)\s+({UNITS_RX})\s+(\d+[,.]\d+)\s+(?:TL|TRY)\b', re.I)
    for m in rx_a.finditer(flat):
        qty = to_num(m.group(1)); unit = m.group(2); up = to_num(m.group(3))
        if qty <= 0 or up <= 0: continue
        expected = qty * up
        after = flat[m.end():m.end()+250]
        best, best_ratio = 0, 0.12
        for tm in re.finditer(r'(\d[\d.]*,\d{2})\s*(?:TL|TRY)\b', after, re.I):
            v = to_num(tm.group(1))
            if v <= 0: continue
            ratio = abs(v - expected) / expected
            if ratio < best_ratio: best = v; best_ratio = ratio
        if not best: continue
        before = flat[max(0, m.start()-120):m.start()]
        kdv_m = re.search(r'%(\d+)', after[:60])
        desc = re.sub(r'\b\d{2}[\/\.]\d{2}[\/\.]\d{2,4}\b', '', before)
        desc = re.sub(r'\b\d{2}:\d{2}:\d{2}\b', '', desc)
        desc = re.sub(r'\s{2,}', ' ', desc).strip()
        add_item(qty, unit, up, best, desc, int(kdv_m.group(1)) if kdv_m else 20)

    # Strateji B - Otojet
    if not items:
        rx_b = re.compile(rf'(\d+[,.]\d+)\s*({UNITS_RX})\s*(\d+[,.]\d+)\s*(?:TRY|TL)(\d+[,.]\d+)\s*(?:TRY|TL)', re.I)
        for m in rx_b.finditer(flat):
            qty, up, lt = to_num(m.group(1)), to_num(m.group(3)), to_num(m.group(4))
            if abs(qty*up - lt)/(lt or 1) > 0.12: continue
            before = flat[max(0,m.start()-100):m.start()]
            add_item(qty, m.group(2), up, lt, before)

    # Strateji C - Zeplin
    if not items:
        rx_c = re.compile(rf'(\d+[,.]\d+)\s*({UNITS_RX})\s*(\d+[,.]\d+)(?:TRY|TL)(?!\d)', re.I)
        for m in rx_c.finditer(flat):
            qty, up = to_num(m.group(1)), to_num(m.group(3))
            after = flat[m.end():m.end()+120]
            all_try = re.findall(r'(\d[\d.]*,\d{2})(?:TRY|TL)', after, re.I)
            lt = to_num(all_try[-1]) if all_try else qty*up
            before = flat[max(0,m.start()-80):m.start()]
            add_item(qty, m.group(2), up, lt, before)

    return items

def parse_invoice(text):
    def find(*patterns):
        for p in patterns:
            m = re.search(p, text)
            if m and m.group(1): return m.group(1).strip()
        return None

    def find_num(*patterns):
        s = find(*patterns)
        return to_num(s) if s else 0

    invoice_no = find(
        r'[Ff]atura\s*[Nn]o\s*[:\s\t]*([A-Za-z0-9\-\/]{3,30})',
        r'FATURA\s*NO\s*[:\s\t]*([A-Za-z0-9\-\/]+)'
    )
    raw_date = find(
        r'[Ff]atura\s+[Tt]arihi\s*[:\s\t]*([\d]{1,2}[\/\.\-][\d]{1,2}[\/\.\-][\d]{2,4})',
        r'([\d]{2}[\.\/][\d]{2}[\.\/][\d]{4})'
    )
    supplier = find(
        r'(?:Satıcı|SATICI|Firma)\s+[Üü]nvan[ıi]\s*[:\s]*(.{4,70}?)(?:\n|Vergi|VKN)',
    )
    supplier_tax_no = find(
        r'[Vv](?:ergi\s+[Kk]imlik\s+[Nn]o|KN)\s*[:\s\t]*([\d]{10,11})',
        r'VKN\s*[:\s\t]*([\d]{10,11})'
    )
    total = find_num(
        r'[Öö]denecek\s+[Tt]utar\s*[:\s\t]*([\d\.,]+)',
        r'GENEL\s+TOPLAM\s*[:\s\t]*([\d\.,]+)',
        r'[Gg]enel\s+[Tt]oplam\s*[:\s\t]*([\d\.,]+)'
    )
    kdv = find_num(
        r'Hesaplanan\s+KDV\s*\([^)]+\)\s*[:\s\t]*([\d\.,]+)',
        r'KDV\s+[Tt]utar[ıi]\s*[:\s\t]*([\d\.,]+)'
    )
    subtotal = find_num(
        r'[Mm]atrah\s*[:\s\t]*([\d\.,]+)',
        r'[Vv]ergi\s+[Mm]atrahı\s*[:\s\t]*([\d\.,]+)',
        r'KDV\s+Matrahı\s*[:\s\t]*([\d\.,]+)'
    ) or round(total / 1.2, 2)

    items = parse_items(text)

    return {
        'invoiceNo': invoice_no or '—',
        'supplier': supplier or 'Belirlenemedi',
        'supplierTaxNo': supplier_tax_no or '',
        'buyerName': '',
        'date': normalize_date(raw_date),
        'currency': 'TRY',
        'subtotal': subtotal,
        'kdv': kdv,
        'total': total,
        'items': items
    }

def download_telegram_file(file_path):
    """Telegram'dan bot token ile dosya indir"""
    url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.content

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'volttrack-parser'})

@app.route('/parse', methods=['POST'])
def parse():
    file_name = 'fatura.pdf'
    pdf_bytes = None

    if request.is_json:
        data = request.get_json()

        if 'pdf_base64' in data:
            pdf_bytes = base64.b64decode(data['pdf_base64'])
            file_name = data.get('file_name', 'fatura.pdf')

        elif 'file_path' in data:
            # Telegram file_path direkt geldi
            try:
                pdf_bytes = download_telegram_file(data['file_path'])
                file_name = data.get('file_name', 'fatura.pdf')
            except Exception as e:
                return jsonify({'error': f'Telegram indirme hatası: {str(e)}'}), 400

        elif 'pdf_url' in data:
            # URL'den file_path çıkar ve bot token ile indir
            url = data['pdf_url']
            file_name = data.get('file_name', 'fatura.pdf')
            # file_path'i URL'den çıkar: /file/botTOKEN/file_path
            fp_match = re.search(r'/file/bot[^/]+/(.+)', url)
            if fp_match:
                file_path = fp_match.group(1)
                try:
                    pdf_bytes = download_telegram_file(file_path)
                except Exception as e:
                    return jsonify({'error': f'Telegram indirme hatası: {str(e)}'}), 400
            else:
                # Direkt URL dene
                try:
                    r = requests.get(url, timeout=30)
                    pdf_bytes = r.content
                except Exception as e:
                    return jsonify({'error': f'URL indirme hatası: {str(e)}'}), 400

    elif 'file' in request.files:
        f = request.files['file']
        pdf_bytes = f.read()
        file_name = f.filename

    if not pdf_bytes:
        return jsonify({'error': 'PDF bulunamadı'}), 400

    # PDF kontrolü
    if pdf_bytes[:4] != b'%PDF':
        return jsonify({'error': f'Geçersiz PDF formatı. İlk 4 byte: {pdf_bytes[:4]}'}), 400

    # PDF → metin
    try:
        text = pdfminer.extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        return jsonify({'error': f'PDF okunamadı: {str(e)}'}), 400

    if not text or len(text.strip()) < 20:
        return jsonify({'error': 'PDF metni çıkarılamadı'}), 400

    inv = parse_invoice(text)
    inv['fileName'] = file_name
    inv['notes'] = ''

    inv_id = str(uuid.uuid4())
    row = {
        'id': inv_id,
        'invoice_no': inv['invoiceNo'],
        'supplier': inv['supplier'],
        'supplier_tax_no': inv['supplierTaxNo'],
        'buyer_name': inv['buyerName'],
        'date': inv['date'],
        'currency': inv['currency'],
        'subtotal': inv['subtotal'],
        'kdv': inv['kdv'],
        'total': inv['total'],
        'file_name': inv['fileName'],
        'notes': '',
        'raw_text': '',
        'items': inv['items']
    }

    headers = {
        'apikey': SUPABASE_KEY,
        'Authorization': f'Bearer {SUPABASE_KEY}',
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal'
    }
    sb_resp = requests.post(
        f'{SUPABASE_URL}/rest/v1/invoices',
        json=row, headers=headers, timeout=10
    )
    if sb_resp.status_code not in (200, 201):
        return jsonify({'error': f'Supabase hatası: {sb_resp.text}'}), 500

    return jsonify({
        'success': True,
        'id': inv_id,
        'invoiceNo': inv['invoiceNo'],
        'supplier': inv['supplier'],
        'total': inv['total'],
        'items_count': len(inv['items'])
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
