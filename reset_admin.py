import os
from app import app
from models import db, User
from werkzeug.security import generate_password_hash
from dotenv import load_dotenv

load_dotenv()

def reset_admin():
    with app.app_context():
        email = os.environ.get('ADMIN_EMAIL', 'admin@zuhraan.com')
        password = os.environ.get('ADMIN_PASSWORD', 'admin123')
        
        print(f"Targeting Admin: {email}")
        
        # Look for existing admin
        admin = User.query.filter_by(email=email).first()
        
        if admin:
            print("Found existing admin user. Updating password...")
            admin.password = generate_password_hash(password, method='pbkdf2:sha256')
            admin.role = 'admin' # Ensure role is correct
        else:
            print("Admin user not found. Creating new administrator...")
            admin = User(
                email=email,
                password=generate_password_hash(password, method='pbkdf2:sha256'),
                role='admin',
                name='Super Admin'
            )
            db.session.add(admin)
            
        db.session.commit()
        print("Admin credentials have been synchronized with your .env file!")
        print(f"You can now login with: {email} / {password}")

if __name__ == '__main__':
    reset_admin()
