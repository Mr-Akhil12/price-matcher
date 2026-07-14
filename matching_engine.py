"""
Intelligent Matching Engine for Product Price Matcher v2

Matches catalog variants to RRP reference prices using:
1. Exact SKU match → guaranteed (trust 1.0)
2. Fuzzy/semantic match on product name + options vs RRP description → believed (trust 0.6-0.95)
3. No match → unmatched (trust 0.0)
"""
import re
from difflib import SequenceMatcher
from typing import Dict, List, Tuple, Optional

def normalise(text: str) -> str:
    """Strip, lowercase, collapse spaces — for comparison"""
    if not text:
        return ""
    return re.sub(r'\s+', ' ', text.strip().lower())

def tokenise(text: str) -> set:
    """Tokenise into words for set-based matching"""
    return set(normalise(text).split())

def sku_match(catalog_sku: str, rrp_sku: str) -> bool:
    """Exact SKU comparison — case insensitive"""
    return normalise(catalog_sku) == normalise(rrp_sku)

def fuzzy_score(catalog_name: str, rrp_description: str, catalog_options: str = "") -> float:
    """
    Calculate a fuzzy match score between catalog product name+options
    and RRP description. Returns 0.0 to 1.0.
    """
    if not catalog_name or not rrp_description:
        return 0.0

    cat_norm = normalise(catalog_name)
    rrp_norm = normalise(rrp_description)
    opt_norm = normalise(catalog_options) if catalog_options else ""

    combined = f"{cat_norm} {opt_norm}"

    # 1. Sequence match ratio on combined text
    seq_score = SequenceMatcher(None, combined, rrp_norm).ratio()

    # 2. Token overlap (Jaccard)
    cat_tokens = tokenise(combined)
    rrp_tokens = tokenise(rrp_description)
    if not cat_tokens or not rrp_tokens:
        token_score = 0.0
    else:
        intersection = len(cat_tokens & rrp_tokens)
        union = len(cat_tokens | rrp_tokens)
        token_score = intersection / union if union > 0 else 0.0

    # 3. Substring check — if one is contained in the other
    substr_score = 1.0 if (cat_norm in rrp_norm or rrp_norm in cat_norm) else 0.0

    # 4. SKU prefix match (first 3+ alphanumeric chars should align)
    cat_prefix = re.match(r'[A-Z0-9]{3,}', catalog_sku_norm := normalise(catalog_name)[:20])
    rrp_prefix = re.match(r'[A-Z0-9]{3,}', rrp_norm[:20])
    prefix_score = 0.3 if (cat_prefix and rrp_prefix and cat_prefix.group() == rrp_prefix.group()) else 0.0

    # Weighted final score
    score = (seq_score * 0.35) + (token_score * 0.40) + (substr_score * 0.20) + (prefix_score * 0.05)

    # Boost if critical tokens match (e.g. model numbers, capacity)
    critical_boost = 0.0
    critical_patterns = [
        r'\d{2,4}\s?gb',      # capacity like 256gb, 512 gb
        r'\d{3,4}',           # model numbers like M4, A16
        r'iphone\s*\d+',      # iPhone model
        r'macbook\s*(pro|air)',
        r'ipad\s*(pro|air|mini)',
        r'airpods\s*(pro|max)?',
    ]
    for pat in critical_patterns:
        if re.search(pat, cat_norm) and re.search(pat, rrp_norm):
            critical_boost += 0.05

    score = min(score + critical_boost, 1.0)
    return round(score, 3)

def classify_match(score: float) -> Tuple[str, float]:
    """
    Classify a match based on score thresholds.
    Returns (match_status, trust_score).
    """
    if score >= 0.90:
        return 'guaranteed', score
    elif score >= 0.55:
        return 'believed', score
    else:
        return 'unmatched', score

def match_variant_to_rrp(
    variant_sku: str,
    product_name: str,
    catalog_options: str,
    reference_prices: List[Dict]
) -> Optional[Dict]:
    """
    Find the best RRP match for a single catalog variant.
    Returns the best match dict with score, or None if no match above threshold.
    """
    best_match = None
    best_score = 0.0

    for rp in reference_prices:
        rrp_sku = rp.get('sku', '')
        rrp_desc = rp.get('rrp_description', '') or ''

        # 1. Exact SKU match — immediate guaranteed
        if sku_match(variant_sku, rrp_sku):
            return {
                'rrp_id': rp['id'],
                'rrp_sku': rrp_sku,
                'rrp_price': rp['rrp'],
                'status': 'guaranteed',
                'trust_score': 1.0,
            }

        # 2. Fuzzy match
        score = fuzzy_score(product_name, rrp_desc, catalog_options)
        if score > best_score:
            best_score = score
            best_match = rp

    if best_match and best_score >= 0.55:
        status, trust = classify_match(best_score)
        return {
            'rrp_id': best_match['id'],
            'rrp_sku': best_match['sku'],
            'rrp_price': best_match['rrp'],
            'status': status,
            'trust_score': trust,
        }

    return None

def run_full_match() -> Dict[str, int]:
    """
    Run matching on all unmatched variants.
    Updates the database directly via db_utils.
    """
    from db_utils import get_db_connection, get_all_reference_prices

    reference_prices = get_all_reference_prices()
    if not reference_prices:
        return {'updated': 0, 'skipped': 0}

    results = {'updated': 0, 'skipped': 0}

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT v.id, v.sku, v.option_1_value, v.option_2_value, v.option_3_value,
                   p.name as product_name
            FROM variants v
            JOIN products p ON v.product_id = p.id
        """)

        for row in cursor.fetchall():
            variant_id = row['id']
            variant_sku = row['sku'] or ''
            options = ' '.join(filter(None, [
                row.get('option_1_value', ''),
                row.get('option_2_value', ''),
                row.get('option_3_value', ''),
            ]))
            product_name = row['product_name'] or ''

            match = match_variant_to_rrp(variant_sku, product_name, options, reference_prices)

            if match:
                cursor.execute("""
                    UPDATE variants
                    SET match_status = ?, matched_rrp_id = ?, trust_score = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (match['status'], match['rrp_id'], match['trust_score'], variant_id))
                results['updated'] += 1
            else:
                cursor.execute("""
                    UPDATE variants
                    SET match_status = 'unmatched', matched_rrp_id = NULL, trust_score = 0.0, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (variant_id,))
                results['skipped'] += 1

        conn.commit()

    return results
