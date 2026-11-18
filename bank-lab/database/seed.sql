INSERT INTO customers(customer_id, display_name) VALUES
    ('c001', '가상 고객 Alice'), ('c002', '가상 고객 Bob')
ON CONFLICT DO NOTHING;
INSERT INTO accounts(account_id, customer_id, balance_cents) VALUES
    ('A001', 'c001', 1000000), ('A002', 'c002', 1000000)
ON CONFLICT DO NOTHING;
INSERT INTO employee_roles(username, role) VALUES
    ('teller', 'teller'), ('auditor', 'auditor'), ('operator', 'operator')
ON CONFLICT DO NOTHING;
