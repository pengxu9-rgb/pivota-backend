"""
PSP Query Templates
Standardized SQL fragments for PSP-related queries
"""

# Standard PSP JOIN condition
# Use this for all queries that need to match orders with PSP configurations
PSP_JOIN_CONDITION = """
LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
    AND o.created_at >= :start_time
    AND (
        -- Primary match: exact psp_id (most reliable)
        (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
        OR 
        -- Fallback match: provider name (case-insensitive)
        (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
         AND LOWER(o.psp_used) = LOWER(mp.provider))
    )
"""

# Standard PSP filter condition (for WHERE clauses)
PSP_FILTER_CONDITION = """
WHERE (
    (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
    OR 
    (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
     AND LOWER(o.psp_used) = LOWER(mp.provider))
)
"""

# PSP matching for aggregations
PSP_AGGREGATION_MATCH = """
COUNT(CASE WHEN 
    (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
    OR 
    (o.psp_used IS NOT NULL AND mp.provider IS NOT NULL 
     AND LOWER(o.psp_used) = LOWER(mp.provider))
THEN o.order_id END) as matching_orders
"""

# Case-insensitive provider match (for simple queries)
PSP_PROVIDER_MATCH = """LOWER(o.psp_used) = LOWER(mp.provider)"""

# Full PSP match (for complex queries)
PSP_FULL_MATCH = """
(o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
OR 
(o.psp_used IS NOT NULL AND mp.provider IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
"""


def get_psp_join_query(
    base_query: str,
    start_time_param: str = ":start_time"
) -> str:
    """
    Inject standard PSP JOIN condition into a query
    
    Usage:
        query = get_psp_join_query('''
            SELECT mp.provider, COUNT(o.order_id)
            FROM merchant_psps mp
            {PSP_JOIN}
            WHERE mp.status = 'active'
        ''')
    """
    return base_query.replace(
        "{PSP_JOIN}",
        PSP_JOIN_CONDITION.replace(":start_time", start_time_param)
    )


def build_psp_stats_query(
    time_range_param: str = ":start_time",
    additional_filters: str = ""
) -> str:
    """
    Build a standard PSP statistics query
    
    Returns query that provides:
    - psp_name, psp_id
    - merchant_count
    - transaction_count, success_count
    - total_volume, avg_transaction_size
    - refund_count
    - last_transaction
    """
    query = f"""
    SELECT 
        mp.provider as psp_name,
        mp.psp_id,
        mp.status,
        COUNT(DISTINCT mp.merchant_id) as merchant_count,
        COUNT(CASE WHEN 
            (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
            OR 
            (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
        THEN o.order_id END) as transaction_count,
        COUNT(CASE WHEN o.payment_status = 'paid' AND (
            (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
            OR 
            (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
        ) THEN 1 END) as success_count,
        COALESCE(SUM(CASE WHEN o.payment_status = 'paid' AND (
            (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
            OR 
            (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
        ) THEN o.total ELSE 0 END), 0) as total_volume,
        AVG(CASE WHEN o.payment_status = 'paid' AND (
            (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
            OR 
            (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
        ) THEN o.total ELSE NULL END) as avg_transaction_size,
        COUNT(CASE WHEN o.payment_status IN ('refunded', 'partially_refunded') AND (
            (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
            OR 
            (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
        ) THEN 1 END) as refund_count,
        MAX(CASE WHEN (
            (o.psp_id IS NOT NULL AND mp.psp_id IS NOT NULL AND o.psp_id = mp.psp_id)
            OR 
            (o.psp_used IS NOT NULL AND LOWER(o.psp_used) = LOWER(mp.provider))
        ) THEN o.created_at END) as last_transaction
    FROM merchant_psps mp
    LEFT JOIN orders o ON o.merchant_id = mp.merchant_id 
        AND o.created_at >= {time_range_param}
    WHERE mp.status = 'active'
    {additional_filters}
    GROUP BY mp.provider, mp.psp_id, mp.status
    ORDER BY transaction_count DESC NULLS LAST
    """
    return query


# Example usage patterns
EXAMPLE_USAGE = """
# Example 1: Simple PSP overview query
from utils.psp_query_templates import build_psp_stats_query

query = build_psp_stats_query(time_range_param=":start_time")
results = await database.fetch_all(query, {"start_time": start_time})

# Example 2: Custom query with PSP JOIN
from utils.psp_query_templates import get_psp_join_query

query = get_psp_join_query('''
    SELECT mp.provider, COUNT(o.order_id) as orders
    FROM merchant_psps mp
    {PSP_JOIN}
    WHERE mp.merchant_id = :merchant_id
    GROUP BY mp.provider
''')
results = await database.fetch_all(query, {
    "merchant_id": merchant_id,
    "start_time": start_time
})

# Example 3: Using templates directly
from utils.psp_query_templates import PSP_FULL_MATCH

query = f'''
    SELECT * FROM orders o
    JOIN merchant_psps mp ON o.merchant_id = mp.merchant_id
    WHERE ({PSP_FULL_MATCH})
'''
"""

