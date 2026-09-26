import unittest

from app.sql.validator import clean_sql, contains_write_statement, strict_identifiers, validate_sql
from tests.helpers import sample_db, sample_schema


def check(sql, strict=True):
    return validate_sql(sql, sample_db(), sample_schema(), strict_relationships=strict)


class ReadOnlyTests(unittest.TestCase):
    def test_brief_example_is_valid(self):
        r = check("SELECT * FROM Employees WHERE HireDate >= '2024-01-01'")
        self.assertTrue(r.ok, r.errors)
        self.assertEqual(r.tables, ["Employees"])

    def test_every_destructive_operation_is_rejected(self):
        statements = [
            "DELETE FROM Employees",
            "UPDATE Employees SET Salary = 0",
            "INSERT INTO Departments (Name, Location, Budget) VALUES ('x', 'y', 1)",
            "DROP TABLE Customers",
            "ALTER TABLE Orders ADD COLUMN x TEXT",
            "TRUNCATE TABLE Orders",
            "CREATE TABLE t (id INTEGER)",
            "REPLACE INTO Categories VALUES (1, 'a', 'b')",
            "PRAGMA table_info(Employees)",
            "ATTACH DATABASE 'x.db' AS x",
        ]
        for sql in statements:
            with self.subTest(sql=sql):
                r = check(sql)
                self.assertFalse(r.ok)
                self.assertTrue(r.destructive)

    def test_write_hidden_after_select_or_in_cte_is_rejected(self):
        for sql in ["SELECT 1; DROP TABLE Customers",
                    "WITH x AS (SELECT 1) DELETE FROM Customers",
                    "SELECT * FROM Customers /* harmless */ ; DELETE FROM Customers"]:
            with self.subTest(sql=sql):
                self.assertFalse(check(sql).ok)

    def test_keywords_inside_strings_and_comments_are_not_writes(self):
        self.assertFalse(contains_write_statement("SELECT 'drop table x' AS note -- delete me"))
        r = check("SELECT FirstName FROM Employees WHERE JobTitle = 'update me' -- DROP")
        self.assertTrue(r.ok, r.errors)

    def test_replace_function_is_allowed(self):
        self.assertTrue(check("SELECT replace(Email, '@', ' at ') FROM Employees").ok)

    def test_internal_tables_and_extensions_are_blocked(self):
        self.assertFalse(check("SELECT * FROM sqlite_master").ok)
        self.assertFalse(check("SELECT load_extension('evil')").ok)

    def test_multiple_statements_rejected(self):
        r = check("SELECT 1; SELECT 2")
        self.assertFalse(r.ok)
        self.assertIn("single SQL statement", r.errors[0])


class SchemaTests(unittest.TestCase):
    def test_unknown_table_with_suggestion(self):
        r = check("SELECT * FROM Employes")
        self.assertFalse(r.ok)
        self.assertIn("Employes", r.errors[0])
        self.assertIn("Did you mean Employees", r.errors[0])

    def test_unknown_column_with_suggestion(self):
        r = check("SELECT FirstName, Salry FROM Employees")
        self.assertFalse(r.ok)
        self.assertIn("Salary", r.errors[0])

    def test_double_quoted_unknown_column_is_not_a_string(self):
        # SQLite would normally treat "Salry" as the string 'Salry'.
        self.assertFalse(check('SELECT "Salry" FROM Employees').ok)

    def test_double_quoted_real_names_and_aliases_are_valid(self):
        r = check('SELECT "e"."Salary" AS "Pay" FROM "Employees" "e" ORDER BY "Pay"')
        self.assertTrue(r.ok, r.errors)

    def test_strict_identifiers_leaves_strings_and_comments_alone(self):
        sql = '''SELECT "Salary" /* "x" */ FROM Employees WHERE FirstName = '"Bob"' '''
        self.assertEqual(strict_identifiers(sql),
                         '''SELECT `Salary` /* "x" */ FROM Employees WHERE FirstName = '"Bob"' ''')

    def test_ambiguous_column(self):
        r = check("SELECT DepartmentId FROM Employees e JOIN Departments d ON d.DepartmentId = e.DepartmentId")
        self.assertFalse(r.ok)
        self.assertIn("ambiguous", r.errors[0])

    def test_syntax_error(self):
        r = check("SELECT FirstName FROM Employees WHERE")
        self.assertFalse(r.ok)
        self.assertIn("syntax", r.errors[0].lower())

    def test_must_start_with_select(self):
        self.assertFalse(check("VALUES (1)").ok)

    def test_cte_and_subquery_are_valid(self):
        sql = ("WITH totals AS (SELECT CustomerId, SUM(TotalAmount) AS spent FROM Orders GROUP BY CustomerId) "
               "SELECT c.FirstName, t.spent FROM Customers c JOIN totals t ON t.CustomerId = c.CustomerId "
               "WHERE t.spent > (SELECT AVG(TotalAmount) FROM Orders)")
        self.assertTrue(check(sql).ok, check(sql).errors)

    def test_columns_are_reported(self):
        r = check("SELECT e.FirstName, d.Name FROM Employees e JOIN Departments d ON d.DepartmentId = e.DepartmentId")
        self.assertIn("Employees.FirstName", r.columns)
        self.assertIn("Departments.Name", r.columns)


class RelationshipTests(unittest.TestCase):
    def test_declared_foreign_key_join_is_valid(self):
        r = check("SELECT c.FirstName, o.OrderId FROM Orders o JOIN Customers c ON c.CustomerId = o.CustomerId")
        self.assertTrue(r.ok, r.errors)

    def test_self_join_along_manager_fk_is_valid(self):
        r = check("SELECT e.FirstName, m.FirstName FROM Employees e LEFT JOIN Employees m ON m.EmployeeId = e.ManagerId")
        self.assertTrue(r.ok, r.errors)

    def test_join_on_unrelated_columns_is_rejected_in_strict_mode(self):
        sql = "SELECT c.FirstName FROM Customers c JOIN Orders o ON o.EmployeeId = c.CustomerId"
        strict = check(sql, strict=True)
        self.assertFalse(strict.ok)
        self.assertIn("declared relationship", strict.errors[0])
        lenient = check(sql, strict=False)
        self.assertTrue(lenient.ok)
        self.assertTrue(lenient.warnings)

    def test_join_without_condition_warns(self):
        r = check("SELECT e.FirstName FROM Employees e JOIN Departments d")
        self.assertTrue(any("cartesian" in w for w in r.warnings))


class CleanSqlTests(unittest.TestCase):
    def test_strips_fences_and_semicolons(self):
        self.assertEqual(clean_sql("```sql\nSELECT 1;\n```"), "SELECT 1")
        self.assertEqual(clean_sql("  SELECT 1 ;; "), "SELECT 1")


if __name__ == "__main__":
    unittest.main()
