"""Schema introspection, optimizer, formatter and executor."""
import sqlite3
import unittest

from app.sql.executor import execute_query
from app.sql.formatter import format_sql
from app.sql.optimizer import analyze_performance
from tests.helpers import sample_db, sample_schema


class SchemaIntrospectionTests(unittest.TestCase):
    def test_tables_and_relationships(self):
        schema = sample_schema()
        self.assertEqual(set(schema.table_names()), {"Departments", "Employees", "Customers", "Categories",
                                                     "Products", "Orders", "OrderItems"})
        self.assertIn(frozenset({("orders", "customerid"), ("customers", "customerid")}),
                      schema.relationship_pairs())

    def test_prompt_contains_values_notes_and_relationships(self):
        prompt = sample_schema().to_prompt()
        self.assertIn("'CA'", prompt)                         # sample values for State
        self.assertIn("YYYY-MM-DD", prompt)                   # column note from schema.sql
        self.assertIn("Orders.CustomerId -> Customers.CustomerId", prompt)

    def test_case_insensitive_lookup(self):
        self.assertTrue(sample_schema().has_column("employees", "hiredate"))
        self.assertFalse(sample_schema().has_column("Employees", "BirthDate"))

    def test_relevant_tables_returns_all_for_small_schema(self):
        self.assertEqual(len(sample_schema().relevant_tables("employees")), 7)


class OptimizerTests(unittest.TestCase):
    def analyze(self, sql):
        return analyze_performance(sql, sample_db(), sample_schema())

    def test_select_star_suggestion(self):
        opt = self.analyze("SELECT * FROM Employees")
        self.assertTrue(any("SELECT *" in s for s in opt.suggestions))

    def test_index_recommended_for_filtered_scan(self):
        opt = self.analyze("SELECT OrderId FROM Orders WHERE OrderDate >= '2024-01-01'")
        self.assertIn("CREATE INDEX idx_orders_orderdate ON Orders(OrderDate);", opt.index_recommendations)

    def test_foreign_key_index_recommended_when_filter_is_on_other_side(self):
        opt = self.analyze("SELECT c.FirstName, o.OrderId FROM Customers c "
                           "JOIN Orders o ON o.CustomerId = c.CustomerId WHERE c.State = 'CA'")
        self.assertIn("CREATE INDEX idx_orders_customerid ON Orders(CustomerId);", opt.index_recommendations)

    def test_no_index_for_small_tables_or_primary_keys(self):
        self.assertEqual(self.analyze("SELECT Name FROM Departments WHERE Location = 'Austin'").index_recommendations, [])
        self.assertEqual(self.analyze("SELECT * FROM Orders WHERE OrderId = 5").index_recommendations, [])

    def test_unused_join_detected(self):
        opt = self.analyze("SELECT o.OrderId, o.TotalAmount FROM Orders o "
                           "LEFT JOIN Employees e ON e.EmployeeId = o.EmployeeId")
        self.assertTrue(any("join to Employees is not used" in s for s in opt.suggestions))

    def test_used_join_not_flagged(self):
        opt = self.analyze("SELECT o.OrderId, e.FirstName FROM Orders o "
                           "JOIN Employees e ON e.EmployeeId = o.EmployeeId")
        self.assertFalse(any("not used" in s for s in opt.suggestions))

    def test_function_on_filtered_column_flagged(self):
        opt = self.analyze("SELECT OrderId FROM Orders WHERE strftime('%Y', OrderDate) = '2024'")
        self.assertTrue(any("STRFTIME" in s and "OrderDate" in s for s in opt.suggestions))

    def test_cost_estimate(self):
        small = self.analyze("SELECT * FROM Orders WHERE OrderId = 1").cost
        big = self.analyze("SELECT o.OrderId FROM Orders o JOIN OrderItems i ON i.OrderId = o.OrderId").cost
        self.assertEqual(small["level"], "low")
        self.assertGreater(big["estimated_rows_examined"], small["estimated_rows_examined"])


class FormatterTests(unittest.TestCase):
    def test_clauses_on_separate_lines(self):
        out = format_sql("select a, b from t join u on u.id = t.id where x = 1 and y between 1 and 2 order by a")
        self.assertEqual(out.splitlines(), [
            "SELECT a, b", "FROM t", "JOIN u ON u.id = t.id", "WHERE x = 1",
            "  AND y BETWEEN 1 AND 2", "ORDER BY a"])

    def test_formatting_preserves_results(self):
        sql = ("select c.State, count(*) n from Customers c where c.Segment in ('Consumer','Enterprise') "
               "group by c.State having count(*) > 3 order by n desc")
        conn = sqlite3.connect(sample_db())
        self.assertEqual(conn.execute(sql).fetchall(), conn.execute(format_sql(sql)).fetchall())
        conn.close()


class ExecutorTests(unittest.TestCase):
    def test_runs_and_limits_rows(self):
        r = execute_query("SELECT OrderId FROM Orders", sample_db(), max_rows=10)
        self.assertEqual(r.row_count, 10)
        self.assertTrue(r.truncated)
        self.assertEqual(r.columns, ["OrderId"])

    def test_brief_example_result(self):
        r = execute_query("SELECT * FROM Employees WHERE HireDate >= '2024-01-01'", sample_db())
        self.assertEqual(r.error, "")
        self.assertGreater(r.row_count, 0)
        hire_idx = r.columns.index("HireDate")
        self.assertTrue(all(row[hire_idx] >= "2024-01-01" for row in r.rows))

    def test_timeout_stops_runaway_query(self):
        r = execute_query("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) "
                          "SELECT count(*) FROM c", sample_db(), timeout_seconds=0.3)
        self.assertIn("stopped", r.error)

    def test_writes_blocked_even_if_validation_is_bypassed(self):
        r = execute_query("DELETE FROM Orders", sample_db())
        self.assertTrue(r.error)
        conn = sqlite3.connect(sample_db())
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM Orders").fetchone()[0], 1500)
        conn.close()


if __name__ == "__main__":
    unittest.main()
