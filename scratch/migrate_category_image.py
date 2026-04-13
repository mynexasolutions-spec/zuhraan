import sqlite3

def add_category_image_col():
    conn = sqlite3.connect('instance/zuhraan_v2.db')
    c = conn.cursor()
    try:
        c.execute("ALTER TABLE category ADD COLUMN image VARCHAR(255);")
        print("Successfully added image column to category table.")
    except sqlite3.OperationalError as e:
        print("Column might already exist or error:", e)
    conn.commit()
    conn.close()

if __name__ == '__main__':
    add_category_image_col()
