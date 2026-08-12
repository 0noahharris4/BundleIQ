"""
Market basket analysis core for BundleIQ.

Deliberately NOT scikit-learn -- association rule mining here is just
counting: group line items into baskets, count how often products appear
alone and together, and derive support / confidence / lift from those
counts. That means there's no model to train and no coefficients to export
later for a standalone build -- the exact same counting logic can be ported
to JS verbatim and will match the Python output bit-for-bit on the same
input, the same way the ARV/rent trees were exported for InvestIQ, just
simpler because there's nothing stochastic about it.

Definitions (standard market-basket terminology):
  support(X)    = fraction of orders containing X
  support(X,Y)  = fraction of orders containing BOTH X and Y
  confidence(X->Y) = support(X,Y) / support(X)   -- "given X, how often Y too"
  lift(X,Y)     = support(X,Y) / (support(X) * support(Y))
                  -- >1 means X and Y co-occur more than chance; the
                     headline "strength of association" number

Product-name normalization: real exports are never perfectly consistent
("EGGS" vs "eggs" vs " Eggs "). Every product is grouped internally by a
case/whitespace-insensitive key (_normalize_key), and the most common
original spelling for that key is chosen as the display name shown in
results -- so messy input still produces one clean row per product instead
of splitting counts across near-duplicate names.

Seasonality: if the upload includes an (optional) order_date column, each
order is bucketed into one of four calendar seasons and we compute, for
each top product, what share of each season's orders included it --
"seasonal support". That answers "which products sell in which season" and
"how much does this product's popularity swing across the year" using the
exact same counting approach as everything else here -- no forecasting
model, just a second cut of the same basket data across a time dimension.
"""

import itertools
import re
from collections import Counter, defaultdict

MIN_LIFT = 1.15
HEATMAP_TOP_N = 14

SEASONS = ["Winter", "Spring", "Summer", "Fall"]
SEASON_BY_MONTH = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer",
    9: "Fall", 10: "Fall", 11: "Fall",
}
MIN_DATED_ORDERS = 20  # below this, a seasonal breakdown is mostly noise


def _normalize_key(name):
    """Case/whitespace-insensitive grouping key for a product name."""
    return re.sub(r"\s+", " ", str(name).strip().lower())


def _season_of(date_value):
    """A 'YYYY-MM-DD'-prefixed string (or anything with that shape) -> one
    of SEASONS, or None if missing/unparseable. Callers pass already-cleaned
    ISO date strings (see app.py), so this just reads the month digits."""
    if not date_value:
        return None
    s = str(date_value).strip()
    if len(s) < 7 or s[4] != "-":
        return None
    try:
        month = int(s[5:7])
    except ValueError:
        return None
    return SEASON_BY_MONTH.get(month)


def build_baskets(rows):
    """rows: iterable of dicts with at least order_id, product_name (and
    optionally category, quantity, unit_price, order_date). Returns baskets
    (dict order_id -> set of normalized-key product names) keyed internally
    by normalization key, plus per-key metadata, a key -> display_name map
    (the most common original spelling seen for that key), and an
    order_id -> season map for whichever orders had a usable date."""
    baskets = defaultdict(set)
    product_category = {}
    product_price = {}
    product_revenue = Counter()
    product_qty = Counter()
    variant_counts = defaultdict(Counter)
    order_season = {}

    for r in rows:
        order_id = str(r["order_id"]).strip()
        raw_product = str(r["product_name"]).strip()
        key = _normalize_key(raw_product)
        if not order_id or not key:
            continue
        baskets[order_id].add(key)
        variant_counts[key][raw_product] += 1

        category = r.get("category")
        if category and str(category).strip():
            product_category[key] = str(category).strip()

        qty = r.get("quantity")
        try:
            qty = float(qty) if qty not in (None, "") else 1.0
        except (TypeError, ValueError):
            qty = 1.0

        price = r.get("unit_price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        if price is not None:
            product_price[key] = price
            product_revenue[key] += price * qty
        product_qty[key] += qty

        if order_id not in order_season:
            season = _season_of(r.get("order_date"))
            if season is not None:
                order_season[order_id] = season

    display_name = {key: counts.most_common(1)[0][0] for key, counts in variant_counts.items()}

    return baskets, product_category, product_price, product_revenue, product_qty, display_name, order_season


def analyze(rows, min_lift=MIN_LIFT):
    baskets, product_category, product_price, product_revenue, product_qty, display_name, order_season = build_baskets(rows)
    n_orders = len(baskets)
    if n_orders == 0:
        raise ValueError("No valid orders found in the uploaded file.")

    product_counts = Counter()
    pair_counts = Counter()
    basket_sizes = []

    for order_id, items in baskets.items():
        basket_sizes.append(len(items))
        for p in items:
            product_counts[p] += 1
        for a, b in itertools.combinations(sorted(items), 2):
            pair_counts[(a, b)] += 1

    min_count = max(3, round(n_orders * 0.005))  # ignore pairs seen fewer than ~0.5% of orders (or 3, whichever is bigger)

    support = {p: c / n_orders for p, c in product_counts.items()}

    rules = []
    for (a, b), count in pair_counts.items():
        if count < min_count:
            continue
        sup_ab = count / n_orders
        sup_a, sup_b = support[a], support[b]
        if sup_a == 0 or sup_b == 0:
            continue
        lift = sup_ab / (sup_a * sup_b)
        if lift < min_lift:
            continue
        conf_a_to_b = count / product_counts[a]
        conf_b_to_a = count / product_counts[b]
        if conf_a_to_b >= conf_b_to_a:
            head, tail, conf = a, b, conf_a_to_b
            conf_reverse = conf_b_to_a
        else:
            head, tail, conf = b, a, conf_b_to_a
            conf_reverse = conf_a_to_b
        rules.append({
            "product_a": display_name[head],
            "product_b": display_name[tail],
            "co_occurrences": count,
            "support_pct": round(sup_ab * 100, 2),
            "confidence_pct": round(conf * 100, 1),
            "confidence_reverse_pct": round(conf_reverse * 100, 1),
            "lift": round(lift, 2),
        })

    rules.sort(key=lambda r: (-r["lift"], r["product_a"], r["product_b"]))

    top_products = sorted(product_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:HEATMAP_TOP_N]
    top_product_keys = [p for p, _ in top_products]

    heatmap = []
    for a in top_product_keys:
        row = []
        for b in top_product_keys:
            if a == b:
                row.append(None)
                continue
            key = (a, b) if a < b else (b, a)
            count = pair_counts.get(key, 0)
            if count < min_count:
                row.append(0.0)
                continue
            sup_ab = count / n_orders
            lift = sup_ab / (support[a] * support[b]) if support[a] and support[b] else 0.0
            row.append(round(lift, 2))
        heatmap.append(row)

    # ---------------------------------------------------------- seasonality --
    # For each top product: what share of each season's orders included it
    # ("seasonal support", pct_matrix), and how that compares to the
    # product's own average share across all dated orders ("seasonal
    # index", matrix -- 1.0 = typical, >1 = over-indexes that season). The
    # index is what drives the chart's color, the same way lift (not raw
    # support) drives the association heatmap -- a product that's popular
    # everywhere shouldn't look "seasonal" just because it sells a lot in
    # absolute terms. Only orders with a usable order_date count here;
    # everything else in this function is unaffected by whether dates exist.
    seasonality = None
    if len(order_season) >= MIN_DATED_ORDERS:
        n_dated = len(order_season)
        season_order_count = Counter(order_season.values())
        product_season_counts = defaultdict(Counter)
        product_dated_count = Counter()
        for oid, season in order_season.items():
            for p in baskets[oid]:
                product_season_counts[p][season] += 1
                product_dated_count[p] += 1

        matrix = []
        pct_matrix = []
        for p in top_product_keys:
            avg_share = product_dated_count[p] / n_dated if n_dated else 0
            idx_row = []
            pct_row = []
            for s in SEASONS:
                denom = season_order_count.get(s, 0)
                if not denom or not avg_share:
                    idx_row.append(None)
                    pct_row.append(None)
                    continue
                share = product_season_counts[p][s] / denom
                idx_row.append(round(share / avg_share, 2))
                pct_row.append(round(share * 100, 1))
            matrix.append(idx_row)
            pct_matrix.append(pct_row)

        seasonality = {
            "seasons": SEASONS,
            "products": [display_name[p] for p in top_product_keys],
            "matrix": matrix,
            "pct_matrix": pct_matrix,
            "n_dated_orders": len(order_season),
            "season_order_counts": [season_order_count.get(s, 0) for s in SEASONS],
        }

    products_summary = []
    for p, count in sorted(product_counts.items(), key=lambda kv: (-kv[1], kv[0])):
        products_summary.append({
            "product_name": display_name[p],
            "category": product_category.get(p),
            "order_count": count,
            "support_pct": round(support[p] * 100, 2),
            "revenue": round(product_revenue[p], 2) if p in product_price else None,
        })

    has_price = len(product_price) > 0
    total_revenue = round(sum(product_revenue.values()), 2) if has_price else None

    return {
        "n_orders": n_orders,
        "n_unique_products": len(product_counts),
        "avg_basket_size": round(sum(basket_sizes) / len(basket_sizes), 2),
        "n_rules": len(rules),
        "has_price": has_price,
        "total_revenue": total_revenue,
        "rules": rules,
        "products": products_summary,
        "heatmap": {"products": [display_name[p] for p in top_product_keys], "matrix": heatmap},
        "seasonality": seasonality,
    }
