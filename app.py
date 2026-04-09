import os
from flask import Flask, session
from models import db, Category, User, Setting
from routes.main import main_bp
from routes.admin import admin_bp
from flask_login import LoginManager
from werkzeug.security import generate_password_hash

app = Flask(__name__)
app.config['SECRET_KEY'] = 'zuhraan_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///zuhraan_v2.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Initialize DB
db.init_app(app)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'main.login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Context Processors
@app.context_processor
def inject_cart_count():
    cart = session.get('cart', {})
    total_items = sum(cart.values()) if cart else 0
    return dict(cart_count=total_items)

# Register Blueprints
app.register_blueprint(main_bp)
app.register_blueprint(admin_bp)

# CREATE DB CLI COMMAND
@app.cli.command('init-db')
def seed_db():
    db.create_all()
    
    # Seed Categories
    cats = ['Eau De Parfum', 'Extrait De Parfum', 'Premium Collection', 'Room Freshener', 'Best sellers']
    for cat_name in cats:
        if not Category.query.filter_by(name=cat_name).first():
            db.session.add(Category(name=cat_name))
            
    # Seed Admin User
    admin = User.query.filter_by(role='admin').first()
    if not admin:
        new_admin = User(
            email='admin@zuhraan.com',
            password=generate_password_hash('admin123', method='pbkdf2:sha256'),
            role='admin'
        )
        db.session.add(new_admin)
        
    # Seed Settings
    settings = {'shipping_charge': '0', 'razorpay_key': '', 'razorpay_secret': ''}
    for k, v in settings.items():
        if not Setting.query.filter_by(key=k).first():
            db.session.add(Setting(key=k, value=v))
            
    db.session.commit()
    print('Database initialized with default categories and admin user (admin@zuhraan.com / admin123)')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(debug=True)
