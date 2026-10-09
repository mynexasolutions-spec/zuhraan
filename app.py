import os
import json
import click
from dotenv import load_dotenv
load_dotenv() # Load variables from .env

from flask import Flask, session
from flask_wtf.csrf import CSRFProtect
import bleach
from sqlalchemy import inspect, text

from models import db, Category, User, Setting, AboutPage, ContactMessage, GoKwikCheckoutSession
from routes.main import main_bp
from routes.admin import admin_bp
from routes.gokwik import gokwik_api_bp, gokwik_storefront_bp
from flask_login import LoginManager
from werkzeug.security import generate_password_hash
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
csrf = CSRFProtect(app)


def get_supabase_database_url() -> str:
    database_url = os.environ.get('SUPABASE_DATABASE_URL')
    if not database_url:
        raise RuntimeError(
            'SUPABASE_DATABASE_URL is missing. In Supabase, open Connect, choose Session pooler, '
            'and paste the complete PostgreSQL connection string into .env.'
        )

    placeholder_markers = ('<', '>', '[your-password]', 'copied-pooler-host', 'project-ref')
    if any(marker in database_url.lower() for marker in placeholder_markers):
        raise RuntimeError(
            'SUPABASE_DATABASE_URL still contains a template placeholder. Copy the complete Session '
            'pooler URL from Supabase Connect; do not type the pooler host manually.'
        )

    return database_url


app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'default_secret_key')
app.config['SQLALCHEMY_DATABASE_URI'] = get_supabase_database_url()
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload size
app.config['SUPABASE_URL'] = os.environ.get('NEXT_PUBLIC_SUPABASE_URL')
app.config['SUPABASE_ANON_KEY'] = os.environ.get('NEXT_PUBLIC_SUPABASE_ANON_KEY')
app.config['SUPABASE_SERVICE_ROLE_KEY'] = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')

app.config['RAZORPAY_KEY_ID'] = os.environ.get('RAZORPAY_KEY_ID')
app.config['RAZORPAY_KEY_SECRET'] = os.environ.get('RAZORPAY_KEY_SECRET')
app.config['RAZORPAY_WEBHOOK_SECRET'] = os.environ.get('RAZORPAY_WEBHOOK_SECRET')

gokwik_environment = os.environ.get('GOKWIK_ENV', 'sandbox').strip().lower()
gokwik_sdk_urls = {
    'sandbox': 'https://sandbox.pdp.gokwik.co/v4/build/gokwik.js',
    'production': 'https://pdp.gokwik.co/v4/build/gokwik.js',
}
gokwik_api_base_urls = {
    'sandbox': 'https://api-gw-v4.dev.gokwik.io/sandbox/',
    'production': 'https://gkx.gokwik.co/',
}
if gokwik_environment not in gokwik_sdk_urls:
    raise RuntimeError('GOKWIK_ENV must be either sandbox or production.')

app.config['GOKWIK_ENABLED'] = os.environ.get('GOKWIK_ENABLED', '0') == '1'
app.config['GOKWIK_STOREFRONT_ENABLED'] = os.environ.get('GOKWIK_STOREFRONT_ENABLED', '0') == '1'
app.config['GOKWIK_ENV'] = gokwik_environment
app.config['GOKWIK_SDK_URL'] = gokwik_sdk_urls[gokwik_environment]
app.config['GOKWIK_API_BASE_URL'] = gokwik_api_base_urls[gokwik_environment]
app.config['GOKWIK_MERCHANT_ID'] = os.environ.get('GOKWIK_MERCHANT_ID')
app.config['GOKWIK_APP_ID'] = os.environ.get('GOKWIK_APP_ID')
app.config['GOKWIK_APP_SECRET'] = os.environ.get('GOKWIK_APP_SECRET')

if app.config['GOKWIK_STOREFRONT_ENABLED'] and not app.config['GOKWIK_ENABLED']:
    raise RuntimeError('GOKWIK_STOREFRONT_ENABLED requires GOKWIK_ENABLED=1.')

if app.config['GOKWIK_ENABLED']:
    required_gokwik_settings = (
        'GOKWIK_MERCHANT_ID',
        'GOKWIK_APP_ID',
        'GOKWIK_APP_SECRET',
    )
    missing_gokwik_settings = [
        name for name in required_gokwik_settings if not app.config.get(name)
    ]
    if missing_gokwik_settings:
        raise RuntimeError(
            'GoKwik is enabled but required configuration is missing: '
            + ', '.join(missing_gokwik_settings)
        )

app.config['BREVO_API_KEY'] = os.environ.get('BREVO_API_KEY')
app.config['BREVO_SENDER_EMAIL'] = os.environ.get('BREVO_SENDER_EMAIL')
app.config['BREVO_SENDER_NAME'] = os.environ.get('BREVO_SENDER_NAME', 'Zuhraan')

# Cloudinary Config
cloudinary.config(
    cloudinary_url=os.environ.get('CLOUDINARY_URL')
)

@app.context_processor
def utility_processor():
    def get_image_url(image_path, cache_bust=None):
        image_path = image_path.strip() if isinstance(image_path, str) else ''
        if not image_path:
            return "/static/images/banner/zuhran_2.webp"
        if image_path.startswith('http'):
            # Add cache-busting parameter for Cloudinary URLs
            if cache_bust:
                separator = '&' if '?' in image_path else '?'
                return f"{image_path}{separator}v={cache_bust}"
            return image_path
        # For legacy relative paths we attach /static/ manually or via url_for
        if not image_path.startswith('/'):
            return f"/static/{image_path}"
        return image_path
    return dict(get_image_url=get_image_url)

# Initialize DB
db.init_app(app)


def ensure_product_optional_columns():
    """Add optional product controls to databases created before these fields existed."""
    columns = {
        'show_top_notes': 'BOOLEAN NOT NULL DEFAULT TRUE',
        'show_middle_notes': 'BOOLEAN NOT NULL DEFAULT TRUE',
        'show_base_notes': 'BOOLEAN NOT NULL DEFAULT TRUE',
        'show_longevity': 'BOOLEAN NOT NULL DEFAULT TRUE',
        'show_projection': 'BOOLEAN NOT NULL DEFAULT TRUE',
        'variants_enabled': 'BOOLEAN NOT NULL DEFAULT TRUE',
    }
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table('product'):
            return
        existing_columns = {column['name'] for column in inspector.get_columns('product')}
        missing_columns = {
            name: definition
            for name, definition in columns.items()
            if name not in existing_columns
        }
        if not missing_columns:
            return
        try:
            for name, definition in missing_columns.items():
                db.session.execute(text(f'ALTER TABLE product ADD COLUMN {name} {definition}'))
            db.session.commit()
        except Exception:
            db.session.rollback()
            app.logger.exception('Failed to add optional product columns.')
            raise


ensure_product_optional_columns()


def ensure_gokwik_schema():
    """Create GoKwik persistence and extend legacy order tables safely."""
    order_columns = {
        'payment_method': 'VARCHAR(50)',
        'gokwik_transaction_id': 'VARCHAR(150)',
        'shipping_provider': 'VARCHAR(100)',
        'awb_number': 'VARCHAR(150)',
    }
    with app.app_context():
        inspector = inspect(db.engine)
        if inspector.has_table('order'):
            existing_columns = {column['name'] for column in inspector.get_columns('order')}
            missing_columns = {
                name: definition
                for name, definition in order_columns.items()
                if name not in existing_columns
            }
            try:
                for name, definition in missing_columns.items():
                    db.session.execute(text(f'ALTER TABLE "order" ADD COLUMN {name} {definition}'))
                db.session.commit()
            except Exception:
                db.session.rollback()
                app.logger.exception('Failed to add GoKwik order columns.')
                raise

        if inspector.has_table('order'):
            GoKwikCheckoutSession.__table__.create(bind=db.engine, checkfirst=True)


ensure_gokwik_schema()


@app.cli.command('gokwik-readiness')
def gokwik_readiness():
    """Verify local GoKwik configuration, schema, and authenticated routing."""
    checks = {
        'environment': app.config['GOKWIK_ENV'] in {'sandbox', 'production'},
        'merchant_id': bool(app.config.get('GOKWIK_MERCHANT_ID')),
        'app_id': bool(app.config.get('GOKWIK_APP_ID')),
        'app_secret': bool(app.config.get('GOKWIK_APP_SECRET')),
        'server_integration_enabled': bool(app.config.get('GOKWIK_ENABLED')),
    }

    inspector = inspect(db.engine)
    checks['checkout_session_table'] = inspector.has_table('go_kwik_checkout_session')
    if inspector.has_table('order'):
        order_columns = {column['name'] for column in inspector.get_columns('order')}
        checks['order_columns'] = {
            'payment_method',
            'gokwik_transaction_id',
            'shipping_provider',
            'awb_number',
        }.issubset(order_columns)
    else:
        checks['order_columns'] = False

    if app.config.get('GOKWIK_ENABLED'):
        response = app.test_client().get(
            '/api/gokwik/v1/cart/health-check',
            headers={
                'appid': app.config['GOKWIK_APP_ID'],
                'appsecret': app.config['GOKWIK_APP_SECRET'],
            },
        )
        checks['authenticated_health_route'] = response.status_code == 200
    else:
        checks['authenticated_health_route'] = False

    for name, passed in checks.items():
        click.echo(f"[{'PASS' if passed else 'FAIL'}] {name}")
    click.echo(
        '[INFO] storefront_enabled='
        + str(bool(app.config.get('GOKWIK_STOREFRONT_ENABLED')))
    )
    if not all(checks.values()):
        raise click.ClickException(
            'GoKwik is not ready. Resolve the failed checks; no credential values were printed.'
        )

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
app.register_blueprint(gokwik_storefront_bp)
app.register_blueprint(gokwik_api_bp)

# Exempt webhook route and AJAX API routes from CSRF
csrf.exempt("routes.main.payment_webhook")
csrf.exempt("routes.main.api_validate_coupon")
csrf.exempt("routes.admin.validate_coupon_api")
csrf.exempt(gokwik_api_bp)

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
        'free_shipping_threshold': '0',
        'payment_cod_enabled': '0',
        'payment_online_enabled': '1'
    }
    for k, v in settings.items():
        if not Setting.query.filter_by(key=k).first():
            db.session.add(Setting(key=k, value=v))

    # Razorpay credentials are intentionally managed only through environment
    # variables. Remove obsolete database values created by older releases.
    for setting in Setting.query.filter(Setting.key.in_({'razorpay_key', 'razorpay_secret'})).all():
        db.session.delete(setting)
            
    # Seed About Page
    if not AboutPage.query.first():
        default_about = AboutPage(
            page_title='About Zuhraan',
            intro_text='At Zuhraan, we believe a fragrance is more than just a scent — it is a part of your identity, your mood, and the memories you leave behind.',
            story_heading='Our Story',
            story_content='Born from a passion for refined fragrances, Zuhraan is created for those who appreciate depth, character, and individuality. Each fragrance is carefully crafted to offer a distinctive experience, bringing together thoughtfully selected notes to create scents that feel timeless and memorable.',
            collection_heading='Our Collection',
            collection_subheading='Diverse Fragrance Portfolio',
            collection_description='From rich oud and warm woods to sweet, fresh, and modern accords, our collection is made to suit different personalities and moments:',
            collection_items=json.dumps([
                'Eau De Parfum - Premium concentration for lasting elegance',
                'Extrait De Parfum - Pure fragrance essence for fragrance lovers',
                'Premium Collection - Exclusive limited-edition scents',
                'Room Freshener - Ambient luxury for your space',
                'Best Sellers - Customer favorites and popular scents'
            ]),
            commitment_heading='Our Commitment',
            commitment_content='We focus on creating fragrances that are enjoyable to wear, beautifully presented, and made to leave an impression. Zuhraan was built on a simple vision - to prepare every product with care and deliver it with confidence.',
            values_heading='Our Values',
            values_items=json.dumps([
                'Quality - Finest ingredients and masterful craftsmanship',
                'Authenticity - Genuine fragrances that tell a story',
                'Excellence - Premium presentation and customer experience',
                'Integrity - Transparent practices and ethical sourcing'
            ]),
            legacy_heading='Legacy',
            legacy_content='Zuhraan — Where fragrance makes a lasting impression. We invite you to discover the scent that matches your style and becomes part of your story.'
        )
        db.session.add(default_about)
            
    db.session.commit()
    print('Database initialized with default categories and admin user (admin@zuhraan.com / admin123)')


@app.cli.command('purge-legacy-razorpay-settings')
def purge_legacy_razorpay_settings():
    """Remove obsolete Razorpay credentials stored in the settings table."""
    deleted_count = (
        Setting.query
        .filter(Setting.key.in_({'razorpay_key', 'razorpay_secret'}))
        .delete(synchronize_session=False)
    )
    db.session.commit()
    click.echo(f'Removed {deleted_count} obsolete Razorpay setting(s).')

@app.cli.command('init-contact-messages')
def init_contact_messages():
    """Create the contact message table without changing existing store data."""
    ContactMessage.__table__.create(bind=db.engine, checkfirst=True)
    print('Contact message table is ready.')

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    app.run(port=5005, host='0.0.0.0', debug=True) 
