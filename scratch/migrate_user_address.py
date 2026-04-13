import sqlite3

def add_user_address_cols():
    conn = sqlite3.connect('instance/zuhraan_v2.db')
    c = conn.cursor()
    columns = [
        "phone VARCHAR(20)",
        "address_line1 VARCHAR(255)",
        "address_line2 VARCHAR(255)",
        "city VARCHAR(100)",
        "state VARCHAR(100)",
        "pincode VARCHAR(20)",
        "country VARCHAR(100) DEFAULT 'India'"
    ]
    for col in columns:
        try:
            c.execute(f"ALTER TABLE user ADD COLUMN {col};")
            print(f"Added {col}")
        except sqlite3.OperationalError as e:
            print(f"Skipped {col}: {e}")
    conn.commit()
    conn.close()

if __name__ == '__main__':
    add_user_address_cols()
