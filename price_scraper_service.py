"""
Price Scraper Service — wraps price_scraper.py for Flask use
"""
import logging
from typing import Dict, Any
from db_utils import get_db_connection

logger = logging.getLogger(__name__)

def scrape_variant_prices(variant_id: int) -> Dict[str, Any]:
    """
    Scrape competitor prices for a single variant.
    Integrates with the existing price_scraper.py logic.
    """
    try:
        from price_scraper import PriceScraper

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT v.sku, v.id, p.name as product_name,
                       v.option_1_value, v.option_2_value, v.option_3_value
                FROM variants v
                JOIN products p ON v.product_id = p.id
                WHERE v.id = ?
            """, (variant_id,))
            row = cursor.fetchone()
            if not row:
                return {'error': 'Variant not found'}

            sku = row['sku'] or ''
            product_name = row['product_name'] or ''
            options = ' '.join(filter(None, [
                row.get('option_1_value', ''),
                row.get('option_2_value', ''),
                row.get('option_3_value', ''),
            ]))
            search_term = f"{product_name} {options}".strip()

        scraper = PriceScraper()
        results = scraper.search_product(search_term, sku)

        scraped_count = 0
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Clear old data
            cursor.execute("DELETE FROM price_comparisons WHERE variant_id = ?", (variant_id,))

            for result in results:
                if result and result.price:
                    cursor.execute("""
                        INSERT INTO price_comparisons
                        (variant_id, site_name, external_price, external_url, external_product_name, availability)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (
                        variant_id,
                        result.site_name,
                        result.price,
                        result.url,
                        result.product_name[:200] if result.product_name else '',
                        result.availability,
                    ))
                    scraped_count += 1
            conn.commit()

        return {'scraped': scraped_count, 'sites': [r.site_name for r in results if r]}

    except ImportError:
        logger.warning("price_scraper not available — scraping disabled")
        return {'scraped': 0, 'error': 'Scraper not available'}
    except Exception as e:
        logger.error(f"Scraping error for variant {variant_id}: {e}")
        return {'scraped': 0, 'error': str(e)}
