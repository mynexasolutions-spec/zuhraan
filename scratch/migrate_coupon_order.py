import sqlite3

def add_coupon_col():
    conn = sqlite3.connect('instance/zuhraan_v2.db') # Check where db is, usually instance folder
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE `order` ADD COLUMN coupon_id INTEGER;")
        print("Successfully added coupon_id column.")
    except sqlite3.OperationalError as e:
        print("Column might already exist or error:", e)
    conn.commit()
    conn.close()

if __name__ == '__main__':
    add_coupon_col()
