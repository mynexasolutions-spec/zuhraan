import os
from dotenv import load_dotenv
load_dotenv()

from app import app
from models import db, User
from werkzeug.security import generate_password_hash

def update_admin():
    with app.app_context():
        email = os.environ.get('ADMIN_EMAIL', 'admin@zuhraan.com')
        password = os.environ.get('ADMIN_PASSWORD', 'admin123')
        
        # Try to find existing admin
        admin = User.query.filter_by(role='admin').first()
        
        if admin:
            print(f"Updating existing admin: {admin.email}")
            admin.email = email
            admin.password = generate_password_hash(password, method='pbkdf2:sha256')
            db.session.commit()
            print("Admin credentials updated successfully.")
        else:
            print("No admin user found. Creating new admin.")
            new_admin = User(
                email=email,
                password=generate_password_hash(password, method='pbkdf2:sha256'),
                role='admin'
            )
            db.session.add(new_admin)
            db.session.commit()
            print("New admin user created.")

if __name__ == "__main__":
    update_admin()
