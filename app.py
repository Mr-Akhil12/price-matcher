"""
Product Price Matcher v2 — Flask + HTMX
"""
import os
import uuid
import pandas as pd
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_file, jsonify, flash
)
from werkzeug.utils import secure_filename

from db_utils import (
    init_database, clear_database,
    get_database_stats, get_matching_stats,
    get_all_products, get_product_cards, get_product_card_detail,
    get_all_reference_prices, get_unmatched_reference_prices,
    update_variant_surcharge, update_variant_ribbon,
    update_variant_match_status, mark_variant_done,
    mark_all_variants_done, bulk_apply_surcharges,
    bulk_apply_all_surcharges, get_export_data,
    search_variants, get_all_collections,
)
from matching_engine import run_full_match
from importers import import_catalog_csv, import_rrp_xlsx, import_rrp_csv

DATA_DIR = os.environ.get('DATA_DIR', '/tmp' if os.environ.get('VERCEL') else '.')

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-' + str(uuid.uuid4()))
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB

ALLOWED_CATALOG = {'csv'}
ALLOWED_RRP = {'csv', 'xlsx'}

os.makedirs(os.path.join(DATA_DIR, 'cache'), exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, 'exports'), exist_ok=True)

# ─── INIT ────────────────────────────────────────────────────────────────────

_db_initialized = False

@app.before_request
def ensure_db():
    global _db_initialized
    if not _db_initialized:
        init_database()
        _db_initialized = True

# ─── HELPERS ─────────────────────────────────────────────────────────────────

def allowed_file(filename, allowed_set):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in allowed_set

def redirect_back(msg=''):
    if msg:
        flash(msg)
    return redirect(request.referrer or url_for('index'))

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Dashboard — stats + recent cards"""
    stats = get_database_stats()
    match_stats = get_matching_stats()
    products_data = get_product_cards(page=1, per_page=12)
    collections = get_all_collections()
    return render_template(
        'index.html',
        stats=stats, match_stats=match_stats,
        products=products_data['products'],
        page=1, pages=products_data['pages'],
        collections=collections,
    )

# ─── IMPORT ─────────────────────────────────────────────────────────────────

@app.route('/import/catalog', methods=['POST'])
def import_catalog():
    file = request.files.get('catalog_file')
    if not file or file.filename == '':
        flash('No file selected', 'error')
        return redirect_back()

    if not allowed_file(file.filename, ALLOWED_CATALOG):
        flash('Only CSV files allowed for catalog', 'error')
        return redirect_back()

    path = os.path.join(DATA_DIR, 'cache', secure_filename(file.filename))
    file.save(path)

    result = import_catalog_csv(path)
    flash(f"Catalogo importado: {result.get('products_imported',0)} products, {result.get('variants_imported',0)} variants", 'success')
    if result.get('errors', 0) > 0:
        flash(f"{result['errors']} erros", 'warning')
    return redirect(url_for('match'))

@app.route('/import/rrp', methods=['POST'])
def import_rrp():
    file = request.files.get('rrp_file')
    if not file or file.filename == '':
        flash('No file selected', 'error')
        return redirect_back()

    if not allowed_file(file.filename, ALLOWED_RRP):
        flash('Only CSV or XLSX files allowed for RRP', 'error')
        return redirect_back()

    path = os.path.join(DATA_DIR, 'cache', secure_filename(file.filename))
    file.save(path)

    if path.endswith('.xlsx'):
        result = import_rrp_xlsx(path)
    else:
        result = import_rrp_csv(path)

    if result.get('error'):
        flash(result['error'], 'error')
        return redirect_back()

    imported = result.get('imported', 0)
    skipped = result.get('skipped', 0)
    flash(f"RRP importado: {imported} precos de referencia. {skipped} ignorados.", 'success')
    return redirect(url_for('match'))

# ─── MATCH ────────────────────────────────────────────────────────────────────

@app.route('/match')
def match():
    match_stats = get_matching_stats()
    unmatched_rrps = get_unmatched_reference_prices()
    collections = get_all_collections()
    return render_template(
        'match.html',
        match_stats=match_stats,
        unmatched_rrps=unmatched_rrps[:200],
        collections=collections,
    )

@app.route('/match/run', methods=['POST'])
def run_match():
    result = run_full_match()
    flash(f"Matching completo: {result['updated']} variantes correspondidas, {result['skipped']} sem match", 'success')
    return redirect(url_for('review'))

# ─── REVIEW (CARD GRID) ───────────────────────────────────────────────────────

@app.route('/review')
def review():
    search = request.args.get('search', '').strip()
    match_filter = request.args.get('match_filter', 'all')
    collection_filter = request.args.get('collection', 'all')
    ribbon_filter = request.args.get('ribbon_filter', 'all')
    page = int(request.args.get('page', 1))

    data = get_product_cards(
        search=search,
        match_filter=match_filter,
        collection_filter=collection_filter,
        ribbon_filter=ribbon_filter,
        page=page,
        per_page=30,
    )
    collections = get_all_collections()
    return render_template(
        'review.html',
        products=data['products'],
        total=data['total'],
        page=data['page'],
        pages=data['pages'],
        collections=collections,
        search=search,
        match_filter=match_filter,
        collection_filter=collection_filter,
        ribbon_filter=ribbon_filter,
    )

@app.route('/product/<int:product_id>')
def product_detail(product_id):
    detail = get_product_card_detail(product_id)
    if not detail:
        flash('Produto nao encontrado', 'error')
        return redirect(url_for('review'))
    return render_template('product_detail.html', **detail)

# ─── AJAX ACTIONS ────────────────────────────────────────────────────────────

@app.route('/action/surcharge', methods=['POST'])
def action_surcharge():
    variant_id = int(request.form.get('variant_id', 0))
    surcharge = float(request.form.get('surcharge', 0))
    ok = update_variant_surcharge(variant_id, surcharge)
    return jsonify({'ok': ok})

@app.route('/action/ribbon', methods=['POST'])
def action_ribbon():
    variant_id = int(request.form.get('variant_id', 0))
    ribbon = request.form.get('ribbon_status', 'active')
    ok = update_variant_ribbon(variant_id, ribbon)
    return jsonify({'ok': ok})

@app.route('/action/match', methods=['POST'])
def action_match():
    variant_id = int(request.form.get('variant_id', 0))
    rrp_id = int(request.form.get('rrp_id', 0))
    status = request.form.get('status', 'user_approved')
    trust = float(request.form.get('trust_score', 1.0))
    ok = update_variant_match_status(variant_id, status, rrp_id, trust)
    return jsonify({'ok': ok})

@app.route('/action/done', methods=['POST'])
def action_done():
    variant_id = int(request.form.get('variant_id', 0))
    done = request.form.get('done', 'false') == 'true'
    mark_variant_done(variant_id, done)
    return jsonify({'ok': True})

@app.route('/action/bulk-apply/<int:product_id>', methods=['POST'])
def action_bulk_apply(product_id):
    result = bulk_apply_surcharges(product_id)
    return jsonify(result)

@app.route('/action/bulk-apply-all', methods=['POST'])
def action_bulk_apply_all():
    result = bulk_apply_all_surcharges()
    flash(f"Aplicado a {result['updated']} variantes", 'success')
    return redirect(url_for('review'))

@app.route('/action/mark-all-done/<int:product_id>', methods=['POST'])
def action_mark_all_done(product_id):
    mark_all_variants_done(product_id, True)
    return jsonify({'ok': True})

# ─── EXPORT ──────────────────────────────────────────────────────────────────

@app.route('/export')
def export_page():
    stats = get_database_stats()
    match_stats = get_matching_stats()
    preview_rows = get_export_data()[:5]
    return render_template('export.html', stats=stats, match_stats=match_stats, preview_rows=preview_rows)

@app.route('/export/download')
def export_download():
    """Export catalog as CSV with original format preserved"""
    rows = get_export_data()
    if not rows:
        flash('Nenhum dado para exportar', 'warning')
        return redirect(url_for('export_page'))

    # Load original catalog to preserve format if available
    original_path = os.path.join(DATA_DIR, 'cache', 'original_catalog.csv')
    if os.path.exists(original_path):
        df_orig = pd.read_csv(original_path)
        df_orig.columns = [str(c).strip() for c in df_orig.columns]
    else:
        # Build from scratch with Wix schema
        df_orig = None

    export_rows = []
    for row in rows:
        if row.get('sku'):  # Variant row
            export_row = {
                'handleId': row['handle_id'],
                'fieldType': 'Variant',
                'name': row['name'],
                'description': row.get('description', ''),
                'productImageUrl': '',
                'collection': row.get('collection', ''),
                'sku': row['sku'],
                'ribbon': row.get('ribbon_status', 'active'),
                'price': row['base_price'],
                'surcharge': row.get('surcharge', 0),
                'visible': row.get('variant_visible', 'true'),
                'discountMode': '',
                'discountValue': '',
                'inventory': '',
                'weight': '',
                'cost': '',
                'productOptionName1': row.get('option_1_name', ''),
                'productOptionType1': 'DROP_DOWN',
                'productOptionDescription1': row.get('option_1_value', ''),
                'productOptionName2': row.get('option_2_name', ''),
                'productOptionType2': 'DROP_DOWN',
                'productOptionDescription2': row.get('option_2_value', ''),
                'productOptionName3': row.get('option_3_name', ''),
                'productOptionType3': 'DROP_DOWN',
                'productOptionDescription3': row.get('option_3_value', ''),
                'productOptionName4': '',
                'productOptionType4': 'DROP_DOWN',
                'productOptionDescription4': '',
                'productOptionName5': '',
                'productOptionType5': 'DROP_DOWN',
                'productOptionDescription5': '',
                'productOptionName6': '',
                'productOptionType6': 'DROP_DOWN',
                'productOptionDescription6': '',
                'additionalInfoTitle1': '',
                'additionalInfoDescription1': '',
                'additionalInfoTitle2': '',
                'additionalInfoDescription2': '',
                'additionalInfoTitle3': '',
                'additionalInfoDescription3': '',
                'additionalInfoTitle4': '',
                'additionalInfoDescription4': '',
                'additionalInfoTitle5': '',
                'additionalInfoDescription5': '',
                'additionalInfoTitle6': '',
                'additionalInfoDescription6': '',
                'customTextField1': '',
                'customTextCharLimit1': '',
                'customTextMandatory1': '',
                'customTextField2': '',
                'customTextCharLimit2': '',
                'customTextMandatory2': '',
                'brand': row.get('brand', ''),
            }
        else:  # Product row
            export_row = {
                'handleId': row['handle_id'],
                'fieldType': 'Product',
                'name': row['name'],
                'description': row.get('description', ''),
                'productImageUrl': '',
                'collection': row.get('collection', ''),
                'sku': '',
                'ribbon': '',
                'price': row['base_price'],
                'surcharge': '',
                'visible': row.get('product_visible', 'true'),
                'discountMode': '',
                'discountValue': '',
                'inventory': '',
                'weight': '',
                'cost': '',
                'productOptionName1': '', 'productOptionType1': 'DROP_DOWN', 'productOptionDescription1': '',
                'productOptionName2': '', 'productOptionType2': 'DROP_DOWN', 'productOptionDescription2': '',
                'productOptionName3': '', 'productOptionType3': 'DROP_DOWN', 'productOptionDescription3': '',
                'productOptionName4': '', 'productOptionType4': 'DROP_DOWN', 'productOptionDescription4': '',
                'productOptionName5': '', 'productOptionType5': 'DROP_DOWN', 'productOptionDescription5': '',
                'productOptionName6': '', 'productOptionType6': 'DROP_DOWN', 'productOptionDescription6': '',
                'additionalInfoTitle1': '', 'additionalInfoDescription1': '',
                'additionalInfoTitle2': '', 'additionalInfoDescription2': '',
                'additionalInfoTitle3': '', 'additionalInfoDescription3': '',
                'additionalInfoTitle4': '', 'additionalInfoDescription4': '',
                'additionalInfoTitle5': '', 'additionalInfoDescription5': '',
                'additionalInfoTitle6': '', 'additionalInfoDescription6': '',
                'customTextField1': '', 'customTextCharLimit1': '', 'customTextMandatory1': '',
                'customTextField2': '', 'customTextCharLimit2': '', 'customTextMandatory2': '',
                'brand': row.get('brand', ''),
            }
        export_rows.append(export_row)

    df = pd.DataFrame(export_rows)

    # Ensure column order matches Wix schema
    wix_cols = [
        'handleId','fieldType','name','description','productImageUrl','collection',
        'sku','ribbon','price','surcharge','visible','discountMode','discountValue',
        'inventory','weight','cost',
        'productOptionName1','productOptionType1','productOptionDescription1',
        'productOptionName2','productOptionType2','productOptionDescription2',
        'productOptionName3','productOptionType3','productOptionDescription3',
        'productOptionName4','productOptionType4','productOptionDescription4',
        'productOptionName5','productOptionType5','productOptionDescription5',
        'productOptionName6','productOptionType6','productOptionDescription6',
        'additionalInfoTitle1','additionalInfoDescription1',
        'additionalInfoTitle2','additionalInfoDescription2',
        'additionalInfoTitle3','additionalInfoDescription3',
        'additionalInfoTitle4','additionalInfoDescription4',
        'additionalInfoTitle5','additionalInfoDescription5',
        'additionalInfoTitle6','additionalInfoDescription6',
        'customTextField1','customTextCharLimit1','customTextMandatory1',
        'customTextField2','customTextCharLimit2','customTextMandatory2',
        'brand',
    ]
    for col in wix_cols:
        if col not in df.columns:
            df[col] = ''
    df = df[wix_cols]

    path = os.path.join(DATA_DIR, 'exports', 'catalog_export.csv')
    df.to_csv(path, index=False, encoding='utf-8')
    return send_file(path, as_attachment=True, download_name='catalog_export.csv')

# ─── DATABASE CONTROLS ───────────────────────────────────────────────────────

@app.route('/admin/clear', methods=['POST'])
def admin_clear():
    clear_database()
    flash('Base de dados limpa', 'success')
    return redirect(url_for('index'))

# ─── PRICE SCRAPER INTEGRATION ─────────────────────────────────────────────

@app.route('/scrape/<int:variant_id>', methods=['POST'])
def scrape_variant(variant_id):
    from price_scraper_service import scrape_variant_prices
    results = scrape_variant_prices(variant_id)
    return jsonify(results)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host='0.0.0.0', port=port, debug=debug)
