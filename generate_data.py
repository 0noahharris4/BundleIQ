"""
Synthetic data generator for BundleIQ -- a market basket analysis tool.

Produces:
  transactions.json              -- ~6,000 orders x line items (the data the
                                     live app "trains" nothing on -- there's
                                     no ML model here, just counting -- but
                                     this is the reference dataset used to
                                     validate the rules engine).
  sample_data/sample_transactions.csv -- what an end user's own upload looks
                                     like; powers the "try with sample data"
                                     button.
  sample_data/template.csv       -- header + example rows, downloadable from
                                     the app.

Each order is generated from one or two "affinity themes" (breakfast, pasta
night, taco night, BBQ, ...) plus a little random noise, so genuine,
discoverable co-purchase patterns exist for the rules engine to (re)find --
the same "bake in structure, then let the algorithm recover it" approach
used for every ML model earlier in this session, just with combinatorics
standing in for a trained classifier.
"""

import csv
import datetime
import json
import random

random.seed(7)

N_ORDERS = 6000

# product -> (category, unit_price)
CATALOG = {
    "Eggs": ("Breakfast", 3.49), "Bacon": ("Breakfast", 5.99), "Coffee Beans": ("Beverages", 8.99),
    "Coffee Filters": ("Breakfast", 2.49), "Creamer": ("Breakfast", 3.29), "Orange Juice": ("Beverages", 4.49),
    "Cereal": ("Breakfast", 4.29), "Milk": ("Breakfast", 3.19), "Bread": ("Breakfast", 2.99),
    "Butter": ("Breakfast", 3.79),
    "Spaghetti": ("Pasta", 1.79), "Marinara Sauce": ("Pasta", 3.49), "Parmesan Cheese": ("Pasta", 4.99),
    "Garlic Bread": ("Pasta", 3.29), "Ground Beef": ("Meat", 6.49), "Basil": ("Produce", 2.19),
    "Tortillas": ("Mexican", 2.99), "Salsa": ("Mexican", 3.49), "Shredded Cheese": ("Dairy", 4.29),
    "Sour Cream": ("Dairy", 2.29), "Avocado": ("Produce", 1.29), "Lime": ("Produce", 0.59),
    "Burger Buns": ("Bakery", 2.79), "Ketchup": ("Condiments", 2.99), "Mustard": ("Condiments", 2.29),
    "Charcoal": ("Outdoor", 7.99), "Potato Chips": ("Snacks", 3.99), "Soda": ("Beverages", 4.99),
    "Dish Soap": ("Cleaning", 3.29), "Sponges": ("Cleaning", 2.49), "Paper Towels": ("Cleaning", 5.49),
    "Laundry Detergent": ("Cleaning", 8.99), "Trash Bags": ("Cleaning", 6.49),
    "Diapers": ("Baby", 19.99), "Baby Wipes": ("Baby", 6.99), "Baby Formula": ("Baby", 22.99),
    "Baby Food": ("Baby", 4.99),
    "Pretzels": ("Snacks", 3.49), "Cookies": ("Snacks", 3.99), "Candy": ("Snacks", 2.49),
    "Shampoo": ("Personal Care", 5.99), "Toothpaste": ("Personal Care", 3.29),
    "Toilet Paper": ("Personal Care", 8.99), "Soap": ("Personal Care", 2.99),
    "Apples": ("Produce", 3.99), "Bananas": ("Produce", 1.79), "Lettuce": ("Produce", 2.29),
    "Tomatoes": ("Produce", 2.99),
    "Bottled Water": ("Beverages", 4.49),
}

THEMES = {
    "breakfast": {"Eggs": 0.70, "Bacon": 0.55, "Coffee Beans": 0.50, "Coffee Filters": 0.35, "Creamer": 0.42,
                  "Orange Juice": 0.32, "Milk": 0.48, "Bread": 0.42, "Butter": 0.30, "Cereal": 0.25},
    "pasta_night": {"Spaghetti": 0.78, "Marinara Sauce": 0.72, "Parmesan Cheese": 0.52, "Garlic Bread": 0.46,
                     "Ground Beef": 0.34, "Basil": 0.22},
    "taco_night": {"Tortillas": 0.78, "Salsa": 0.62, "Shredded Cheese": 0.56, "Ground Beef": 0.50,
                    "Sour Cream": 0.42, "Avocado": 0.36, "Lime": 0.28},
    "bbq": {"Burger Buns": 0.72, "Ground Beef": 0.55, "Ketchup": 0.46, "Mustard": 0.30, "Charcoal": 0.42,
            "Potato Chips": 0.36, "Soda": 0.40},
    "cleaning": {"Dish Soap": 0.60, "Sponges": 0.50, "Paper Towels": 0.55, "Laundry Detergent": 0.40,
                 "Trash Bags": 0.36},
    "baby": {"Diapers": 0.80, "Baby Wipes": 0.72, "Baby Formula": 0.40, "Baby Food": 0.36},
    "snacks": {"Potato Chips": 0.48, "Pretzels": 0.36, "Soda": 0.44, "Cookies": 0.40, "Candy": 0.30},
    "self_care": {"Shampoo": 0.50, "Toothpaste": 0.46, "Toilet Paper": 0.56, "Soap": 0.40},
    "produce": {"Apples": 0.50, "Bananas": 0.56, "Lettuce": 0.40, "Tomatoes": 0.40, "Avocado": 0.28},
    "beverages": {"Coffee Beans": 0.34, "Soda": 0.38, "Bottled Water": 0.46, "Orange Juice": 0.28},
}

THEME_WEIGHTS = {
    "breakfast": 0.16, "pasta_night": 0.10, "taco_night": 0.10, "bbq": 0.08, "cleaning": 0.12,
    "baby": 0.06, "snacks": 0.12, "self_care": 0.10, "produce": 0.10, "beverages": 0.06,
}

# How much more/less likely each theme is to drive a basket in a given
# season, relative to its baseline THEME_WEIGHTS -- this is what gives the
# app's seasonality chart genuine, discoverable structure to find (grilling
# in summer, spring cleaning, cold-weather comfort food, etc.) instead of
# random noise. 1.0 = no seasonal effect.
SEASONS = ["Winter", "Spring", "Summer", "Fall"]
SEASON_BY_MONTH = {
    12: "Winter", 1: "Winter", 2: "Winter",
    3: "Spring", 4: "Spring", 5: "Spring",
    6: "Summer", 7: "Summer", 8: "Summer",
    9: "Fall", 10: "Fall", 11: "Fall",
}
THEME_SEASON_MULTIPLIER = {
    "breakfast":   {"Winter": 1.0, "Spring": 1.0, "Summer": 1.0, "Fall": 1.0},
    "pasta_night": {"Winter": 1.4, "Spring": 0.9, "Summer": 0.6, "Fall": 1.3},
    "taco_night":  {"Winter": 0.8, "Spring": 1.1, "Summer": 1.3, "Fall": 0.9},
    "bbq":         {"Winter": 0.15, "Spring": 0.9, "Summer": 2.6, "Fall": 0.5},
    "cleaning":    {"Winter": 0.7, "Spring": 1.6, "Summer": 1.0, "Fall": 0.9},
    "baby":        {"Winter": 1.0, "Spring": 1.0, "Summer": 1.0, "Fall": 1.0},
    "snacks":      {"Winter": 1.3, "Spring": 0.9, "Summer": 1.0, "Fall": 1.1},
    "self_care":   {"Winter": 1.2, "Spring": 1.0, "Summer": 0.9, "Fall": 1.0},
    "produce":     {"Winter": 0.6, "Spring": 1.2, "Summer": 1.6, "Fall": 1.0},
    "beverages":   {"Winter": 0.7, "Spring": 1.1, "Summer": 1.6, "Fall": 0.9},
}

ORDER_DATE_START = datetime.date(2025, 1, 1)
ORDER_DATE_SPAN_DAYS = 365


def _random_order_date():
    return ORDER_DATE_START + datetime.timedelta(days=random.randrange(ORDER_DATE_SPAN_DAYS))


ALL_PRODUCTS = list(CATALOG.keys())

# A small fraction of line items get a case/whitespace-mangled product name --
# real point-of-sale exports are never perfectly consistent ("EGGS" on one
# register, "eggs " on another). rules_engine.py normalizes these back
# together (see _normalize_key), and this is what exercises that path.
MESSY_NAME_RATE = 0.05


def _messy_variant(name):
    r = random.random()
    if r < 0.35:
        return name.upper()
    elif r < 0.70:
        return name.lower()
    elif r < 0.88:
        return f" {name} "
    else:
        return "  ".join(name.split())  # doubled internal spacing


def gen_basket(season):
    theme_names = list(THEME_WEIGHTS.keys())
    weights = [THEME_WEIGHTS[t] * THEME_SEASON_MULTIPLIER[t][season] for t in theme_names]
    primary = random.choices(theme_names, weights=weights)[0]

    items = set()
    for product, p in THEMES[primary].items():
        if random.random() < p:
            items.add(product)

    if random.random() < 0.30:
        secondary = random.choices(theme_names, weights=weights)[0]
        for product, p in THEMES[secondary].items():
            if random.random() < p * 0.6:  # secondary theme items appear less reliably
                items.add(product)

    # a little noise so baskets aren't purely theme-shaped
    for _ in range(random.choices([0, 1, 2], weights=[0.55, 0.30, 0.15])[0]):
        items.add(random.choice(ALL_PRODUCTS))

    if not items:
        items.add(random.choice(ALL_PRODUCTS))

    return items


def gen_transactions(n_orders, messy_rate=MESSY_NAME_RATE):
    rows = []
    for i in range(n_orders):
        order_id = f"ORD{i+1:05d}"
        order_date = _random_order_date()
        season = SEASON_BY_MONTH[order_date.month]
        for product in gen_basket(season):
            category, price = CATALOG[product]
            qty = random.choices([1, 2, 3], weights=[0.75, 0.20, 0.05])[0]
            name = _messy_variant(product) if random.random() < messy_rate else product
            rows.append({
                "order_id": order_id,
                "product_name": name,
                "category": category,
                "quantity": qty,
                "unit_price": price,
                "order_date": order_date.isoformat(),
            })
    return rows


CSV_COLUMNS = ["order_id", "product_name", "category", "quantity", "unit_price", "order_date"]


def write_csv(path, rows, columns):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in columns})


if __name__ == "__main__":
    all_rows = gen_transactions(N_ORDERS)

    with open("transactions.json", "w") as f:
        json.dump(all_rows, f)

    # sample upload: a smaller, separate slice of orders (different order_ids,
    # same generative process) so it reads like a real, independent export
    sample_rows = gen_transactions(600)
    for i, r in enumerate(sample_rows):
        pass  # order_ids already unique within this batch (ORD00001.. up to 600)
    write_csv("sample_data/sample_transactions.csv", sample_rows, CSV_COLUMNS)

    template_rows = [
        {"order_id": "ORD1001", "product_name": "Spaghetti", "category": "Pasta", "quantity": 1, "unit_price": 1.79, "order_date": "2025-03-14"},
        {"order_id": "ORD1001", "product_name": "Marinara Sauce", "category": "Pasta", "quantity": 2, "unit_price": 3.49, "order_date": "2025-03-14"},
        {"order_id": "ORD1002", "product_name": "Diapers", "category": "Baby", "quantity": 1, "unit_price": 19.99, "order_date": "2025-03-20"},
    ]
    write_csv("sample_data/template.csv", template_rows, CSV_COLUMNS)

    n_baskets = len(set(r["order_id"] for r in all_rows))
    avg_basket = len(all_rows) / n_baskets
    print(f"Wrote {n_baskets} orders / {len(all_rows)} line items ({avg_basket:.2f} items/order avg)")
    print(f"Sample file: {len(set(r['order_id'] for r in sample_rows))} orders / {len(sample_rows)} line items")
