import os, re, json, base64
from flask import Flask, request, jsonify
from flask_cors import CORS
import pdfminer.high_level as pdfminer
import io

app = Flask(__name__)
CORS(app)

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://kdyidnkuvxpwmwenobio.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'sb_publishable_6RIEGiGcOLcTl5XXT5C0Lg_rEEIG7l6')

# ── Yardımcılar ───────────────────────────────────────────────────────────────
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

    # Normalize: tab + newline + xa0 → boşluk
    flat = re.sub(r'[\t\n\r\xa0]', ' ', text)
    flat = re.sub(r' {2,}', ' ', flat)

    # Strateji A: miktar BİRİM fiyat TL|TRY → nearest match
    rx_a = re.compile(rf'(\d+[,.]\d+)\s+({UNITS_RX})\s+(\d+[,.]\d+)\s+(?:TL|TRY)\b', re.I)
    for m in rx_a.finditer(flat):
        qty = to_num(m.group(1))
        unit = m.group(2)
        up = to_num(m.group(3))
        if qty <= 0 or up <= 0: continue
        expected = qty * up
        after = flat[m.end():m.end()+250]
        best, best_ratio = 0, 0.12
        for tm in re.finditer(r'(\d[\d.]*,\d{2})\s*(?:TL|TRY)\b', after, re.I):
            v = to_num(tm.group(1))
            if v <= 0: continue
            ratio = abs(v - expected) / expected
            if ratio < best_ratio:
                best = v; best_ratio = ratio
        if not best: continue
        before = flat[max(0, m.start()-120):m.start()]
        kdv_m = re.search(r'%(\d+)', after[:60])
        desc = re.sub(r'\b\d{2}[\/\.]\d{2}[\/\.]\d{2,4}\b', '', before)
        desc = re.sub(r'\b\d{2}:\d{2}:\d{2}\b', '', desc)
        desc = re.sub(r'\b[A-Z0-9]{5,}(?:[.\-][A-Z0-9]+)+\b', '', desc)
        desc = re.sub(r'\s{2,}', ' ', desc).strip()
        add_item(qty, unit, up, best, desc, int(kdv_m.group(1)) if kdv_m else 20)

    # Strateji B: Otojet — bitişik TRY
    if not items:
        rx_b = re.compile(rf'(\d+[,.]\d+)\s*({UNITS_RX})\s*(\d+[,.]\d+)\s*(?:TRY|TL)(\d+[,.]\d+)\s*(?:TRY|TL)', re.I)
        for m in rx_b.finditer(flat):
            qty, up, lt = to_num(m.group(1)), to_num(m.group(3)), to_num(m.group(4))
            if abs(qty*up - lt)/(lt or 1) > 0.12: continue
            before = flat[max(0,m.start()-100):m.start()]
            add_item(qty, m.group(2), up, lt, before)

    # Strateji C: Zeplin — fiyat+TRY bitişik
    if not items:
        rx_c = re.compile(rf'(\d+[,.]\d+)\s*({UNITS_RX})\s*(\d+[,.]\d+)(?:TRY|TL)(?!\d)', re.I)
        for m in rx_c.finditer(flat):
            qty, up = to_num(m.group(1)), to_num(m.group(3))
            after = flat[m.end():m.end()+120]
            all_try = re.findall(r'(\d[\d.]*,\d{2})(?:TRY|TL)', after, re.I)
            lt = to_num(all_try[-1]) if all_try else qty*up
            before = flat[max(0,m.start()-80):m.start()]
            add_item(qty, m.group(2), up, lt, before)

    # Strateji D: Blok bazlı (\n sütunlu)
    if not items:
        block_rx = re.compile(
            r'(?:^|\n)\s*(\d{1,3})\s*\n([\s\S]+?)(?=\n\s*\d{1,3}\s*\n|Malzeme|Mal\s+Hizmet\s+Toplam|Hesaplanan\s+KDV|Toplam\s+İskonto|Ödenecek|Vergiler\s+(?:Hariç|Dahil))',
            re.I
        )
        m_rx = re.compile(rf'(\d+[,.]\d+)\s*({UNITS_RX})', re.I)
        tl_rx = re.compile(r'(\d[\d.]*,\d{2})\s*TL')
        for bm in block_rx.finditer(text):
            block = bm.group(2)
            mm = m_rx.search(block)
            if not mm: continue
            tls = [to_num(m.group(1)) for m in tl_rx.finditer(block) if to_num(m.group(1)) > 0]
            if len(tls) < 2: continue
            desc = block[:mm.start()].replace('\n',' ').replace('\t',' ')
            kdv_m = re.search(r'%(\d+)', block)
            add_item(to_num(mm.group(1)), mm.group(2), tls[0], tls[-1], desc,
                     int(kdv_m.group(1)) if kdv_m else 20)

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
        r'[Ff]atura\s*[Nn]o\s*[:\s]*([A-Za-z0-9\-\/]{3,30})',
        r'FATURA\s*NO\s*[:\s]*([A-Za-z0-9\-\/]+)'
    )
    raw_date = find(
        r'[Ff]atura\s+[Tt]arihi\s*[:\s]*([\d]{1,2}[\/\.\-][\d]{1,2}[\/\.\-][\d]{2,4})',
        r'([\d]{2}[\.\/][\d]{2}[\.\/][\d]{4})'
    )
    supplier = find(
        r'(?:Satıcı|SATICI|Firma)\s+[Üü]nvan[ıi]\s*[:\s]*(.{4,70}?)(?:\n|Vergi|VKN)',
    )
    supplier_tax_no = find(
        r'[Vv](?:ergi\s+[Kk]imlik\s+[Nn]o|KN)\s*[:\s]*([\d]{10,11})',
        r'VKN\s*[:\s]*([\d]{10,11})'
    )
    total = find_num(
        r'[Öö]denecek\s+[Tt]utar\s*[:\s]*([\d\.,]+)',
        r'GENEL\s+TOPLAM\s*[:\s]*([\d\.,]+)',
        r'[Gg]enel\s+[Tt]oplam\s*[:\s]*([\d\.,]+)'
    )
    kdv = find_num(
        r'Hesaplanan\s+KDV\s*\([^)]+\)\s*[:\s]*([\d\.,]+)',
        r'KDV\s+[Tt]utar[ıi]\s*[:\s]*([\d\.,]+)'
    )
    subtotal = find_num(
        r'[Mm]atrah\s*[:\s]*([\d\.,]+)',
        r'[Vv]ergi\s+[Mm]atrahı\s*[:\s]*([\d\.,]+)',
        r'KDV\s+Matrahı\s*[:\s]*([\d\.,]+)'
    ) or round(total / 1.2, 2)

    items = parse_items(text)
    # items yoksa TRY para birimini dene
    currency = 'TRY'
    if 'TRY' in text and 'TL' not in text: currency = 'TRY'

    return {
        'invoiceNo': invoice_no or '—',
        'supplier': supplier or 'Belirlenemedi',
        'supplierTaxNo': supplier_tax_no or '',
        'buyerName': '',
        'date': normalize_date(raw_date),
        'currency': currency,
        'subtotal': subtotal,
        'kdv': kdv,
        'total': total,
        'items': items
    }

# ── Routes ────────────────────────────────────────────────────────────────────
@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'volttrack-parser'})

@app.route('/parse', methods=['POST'])
def parse():
    """
    POST /parse
    Body: { "pdf_base64": "...", "file_name": "fatura.pdf" }
    veya multipart form-data ile "file" alanı
    """
    import requests

    file_name = 'fatura.pdf'
    pdf_bytes = None

    # JSON body (base64)
    if request.is_json:
        data = request.get_json()
        if 'pdf_base64' in data:
            pdf_bytes = base64.b64decode(data['pdf_base64'])
            file_name = data.get('file_name', 'fatura.pdf')
        elif 'pdf_url' in data:
            # Telegram file URL
            r = requests.get(data['pdf_url'], timeout=30)
            pdf_bytes = r.content
            file_name = data.get('file_name', 'fatura.pdf')
    elif 'file' in request.files:
        f = request.files['file']
        pdf_bytes = f.read()
        file_name = f.filename

    if not pdf_bytes:
        return jsonify({'error': 'PDF bulunamadı'}), 400

    # PDF → metin
    try:
        text = pdfminer.extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        return jsonify({'error': f'PDF okunamadı: {str(e)}'}), 400

    if not text or len(text.strip()) < 20:
        return jsonify({'error': 'PDF metni çıkarılamadı'}), 400

    # Parse et
    inv = parse_invoice(text)
    inv['fileName'] = file_name
    inv['notes'] = ''
    inv['rawText'] = ''  # ham metin saklamıyoruz

    # Supabase'e kaydet
    import uuid
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
