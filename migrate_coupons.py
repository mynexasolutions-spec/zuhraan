import sqlite3, os

db_path = os.path.join('instance', 'zuhraan_v2.db')
conn = sqlite3.connect(db_path)
cur = conn.cursor()

# 1. Create coupon table
cur.execute('''
CREATE TABLE IF NOT EXISTS coupon (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code VARCHAR(50) UNIQUE NOT NULL,
    discount_type VARCHAR(10) NOT NULL DEFAULT 'percent',
    discount_value FLOAT NOT NULL,
    min_order_amount FLOAT DEFAULT 0.0,
    max_uses INTEGER,
    used_count INTEGER DEFAULT 0,
    is_active BOOLEAN DEFAULT 1,
    expires_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)
''')
print('coupon table: OK')

# 2. Seed new settings (payment toggles)
new_settings = {
    'payment_cod_enabled': '1',
    'payment_online_enabled': '0',
    'free_shipping_threshold': '0',
}
for key, default in new_settings.items():
    existing = cur.execute('SELECT id FROM setting WHERE key=?', (key,)).fetchone()
    if not existing:
        cur.execute('INSERT INTO setting (key, value) VALUES (?, ?)', (key, default))
        print(f'Setting added: {key} = {default}')
    else:
        print(f'Setting exists: {key}')

conn.commit()
conn.close()
print('\nMigration complete.')
