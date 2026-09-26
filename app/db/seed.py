"""Create and populate the sample SQLite database.

Data is generated from a fixed random seed, so every build of the database is
identical. That keeps demo answers and tests reproducible.

Usage:  python -m app.db.seed [path]   (default: data/sample.db)
"""
from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCHEMA_FILE = Path(__file__).with_name("schema.sql")

FIRST_NAMES = [
    "Aarav", "Olivia", "Liam", "Emma", "Noah", "Ava", "Mia", "Ethan", "Sofia",
    "Lucas", "Isabella", "Mason", "Priya", "James", "Chloe", "Daniel", "Zara",
    "Henry", "Grace", "Leo", "Nora", "Samuel", "Maya", "Owen", "Hannah", "Ravi",
    "Elena", "Jack", "Aisha", "Wyatt",
]
LAST_NAMES = [
    "Smith", "Johnson", "Patel", "Garcia", "Brown", "Nguyen", "Lee", "Martinez",
    "Davis", "Wilson", "Anderson", "Thomas", "Kim", "Clark", "Lopez", "Walker",
    "Young", "Hall", "Shah", "Rivera", "Wright", "Scott", "Green", "Baker",
]
DEPARTMENTS = [
    ("Sales", "New York", 1_200_000), ("Engineering", "San Francisco", 2_500_000),
    ("Marketing", "Chicago", 800_000), ("Finance", "New York", 600_000),
    ("Customer Support", "Austin", 500_000), ("Human Resources", "Chicago", 400_000),
]
TITLES = {
    "Sales": ["Account Executive", "Sales Representative", "Sales Manager"],
    "Engineering": ["Software Engineer", "Senior Software Engineer", "Engineering Manager"],
    "Marketing": ["Marketing Specialist", "Content Strategist", "Marketing Manager"],
    "Finance": ["Financial Analyst", "Accountant", "Finance Manager"],
    "Customer Support": ["Support Specialist", "Support Lead", "Support Manager"],
    "Human Resources": ["HR Generalist", "Recruiter", "HR Manager"],
}
BASE_SALARY = {"Sales": 70_000, "Engineering": 110_000, "Marketing": 72_000,
               "Finance": 85_000, "Customer Support": 55_000, "Human Resources": 65_000}
CITIES = [
    ("Los Angeles", "CA"), ("San Francisco", "CA"), ("San Diego", "CA"),
    ("San Jose", "CA"), ("New York", "NY"), ("Buffalo", "NY"), ("Austin", "TX"),
    ("Houston", "TX"), ("Dallas", "TX"), ("Seattle", "WA"), ("Chicago", "IL"),
    ("Miami", "FL"), ("Orlando", "FL"), ("Boston", "MA"), ("Denver", "CO"),
]
SEGMENTS = ["Consumer", "Consumer", "Consumer", "Small Business", "Enterprise"]
CATEGORIES = [
    ("Laptops", "Portable computers"), ("Monitors", "Displays and screens"),
    ("Accessories", "Keyboards, mice and cables"), ("Audio", "Headphones and speakers"),
    ("Storage", "Drives and memory cards"), ("Networking", "Routers and switches"),
]
PRODUCTS = {
    "Laptops": [("UltraBook 13", 1199), ("ProBook 15", 1599), ("Student Laptop 14", 649),
                ("Gaming Laptop 17", 2199), ("Chromebook 11", 299)],
    "Monitors": [("27in 4K Monitor", 429), ("24in Office Monitor", 179),
                 ("34in Ultrawide", 649), ("Portable Monitor 15", 229)],
    "Accessories": [("Wireless Mouse", 29), ("Mechanical Keyboard", 119),
                    ("USB-C Hub", 59), ("Laptop Stand", 45), ("HDMI Cable 2m", 12),
                    ("Webcam 1080p", 79)],
    "Audio": [("Noise-Cancelling Headphones", 299), ("Wireless Earbuds", 149),
              ("Bluetooth Speaker", 89), ("USB Microphone", 119)],
    "Storage": [("1TB SSD", 99), ("2TB External HDD", 79), ("128GB Flash Drive", 19),
                ("256GB SD Card", 35), ("4TB NAS Drive", 139)],
    "Networking": [("Wi-Fi 6 Router", 189), ("Mesh Wi-Fi Kit", 329),
                   ("8-Port Switch", 39), ("Powerline Adapter", 69)],
}
STATUSES = ["Delivered"] * 6 + ["Shipped"] * 2 + ["Pending", "Cancelled"]


def _rand_date(rng: random.Random, start: date, end: date) -> str:
    return (start + timedelta(days=rng.randint(0, (end - start).days))).isoformat()


def build_database(path: str | Path) -> Path:
    """(Re)create the sample database at ``path`` and return the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()

    rng = random.Random(42)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA_FILE.read_text())

        conn.executemany(
            "INSERT INTO Departments (DepartmentId, Name, Location, Budget) VALUES (?, ?, ?, ?)",
            [(i + 1, n, loc, b) for i, (n, loc, b) in enumerate(DEPARTMENTS)],
        )

        # Employees: one manager per department first, then staff reporting to them.
        employees, emp_id, used_emails = [], 1, set()
        managers: dict[int, int] = {}

        def make_email(first: str, last: str, domain: str) -> str:
            base, n = f"{first}.{last}".lower(), 1
            email = f"{base}@{domain}"
            while email in used_emails:
                n += 1
                email = f"{base}{n}@{domain}"
            used_emails.add(email)
            return email

        for dept_id, (dept, _, _) in enumerate(DEPARTMENTS, start=1):
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            hire = _rand_date(rng, date(2019, 1, 1), date(2021, 12, 31))
            salary = round(BASE_SALARY[dept] * 1.6 + rng.randint(0, 20) * 1000, -3)
            employees.append((emp_id, first, last, make_email(first, last, "company.com"),
                              TITLES[dept][2], dept_id, None, hire, salary, 1))
            managers[dept_id] = emp_id
            emp_id += 1
        while emp_id <= 60:
            dept_id = rng.randint(1, len(DEPARTMENTS))
            dept = DEPARTMENTS[dept_id - 1][0]
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            hire = _rand_date(rng, date(2020, 1, 1), date(2025, 6, 30))
            senior = rng.random() < 0.3
            salary = round(BASE_SALARY[dept] * (1.25 if senior else 1.0) + rng.randint(-8, 15) * 1000, -3)
            active = 0 if rng.random() < 0.1 else 1
            employees.append((emp_id, first, last, make_email(first, last, "company.com"),
                              TITLES[dept][1 if senior else 0], dept_id, managers[dept_id],
                              hire, salary, active))
            emp_id += 1
        conn.executemany("INSERT INTO Employees VALUES (?,?,?,?,?,?,?,?,?,?)", employees)
        sales_reps = [e[0] for e in employees if e[5] == 1]

        customers = []
        for cid in range(1, 201):
            first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
            city, state = rng.choice(CITIES)
            phone = f"+1-{rng.randint(200, 989)}-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"
            if rng.random() < 0.08:
                phone = None
            customers.append((cid, first, last, make_email(first, last, "example.com"), phone,
                              city, state, "USA", rng.choice(SEGMENTS),
                              _rand_date(rng, date(2021, 1, 1), date(2025, 3, 31)),
                              rng.choice(sales_reps)))
        conn.executemany("INSERT INTO Customers VALUES (?,?,?,?,?,?,?,?,?,?,?)", customers)

        conn.executemany("INSERT INTO Categories VALUES (?,?,?)",
                         [(i + 1, n, d) for i, (n, d) in enumerate(CATEGORIES)])
        products, pid = [], 1
        for cat_id, (cat, _) in enumerate(CATEGORIES, start=1):
            for name, price in PRODUCTS[cat]:
                products.append((pid, name, cat_id, float(price), rng.randint(0, 250),
                                 1 if rng.random() < 0.1 else 0))
                pid += 1
        conn.executemany("INSERT INTO Products VALUES (?,?,?,?,?,?)", products)
        price_of = {p[0]: p[3] for p in products}

        orders, items, item_id = [], [], 1
        for oid in range(1, 1501):
            cust = customers[rng.randint(0, len(customers) - 1)]
            order_date = _rand_date(rng, date(2023, 1, 1), date(2025, 6, 30))
            status = rng.choice(STATUSES)
            total = 0.0
            for product_id in rng.sample(list(price_of), rng.randint(1, 4)):
                qty = rng.randint(1, 5)
                discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15])
                total += qty * price_of[product_id] * (1 - discount)
                items.append((item_id, oid, product_id, qty, price_of[product_id], discount))
                item_id += 1
            orders.append((oid, cust[0], cust[10], order_date, status, cust[5], cust[6],
                           round(total, 2)))
        conn.executemany("INSERT INTO Orders VALUES (?,?,?,?,?,?,?,?)", orders)
        conn.executemany("INSERT INTO OrderItems VALUES (?,?,?,?,?,?)", items)
        conn.commit()
    finally:
        conn.close()
    return path


def ensure_database(path: str | Path) -> Path:
    """Build the sample database only if it does not exist yet."""
    path = Path(path)
    if not path.exists():
        build_database(path)
    return path


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "data/sample.db"
    print(f"Created {build_database(target)}")
