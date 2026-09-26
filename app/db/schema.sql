-- Sample schema for the SQL Query AI Agent (SQLite dialect).
-- A small retail company: departments, employees, customers, a product
-- catalogue and orders. All dates are ISO-8601 TEXT ('YYYY-MM-DD').
--
-- Foreign keys are declared explicitly: the agent's validator uses them to
-- check that JOIN conditions follow real relationships.
-- Only the indexes SQLite creates for primary keys exist, plus one on
-- OrderItems(OrderId). This is deliberate, so the optimizer has realistic
-- index recommendations to make.

PRAGMA foreign_keys = ON;

CREATE TABLE Departments (
    DepartmentId  INTEGER PRIMARY KEY,
    Name          TEXT    NOT NULL UNIQUE,
    Location      TEXT    NOT NULL,
    Budget        REAL    NOT NULL
);

CREATE TABLE Employees (
    EmployeeId    INTEGER PRIMARY KEY,
    FirstName     TEXT    NOT NULL,
    LastName      TEXT    NOT NULL,
    Email         TEXT    NOT NULL UNIQUE,
    JobTitle      TEXT    NOT NULL,
    DepartmentId  INTEGER NOT NULL REFERENCES Departments(DepartmentId),
    ManagerId     INTEGER REFERENCES Employees(EmployeeId),
    HireDate      TEXT    NOT NULL,          -- 'YYYY-MM-DD'
    Salary        REAL    NOT NULL,
    IsActive      INTEGER NOT NULL DEFAULT 1 -- 1 = active, 0 = left the company
);

CREATE TABLE Customers (
    CustomerId        INTEGER PRIMARY KEY,
    FirstName         TEXT    NOT NULL,
    LastName          TEXT    NOT NULL,
    Email             TEXT    NOT NULL UNIQUE,
    Phone             TEXT,
    City              TEXT    NOT NULL,
    State             TEXT    NOT NULL,      -- two-letter US state code, e.g. 'CA'
    Country           TEXT    NOT NULL DEFAULT 'USA',
    Segment           TEXT    NOT NULL,      -- 'Consumer', 'Small Business', 'Enterprise'
    CreatedAt         TEXT    NOT NULL,      -- 'YYYY-MM-DD'
    AccountManagerId  INTEGER REFERENCES Employees(EmployeeId)
);

CREATE TABLE Categories (
    CategoryId    INTEGER PRIMARY KEY,
    Name          TEXT    NOT NULL UNIQUE,
    Description   TEXT
);

CREATE TABLE Products (
    ProductId     INTEGER PRIMARY KEY,
    Name          TEXT    NOT NULL,
    CategoryId    INTEGER NOT NULL REFERENCES Categories(CategoryId),
    UnitPrice     REAL    NOT NULL,
    UnitsInStock  INTEGER NOT NULL,
    Discontinued  INTEGER NOT NULL DEFAULT 0 -- 1 = no longer sold
);

CREATE TABLE Orders (
    OrderId       INTEGER PRIMARY KEY,
    CustomerId    INTEGER NOT NULL REFERENCES Customers(CustomerId),
    EmployeeId    INTEGER REFERENCES Employees(EmployeeId), -- sales rep
    OrderDate     TEXT    NOT NULL,          -- 'YYYY-MM-DD'
    Status        TEXT    NOT NULL,          -- 'Pending', 'Shipped', 'Delivered', 'Cancelled'
    ShipCity      TEXT    NOT NULL,
    ShipState     TEXT    NOT NULL,
    TotalAmount   REAL    NOT NULL
);

CREATE TABLE OrderItems (
    OrderItemId   INTEGER PRIMARY KEY,
    OrderId       INTEGER NOT NULL REFERENCES Orders(OrderId),
    ProductId     INTEGER NOT NULL REFERENCES Products(ProductId),
    Quantity      INTEGER NOT NULL,
    UnitPrice     REAL    NOT NULL,
    Discount      REAL    NOT NULL DEFAULT 0 -- fraction, 0.10 = 10% off
);

CREATE INDEX idx_orderitems_orderid ON OrderItems(OrderId);
