import os
from dotenv import load_dotenv
load_dotenv() # Load variables from .env

from flask import Flask, session
from flask_wtf.csrf import CSRFProtect
import bleach

from models import db, Category, User, Setting
from routes.main import main_bp
from routes.admin import admin_bp
from flask_login import LoginManager
from werkzeug.security import generate_password_hash
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
csrf = CSRFProtect(app)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default_secret_key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///zuhraan_v2.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

app.config['RAZORPAY_KEY_ID'] = os.environ.get('RAZORPAY_KEY_ID')
app.config['RAZORPAY_KEY_SECRET'] = os.environ.get('RAZORPAY_KEY_SECRET')
app.config['RAZORPAY_WEBHOOK_SECRET'] = os.environ.get('RAZORPAY_WEBHOOK_SECRET')

# Cloudinary Config
cloudinary.config(
    cloudinary_url=os.environ.get('CLOUDINARY_URL')
)

@app.context_processor
def utility_processor():
    def get_image_url(image_path):
        if not image_path: return "/static/images/banner/zuhran_2.webp"
        if image_path.startswith('http'): return image_path
        # For legacy relative paths we attach /static/ manually or via url_for
        if not image_path.startswith('/'):
            return f"/static/{image_path}"
        return image_path
    return dict(get_image_url=get_image_url)

# Initialize DB
db.init_app(app)

# Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'main.login'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Context Processors and Filters
@app.context_processor
def inject_cart_count():
    cart = session.get('cart', {})
    total_items = sum(cart.values()) if cart else 0
    return dict(cart_count=total_items)

@app.template_filter('clean_html')
def clean_html(text):
    if not text:
        return ""
    allowed_tags = ['b', 'i', 'strong', 'em', 'p', 'br', 'ul', 'li', 'ol', 'h3', 'h4', 'span', 'div']
    return bleach.clean(text, tags=allowed_tags, attributes={'*': ['class', 'style']}, strip=True)

# Register Blueprints
app.register_blueprint(main_bp)
app.register_blueprint(admin_bp)

# Exempt webhook route and AJAX API routes from CSRF
csrf.exempt("routes.main.payment_webhook")
csrf.exempt("routes.main.api_validate_coupon")
csrf.exempt("routes.admin.validate_coupon_api")

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
    admin_email = os.environ.get('ADMIN_EMAIL', 'admin@zuhraan.com')
    admin_password = os.environ.get('ADMIN_PASSWORD', 'admin123')
    
    admin = User.query.filter_by(role='admin').first()
    if not admin:
        new_admin = User(
            email=admin_email,
            password=generate_password_hash(admin_password, method='pbkdf2:sha256'),
            role='admin'
        )
        db.session.add(new_admin)
        
    # Seed Settings
    settings = {
        'shipping_charge': '0', 
        'razorpay_key': 'rzp_test_RbJeXJAhskSAHd', 
        'razorpay_secret': '0Z7vD3Oy3QliqQqW82jw1yML',
        'payment_cod_enabled': '0',
        'payment_online_enabled': '1'
    }
    for k, v in settings.items():
        if not Setting.query.filter_by(key=k).first():
            db.session.add(Setting(key=k, value=v))
        else:
            # Update existing if needed
            s = Setting.query.filter_by(key=k).first()
            s.value = v
            
    db.session.commit()
    print('Database initialized with default categories and admin user (admin@zuhraan.com / admin123)')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(port=5005, host='0.0.0.0', debug=True) 
