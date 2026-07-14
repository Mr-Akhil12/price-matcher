"""
Intelligent Matching Engine for Product Price Matcher v2

Matching strategy:
1. Exact SKU match (case-insensitive) → guaranteed (trust 1.0)
2. Fuzzy match via token pre-filter → believed (trust 0.55-0.95)
3. No match → unmatched (trust 0.0)
"""
import re
from difflib import SequenceMatcher
from typing import Dict, List, Tuple, Optional

# ─── NORMALISATION ────────────────────────────────────────────────────────────

def normalise(text: str) -> str:
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text.strip().lower())

def tokenise(text: str) -> set:
    return set(normalise(text).split())

def sku_match(catalog_sku: str, rrp_sku: str) -> bool:
    """Exact SKU match — case insensitive."""
    return normalise(catalog_sku) == normalise(rrp_sku)

# ─── SCORING ─────────────────────────────────────────────────────────────────

def fuzzy_score(catalog_name: str, rrp_description: str, catalog_options: str = "") -> float:
    """
    Calculate fuzzy match score between catalog name+options and RRP description.
    Returns 0.0 to 1.0.
    """
    if not catalog_name or not rrp_description:
        return 0.0

    cat_norm = normalise(catalog_name)
    rrp_norm = normalise(rrp_description)
    combined = f"{cat_norm} {normalise(catalog_options)}"

    seq_score = SequenceMatcher(None, combined, rrp_norm).ratio()

    cat_tokens = tokenise(combined)
    rrp_tokens = tokenise(rrp_description)
    token_score = (
        len(cat_tokens & rrp_tokens) / len(cat_tokens | rrp_tokens)
        if (cat_tokens and rrp_tokens) else 0.0
    )

    substr_score = 1.0 if (cat_norm in rrp_norm or rrp_norm in cat_norm) else 0.0

    score = (seq_score * 0.35) + (token_score * 0.35) + (substr_score * 0.30)
    return round(min(score, 1.0), 3)

def classify_match(score: float) -> Tuple[str, float]:
    if score >= 0.90:
        return 'guaranteed', score
    elif score >= 0.55:
        return 'believed', score
    return 'unmatched', score

# ─── MATCHING ────────────────────────────────────────────────────────────────

def run_full_match() -> Dict[str, int]:
    """
    Run matching on all variants.
    1. Exact SKU match via dict → guaranteed
    2. Token pre-filter + fuzzy score → believed (only candidates with shared tokens)
    """
    from db_utils import get_db_connection, get_all_reference_prices

    reference_prices = get_all_reference_prices()
    if not reference_prices:
        return {'updated': 0, 'skipped': 0}

    # Build O(1) exact SKU lookup
    sku_lookup: Dict[str, Dict] = {}
    for rp in reference_prices:
        key = normalise(rp['sku'])
        if key:
            sku_lookup[key] = rp

    results = {'updated': 0, 'skipped': 0}

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.id, v.sku,
                   v.option_1_value, v.option_2_value, v.option_3_value,
                   p.name as product_name
            FROM variants v
            JOIN products p ON v.product_id = p.id
        """)

        for row in cursor.fetchall():
            variant_id = row['id']
            variant_sku = row['sku'] or ''
            options = ' '.join(filter(None, [
                (row['option_1_value'] or '').strip(),
                (row['option_2_value'] or '').strip(),
                (row['option_3_value'] or '').strip(),
            ])).strip()
            product_name = (row['product_name'] or '').strip()

            norm_sku = normalise(variant_sku)

            # 1. Exact SKU match → guaranteed
            if norm_sku in sku_lookup:
                rp = sku_lookup[norm_sku]
                cursor.execute("""
                    UPDATE variants
                    SET match_status = 'guaranteed', matched_rrp_id = ?,
                        trust_score = 1.0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (rp['id'], variant_id))
                results['updated'] += 1
                continue

            # 2. Fuzzy match with token pre-filter (avoids full O(n*m) scan)
            product_tokens = set(normalise(product_name + ' ' + options).split())
            best_match = None
            best_score = 0.0

            for rp in reference_prices:
                rrp_desc = rp.get('rrp_description') or ''
                rrp_tokens = set(normalise(rrp_desc).split())
                # Quick pre-filter: skip if zero token overlap
                if product_tokens and rrp_tokens and not (product_tokens & rrp_tokens):
                    continue
                score = fuzzy_score(product_name, rrp_desc, options)
                if score > best_score:
                    best_score = score
                    best_match = rp

            if best_match and best_score >= 0.55:
                status, trust = classify_match(best_score)
                cursor.execute("""
                    UPDATE variants
                    SET match_status = ?, matched_rrp_id = ?,
                        trust_score = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (status, best_match['id'], trust, variant_id))
                results['updated'] += 1
            else:
                cursor.execute("""
                    UPDATE variants
                    SET match_status = 'unmatched', trust_score = 0.0,
                        matched_rrp_id = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (variant_id,))
                results['skipped'] += 1

        conn.commit()

    return results
