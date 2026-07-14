"""
Import handlers for catalog CSV and RRP XLSX files
"""
import pandas as pd
import re
import os
from typing import Dict, List, Tuple, Any
from db_utils import get_db_connection

# ─────────────────────────────────────────────────────────────────────────────
# WIX CATALOG CSV IMPORTER
# ─────────────────────────────────────────────────────────────────────────────

def import_catalog_csv(filepath: str) -> Dict[str, Any]:
    """
    Import Wix catalog CSV. Returns dict with counts.
    Handles both comma and semicolon separators.
    Also saves the original file as DATA_DIR/cache/original_catalog.csv
    for export format preservation.
    """
    # Save original for export template preservation
    data_dir = os.environ.get('DATA_DIR', '.')
    cache_dir = os.path.join(data_dir, 'cache')
    os.makedirs(cache_dir, exist_ok=True)
    dest = os.path.join(cache_dir, 'original_catalog.csv')
    import shutil
    shutil.copy2(filepath, dest)

    # Detect separator
    try:
        df = pd.read_csv(filepath, encoding='utf-8')
        if len(df.columns) < 5:
            df = pd.read_csv(filepath, encoding='utf-8', sep=';')
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(filepath, encoding='latin1')
            if len(df.columns) < 5:
                df = pd.read_csv(filepath, encoding='latin1', sep=';')
        except Exception:
            return {'error': 'Could not parse CSV file'}

    if 'fieldType' not in df.columns and len(df.columns) < 5:
        return {'error': 'Unrecognised CSV format'}

    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]

    products_imported = 0
    variants_imported = 0
    errors = 0

    with get_db_connection() as conn:
        cursor = conn.cursor()

        current_product_id = None
        current_handle_id = None

        # Separate product rows and variant rows
        product_rows = df[df.get('fieldType', pd.Series(['Product']*len(df))) == 'Product']
        variant_rows = df[df.get('fieldType', pd.Series(['Variant']*len(df))) == 'Variant']

        # Build a map of handle_id -> product row for option name lookups
        option_names = {}
        for _, row in product_rows.iterrows():
            handle = str(row.get('handleId', '')).strip()
            names = {}
            for i in range(1, 7):
                nm = row.get(f'productOptionName{i}', '')
                if pd.notna(nm) and str(nm).strip():
                    names[i] = str(nm).strip()
            option_names[handle] = names

        # Import products
        for _, row in product_rows.iterrows():
            try:
                handle_id = str(row.get('handleId', '')).strip()
                if not handle_id:
                    continue

                # Upsert product
                cursor.execute("SELECT id FROM products WHERE handle_id = ?", (handle_id,))
                existing = cursor.fetchone()

                image_url = str(row.get('productImageUrl', '')) if pd.notna(row.get('productImageUrl')) else ''
                base_price = 0.0
                if pd.notna(row.get('price')):
                    try:
                        base_price = float(str(row['price']).replace(',', ''))
                    except (ValueError, TypeError):
                        pass

                if existing:
                    cursor.execute("""
                        UPDATE products SET
                            name = ?, description = ?, base_price = ?,
                            collection = ?, brand = ?, visible = ?,
                            product_image_url = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE handle_id = ?
                    """, (
                        str(row.get('name', '')), str(row.get('description', '')),
                        base_price, str(row.get('collection', '') or ''),
                        str(row.get('brand', '') or ''),
                        str(row.get('visible', 'true')),
                        image_url, handle_id
                    ))
                    current_product_id = existing['id']
                else:
                    cursor.execute("""
                        INSERT INTO products (handle_id, name, description, base_price, collection, brand, visible, product_image_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        handle_id, str(row.get('name', '')), str(row.get('description', '')),
                        base_price, str(row.get('collection', '') or ''),
                        str(row.get('brand', '') or ''),
                        str(row.get('visible', 'true')), image_url
                    ))
                    current_product_id = cursor.lastrowid

                products_imported += 1
                current_handle_id = handle_id

            except Exception as e:
                errors += 1
                print(f"[import] product error: {e}")

        # Import variants
        for _, row in variant_rows.iterrows():
            try:
                sku = str(row.get('sku', '')).strip()
                if not sku:
                    continue

                # Find product by SKU's presence after the last Product row
                # Actually, we need to associate variants with products based on row order
                # The catalog format: Product row followed by its Variant rows
                # So we track current_product_id as we iterate in original order

                surcharge = 0.0
                if pd.notna(row.get('surcharge')):
                    try:
                        surcharge = float(str(row['surcharge']).replace(',', ''))
                    except (ValueError, TypeError):
                        surcharge = 0.0

                # Get option values
                opt_values = {}
                for i in range(1, 7):
                    ov = row.get(f'productOptionDescription{i}', '')
                    if pd.notna(ov) and str(ov).strip():
                        opt_values[i] = str(ov).strip()

                cursor.execute("SELECT id FROM variants WHERE sku = ?", (sku,))
                existing = cursor.fetchone()

                if existing:
                    cursor.execute("""
                        UPDATE variants SET
                            surcharge = ?, visible = ?, done = 0, updated_at = CURRENT_TIMESTAMP
                        WHERE sku = ?
                    """, (surcharge, str(row.get('visible', 'true')), sku))
                    variant_id = existing['id']
                else:
                    cursor.execute("""
                        INSERT INTO variants
                        (product_id, sku, surcharge, visible,
                         option_1_name, option_1_value,
                         option_2_name, option_2_value,
                         option_3_name, option_3_value,
                         option_4_name, option_4_value,
                         option_5_name, option_5_value,
                         option_6_name, option_6_value)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        current_product_id, sku, surcharge,
                        str(row.get('visible', 'true')),
                        option_names.get(current_handle_id, {}).get(1, ''), opt_values.get(1, ''),
                        option_names.get(current_handle_id, {}).get(2, ''), opt_values.get(2, ''),
                        option_names.get(current_handle_id, {}).get(3, ''), opt_values.get(3, ''),
                        option_names.get(current_handle_id, {}).get(4, ''), opt_values.get(4, ''),
                        option_names.get(current_handle_id, {}).get(5, ''), opt_values.get(5, ''),
                        option_names.get(current_handle_id, {}).get(6, ''), opt_values.get(6, ''),
                    ))
                    variant_id = cursor.lastrowid

                variants_imported += 1

            except Exception as e:
                errors += 1
                print(f"[import] variant error: {e}")

        # Also handle Product rows that have embedded variant data (no separate Variant rows)
        for _, row in product_rows.iterrows():
            if pd.isna(row.get('sku')) or str(row.get('sku', '')).strip() == '':
                # This is a pure Product row — check if we already created a variant for it
                handle_id = str(row.get('handleId', '')).strip()
                if not handle_id:
                    continue
                cursor.execute("SELECT id FROM products WHERE handle_id = ?", (handle_id,))
                prod = cursor.fetchone()
                if not prod:
                    continue
                product_id = prod['id']
                # Check if this product already has variants
                cursor.execute("SELECT COUNT(*) as cnt FROM variants WHERE product_id = ?", (product_id,))
                if cursor.fetchone()['cnt'] == 0:
                    # No variants exist — create a default variant from the product's price/sku
                    sku = str(row.get('sku', '')).strip()
                    if sku:
                        surcharge = 0.0
                        if pd.notna(row.get('surcharge')):
                            try:
                                surcharge = float(str(row['surcharge']).replace(',', ''))
                            except (ValueError, TypeError):
                                surcharge = 0.0
                        cursor.execute("""
                            INSERT OR IGNORE INTO variants (product_id, sku, surcharge, visible)
                            VALUES (?, ?, ?, ?)
                        """, (product_id, sku, surcharge, str(row.get('visible', 'true'))))
                        variants_imported += 1

        conn.commit()

    return {
        'products_imported': products_imported,
        'variants_imported': variants_imported,
        'errors': errors,
    }

# ─────────────────────────────────────────────────────────────────────────────
# RRP XLSX IMPORTER  (Apple Reseller Program Price List format)
# ─────────────────────────────────────────────────────────────────────────────

def import_rrp_xlsx(filepath: str) -> Dict[str, Any]:
    """
    Import Core Group Apple Reseller Program XLSX.
    Format: SKU | Description | Status (ribbon) | RRP ZAR
    Header row detected dynamically (skip leading text rows).
    """
    df_raw = pd.read_excel(filepath, header=None)

    # Find the header row — look for 'RRP' text in column 3
    header_row = 0
    for i, row in df_raw.iterrows():
        row_vals = [str(v).strip() for v in row.values if pd.notna(v)]
        if any('rrp' in v.lower() for v in row_vals):
            header_row = i
            break

    df = pd.read_excel(filepath, header=header_row)
    df.columns = [str(c).strip() for c in df.columns]

    # Rename columns based on what we found
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if 'sku' in cl or re.match(r'^[a-z0-9]{5,}$', c.strip()):
            col_map[c] = 'sku'
        elif 'rrp' in cl or 'zar' in cl or 'price' in cl or 'incl' in cl:
            col_map[c] = 'rrp'
        elif 'description' in cl or 'product' in cl or 'name' in cl:
            col_map[c] = 'description'
        elif 'status' in cl or 'ribbon' in cl or 'active' in cl or 'eol' in cl.lower():
            col_map[c] = 'status'

    df = df.rename(columns=col_map)

    # Ensure required columns
    if 'sku' not in df.columns or 'rrp' not in df.columns:
        return {'error': 'Could not detect SKU/RRP columns'}

    if 'description' not in df.columns:
        df['description'] = ''
    if 'status' not in df.columns:
        df['status'] = 'active'

    imported = 0
    errors = 0
    skipped = 0

    with get_db_connection() as conn:
        cursor = conn.cursor()

        for _, row in df.iterrows():
            try:
                sku = str(row.get('sku', '')).strip()
                rrp_val = row.get('rrp', '')

                if not sku or sku.lower() in ('nan', 'none', 'sku'):
                    skipped += 1
                    continue

                if pd.isna(rrp_val) or str(rrp_val).strip() in ('', 'nan', 'None'):
                    skipped += 1
                    continue

                # Parse RRP — remove R, spaces, commas
                rrp_str = str(rrp_val).replace('R', '').replace(' ', '').replace(',', '').strip()
                try:
                    rrp = float(rrp_str)
                    if rrp <= 0:
                        skipped += 1
                        continue
                except ValueError:
                    skipped += 1
                    continue

                description = str(row.get('description', '') or '').strip()
                status = str(row.get('status', 'active') or 'active').strip().lower()

                # Determine ribbon
                ribbon = 'active'
                if status in ('eol', 'end of life', 'discontinued', 'obsolete'):
                    ribbon = 'clearance'
                elif status in ('new', 'launch', 'pre-order'):
                    ribbon = 'new_arrival'
                elif status == 'active':
                    ribbon = 'active'
                else:
                    ribbon = 'active'

                cursor.execute("""
                    INSERT OR REPLACE INTO reference_prices (sku, rrp, rrp_description, source_file)
                    VALUES (?, ?, ?, ?)
                """, (sku, rrp, description, os.path.basename(filepath)))
                imported += 1

            except Exception as e:
                errors += 1
                print(f"[import rrp] row error: {e}")

        conn.commit()

    return {
        'imported': imported,
        'skipped': skipped,
        'errors': errors,
    }

def import_rrp_csv(filepath: str) -> Dict[str, Any]:
    """Import RRP from CSV — auto-detect SKU and RRP columns"""
    try:
        df = pd.read_csv(filepath, sep=';', on_bad_lines='skip')
        if len(df.columns) <= 1:
            df = pd.read_csv(filepath, sep=',', on_bad_lines='skip')
    except Exception as e:
        return {'error': str(e)}

    df.columns = [str(c).strip() for c in df.columns]

    sku_col = next((c for c in df.columns if 'sku' in c.lower()), df.columns[0])
    rrp_col = next((c for c in df.columns if 'rrp' in c.lower() or 'zar' in c.lower() or 'price' in c.lower()), df.columns[1])

    df = df.rename(columns={sku_col: 'sku', rrp_col: 'rrp'})

    imported = 0
    with get_db_connection() as conn:
        cursor = conn.cursor()
        for _, row in df.iterrows():
            sku = str(row.get('sku', '')).strip()
            rrp_val = row.get('rrp', '')
            if not sku or sku.lower() in ('nan', 'sku'):
                continue
            try:
                rrp_str = str(rrp_val).replace('R', '').replace(' ', '').replace(',', '.').strip()
                rrp = float(rrp_str)
                if rrp > 0:
                    cursor.execute("""
                        INSERT OR REPLACE INTO reference_prices (sku, rrp, source_file)
                        VALUES (?, ?, ?)
                    """, (sku, rrp, os.path.basename(filepath)))
                    imported += 1
            except (ValueError, TypeError):
                pass
        conn.commit()

    return {'imported': imported}
