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
            'quantity': round(qty, 4), 'unit': u,
            'unitPrice': round(unit_price, 5),
            'total': round(line_total, 2), 'kdvRate': kdv_rate
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

    # Strateji B - Otojet (bitişik TRY)
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
    flat = re.sub(r'[\t\n\r\xa0]', ' ', text)
    flat = re.sub(r' {2,}', ' ', flat)

    def find(t, *patterns):
        for p in patterns:
            m = re.search(p, t)
            if m and m.group(1): return m.group(1).strip()
        return None

    def find_num(t, *patterns):
        s = find(t, *patterns)
        return to_num(s) if s else 0

    # ── Fatura No ──────────────────────────────────────────────────────────────
    # "Fatura No : EARSIVFATURA : SARJANLIK : CTA2026..." → 3. değer
    # Önce spesifik prefix'li no'ları dene (CTA, OTE, KE3, ZES, TAA vb.)
    invoice_no = find(flat,
        r'Fatura\s+No\s*[:\s]+(?:EARSIVFATURA|EFATURA)\s*[:\s]+(?:\S+)\s*[:\s]+([A-Za-z0-9\-\/]{4,30})',
        r'Fatura\s+No\s*[:\s]+([A-Z]{2,5}\d{6,})',  # CTA2026..., OTE2026...
        r'([A-Z]{2,5}\d{4}\d{6,})',                   # direkt format
    )
    # Hiç bulunamazsa genel pattern
    if not invoice_no:
        invoice_no = find(flat,
            r'Fatura\s+No\s*[:\s]+([A-Za-z0-9\-\/]{4,30})',
        )
    # EARSIVFATURA veya SARJANLIK gelirse reddet
    if invoice_no and re.match(r'^(EARSIVFATURA|SARJANLIK|EFATURA|TR\d\.\d)$', invoice_no, re.I):
        invoice_no = None

    # ── Tarih ──────────────────────────────────────────────────────────────────
    raw_date = find(flat,
        r'Fatura\s+Tarihi\s*[:\s]+([\d]{1,2}[\/\.\-][\d]{1,2}[\/\.\-][\d]{2,4})',
        r'([\d]{2}[\.\/][\d]{2}[\.\/][\d]{4})'
    )

    # ── Tedarikçi ──────────────────────────────────────────────────────────────
    # pdfminer ham metinde ilk anlamlı satır tedarikçi
    skip_rx = re.compile(r'^(fatura|invoice|no|tarih|date|sayfa|page|e-?fatura|tel|vergi|vkn|tckn|mersis|epdk|\d{1,4}|tr\d)$', re.I)
    supplier = None
    # Önce satır satır dene (CTG, Dicle gibi)
    for line in text.split('\n'):
        cl = line.strip()
        if len(cl) >= 4 and not skip_rx.match(cl) and not re.match(r'^\d+$', cl) and not re.match(r'^[:\-\.\s]+$', cl):
            # Satır meta bilgi değil mi kontrol et
            if not re.match(r'^(Özelleştirme|Senaryo|ETTN|Sicil|İşlem)', cl):
                supplier = cl[:70]
                break

    # Satırdan bulunamazsa (Otojet gibi tek uzun satır) — firma adı pattern ile bul
    if not supplier:
        norm = re.sub(r'\xa0', ' ', text)
        # ETTN/UUID ve hex kodlarını temizle ki "0EC4FOTOJET" → "OTOJET" yakalansın
        norm_clean = re.sub(r'[0-9A-Fa-f]{8}-[0-9A-Fa-f-]{27}', ' ', norm)
        norm_clean = re.sub(r'[0-9A-Fa-f]{4,}(?=[A-ZÇĞİÖŞÜ])', ' ', norm_clean)
        m_sup = re.search(r'(?<![A-Za-z])([A-ZÇĞİÖŞÜ]{2,}(?:\s+[A-ZÇĞİÖŞÜa-zçğışöşü\.]+){1,6}\s*(?:SİRKETİ|SİRKETI|SIRKETI|A\.Ş|A\.S|Ltd|LTD))', norm_clean)
        if m_sup:
            s = re.sub(r'\s+', ' ', m_sup.group(1)).strip()
            s = re.sub(r'([A-Z])ANONİM', r'\1 ANONİM', s)
            s = re.sub(r'([A-Z])ANONIM', r'\1 ANONIM', s)
            s = re.sub(r'([A-Z])SİRKETİ', r'\1 SİRKETİ', s)
            s = re.sub(r'([A-Z])SIRKETI', r'\1 SİRKETİ', s)
            supplier = re.sub(r'\s+', ' ', s).strip()[:80]

    if not supplier:
        supplier = find(flat,
            r'([A-ZÇĞİÖŞÜa-zçğışöşü][^\d\n]{3,60}(?:A\.Ş|Ltd|A\.S|LTD|ELEKTRİK|ENERJİ|ŞARJ)[^\n]*)',
        )

    # ── Vergi No ───────────────────────────────────────────────────────────────
    supplier_tax_no = find(flat,
        r'V\.K\.N\.\s*[:\s]+([\d]{10,11})',
        r'VKN\s*[:\s]+([\d]{10,11})',
        r'Vergi\s+Numarası\s*[:\s]+([\d]{10,11})',
        r'Vergi\s+Kimlik\s+No\s*[:\s]+([\d]{10,11})',
    )

    # ── Tutarlar ───────────────────────────────────────────────────────────────
    total = find_num(flat,
        r'[Öö]denecek\s+[Tt]utar\s*[:\s]+([\d\.,]+)',
        r'GENEL\s+TOPLAM\s*[:\s]+([\d\.,]+)',
        r'[Gg]enel\s+[Tt]oplam\s*[:\s]+([\d\.,]+)'
    )
    kdv = find_num(flat,
        r'Hesaplanan\s+KDV\s*\([^)]+\)\s*[:\s]+([\d\.,]+)',
        r'KDV\s+[Tt]utar[ıi]\s*[:\s]+([\d\.,]+)',
        r'Katma\s+Değer\s+Vergisi\s*%\d+\s*[:\s]+([\d\.,]+)',
    )
    subtotal = find_num(flat,
        r'[Mm]atrah\s*[:\s]+([\d\.,]+)',
        r'[Vv]ergi\s+[Mm]atrahı\s*[:\s]+([\d\.,]+)',
        r'KDV\s+Matrahı\s*[:\s]+([\d\.,]+)'
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
            try:
                pdf_bytes = download_telegram_file(data['file_path'])
                file_name = data.get('file_name', 'fatura.pdf')
            except Exception as e:
                return jsonify({'error': f'Telegram indirme hatası: {str(e)}'}), 400
        elif 'pdf_url' in data:
            url = data['pdf_url']
            file_name = data.get('file_name', 'fatura.pdf')
            fp_match = re.search(r'/file/bot[^/]+/(.+)', url)
            if fp_match:
                try:
                    pdf_bytes = download_telegram_file(fp_match.group(1))
                except Exception as e:
                    return jsonify({'error': f'Telegram indirme hatası: {str(e)}'}), 400
            else:
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

    if pdf_bytes[:4] != b'%PDF':
        return jsonify({'error': f'Geçersiz PDF. İlk bytes: {pdf_bytes[:20]}'}), 400

    try:
        text = pdfminer.extract_text(io.BytesIO(pdf_bytes))
    except Exception as e:
        return jsonify({'error': f'PDF okunamadı: {str(e)}'}), 400

    if not text or len(text.strip()) < 20:
        return jsonify({'error': 'PDF metni çıkarılamadı'}), 400

    inv = parse_invoice(text)
    inv['fileName'] = file_name

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
