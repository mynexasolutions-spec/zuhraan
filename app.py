import os
import json
import logging
import click
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv
load_dotenv() # Load variables from .env

from flask import Flask, session
from flask_wtf.csrf import CSRFProtect
import bleach
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError
from waitress import serve
from werkzeug.middleware.proxy_fix import ProxyFix

from models import db, Category, User, Setting, AboutPage, ContactMessage, GoKwikCheckoutSession
from routes.main import main_bp
from routes.admin import admin_bp
from routes.gokwik import gokwik_api_bp, gokwik_storefront_bp
from routes.ithink import apply_ithink_tracking
from gokwik_client import (
    GoKwikRequestError,
    is_gokwik_payment_method,
    sync_gokwik_order,
    verify_gokwik_merchant,
)
from flask_login import LoginManager
from werkzeug.security import generate_password_hash
import cloudinary
import cloudinary.uploader
import cloudinary.api

app = Flask(__name__)
app.logger.setLevel(logging.INFO)
csrf = CSRFProtect(app)


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def is_production_runtime() -> bool:
    return os.environ.get('APP_ENV', 'development').strip().lower() == 'production'


def get_supabase_database_url() -> str:
    database_url = os.environ.get('SUPABASE_DATABASE_URL')
    if not database_url:
        raise RuntimeError(
            'SUPABASE_DATABASE_URL is missing. In Supabase, open Connect and copy the complete '
            'PostgreSQL connection string into the deployment environment.'
        )

    placeholder_markers = ('<', '>', '[your-password]', 'copied-pooler-host', 'project-ref')
    if any(marker in database_url.lower() for marker in placeholder_markers):
        raise RuntimeError(
            'SUPABASE_DATABASE_URL still contains a template placeholder. Copy the complete pooler '
            'URL from Supabase Connect; do not type the pooler host manually.'
        )

    is_postgres_url = database_url.lower().startswith(
        ('postgresql://', 'postgres://', 'postgresql+')
    )
    if is_postgres_url:
        parsed_url = urlparse(database_url)
        query_parameters = parse_qs(parsed_url.query)
        ssl_mode = query_parameters.get('sslmode', [''])[0].lower()
        if not ssl_mode:
            separator = '&' if parsed_url.query else '?'
            database_url = f'{database_url}{separator}sslmode=require'
            ssl_mode = 'require'
        if is_production_runtime() and ssl_mode not in {'require', 'verify-ca', 'verify-full'}:
            raise RuntimeError(
                'Production SUPABASE_DATABASE_URL must require SSL. Add sslmode=require '
                'or use verify-ca/verify-full.'
            )

    return database_url


def get_database_engine_options() -> dict[str, object]:
    return {
        'pool_pre_ping': True,
        'pool_recycle': 300,
    }


secret_key = os.environ.get('SECRET_KEY')
if is_production_runtime() and (not secret_key or len(secret_key) < 32):
    raise RuntimeError('Production SECRET_KEY must be set and contain at least 32 characters.')

app.config['SECRET_KEY'] = secret_key or 'default_secret_key'
app.config['SQLALCHEMY_DATABASE_URI'] = get_supabase_database_url()
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = get_database_engine_options()
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB max upload size
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE'] = environment_flag(
    'SESSION_COOKIE_SECURE',
    is_production_runtime(),
)
app.config['META_PIXEL_ID'] = os.environ.get(
    'META_PIXEL_ID',
    '2200260290560993',
).strip()
app.config['SUPABASE_URL'] = os.environ.get('NEXT_PUBLIC_SUPABASE_URL')
app.config['SUPABASE_ANON_KEY'] = os.environ.get('NEXT_PUBLIC_SUPABASE_ANON_KEY')
app.config['SUPABASE_SERVICE_ROLE_KEY'] = os.environ.get('SUPABASE_SERVICE_ROLE_KEY')
app.config['RAZORPAY_KEY_ID'] = os.environ.get('RAZORPAY_KEY_ID')
app.config['RAZORPAY_KEY_SECRET'] = os.environ.get('RAZORPAY_KEY_SECRET')
app.config['RAZORPAY_WEBHOOK_SECRET'] = os.environ.get('RAZORPAY_WEBHOOK_SECRET')
app.config['RAZORPAY_ENABLED'] = environment_flag(
    'RAZORPAY_ENABLED',
    bool(app.config['RAZORPAY_KEY_ID'] and app.config['RAZORPAY_KEY_SECRET']),
)

if app.config['RAZORPAY_ENABLED']:
    missing_razorpay_settings = [
        name for name in (
            'RAZORPAY_KEY_ID',
            'RAZORPAY_KEY_SECRET',
            'RAZORPAY_WEBHOOK_SECRET',
        ) if not app.config.get(name)
    ]
    if missing_razorpay_settings:
        raise RuntimeError(
            'Razorpay is enabled but required configuration is missing: '
            + ', '.join(missing_razorpay_settings)
        )

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

app.config['GOKWIK_ENABLED'] = environment_flag('GOKWIK_ENABLED', is_production_runtime())
app.config['GOKWIK_ENV'] = gokwik_environment
app.config['GOKWIK_PRODUCTION_APPROVED'] = environment_flag('GOKWIK_PRODUCTION_APPROVED')
app.config['GOKWIK_SDK_URL'] = gokwik_sdk_urls[gokwik_environment]
app.config['GOKWIK_API_BASE_URL'] = gokwik_api_base_urls[gokwik_environment]
app.config['GOKWIK_MERCHANT_ID'] = os.environ.get('GOKWIK_MERCHANT_ID')
app.config['GOKWIK_APP_ID'] = os.environ.get('GOKWIK_APP_ID')
app.config['GOKWIK_APP_SECRET'] = os.environ.get('GOKWIK_APP_SECRET')

if is_production_runtime() and not app.config['GOKWIK_ENABLED']:
    raise RuntimeError('Production checkout requires GOKWIK_ENABLED=1.')

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

app.config['ITHINK_ENABLED'] = environment_flag('ITHINK_ENABLED')
app.config['ITHINK_ACCESS_TOKEN'] = os.environ.get('ITHINK_ACCESS_TOKEN')
app.config['ITHINK_SECRET_KEY'] = os.environ.get('ITHINK_SECRET_KEY')
app.config['ITHINK_PICKUP_ADDRESS_ID'] = os.environ.get('ITHINK_PICKUP_ADDRESS_ID')
app.config['ITHINK_RETURN_ADDRESS_ID'] = os.environ.get('ITHINK_RETURN_ADDRESS_ID')
app.config['ITHINK_STORE_ID'] = os.environ.get('ITHINK_STORE_ID', '')
app.config['ITHINK_API_SOURCE'] = os.environ.get('ITHINK_API_SOURCE', '')
app.config['ITHINK_LOGISTICS'] = os.environ.get('ITHINK_LOGISTICS', '')
app.config['ITHINK_SERVICE_TYPE'] = os.environ.get('ITHINK_SERVICE_TYPE') or 'surface'
app.config['ITHINK_DEFAULT_WEIGHT_KG'] = os.environ.get('ITHINK_DEFAULT_WEIGHT_KG', '0.5')
app.config['ITHINK_DEFAULT_LENGTH_CM'] = os.environ.get('ITHINK_DEFAULT_LENGTH_CM', '10')
app.config['ITHINK_DEFAULT_WIDTH_CM'] = os.environ.get('ITHINK_DEFAULT_WIDTH_CM', '10')
app.config['ITHINK_DEFAULT_HEIGHT_CM'] = os.environ.get('ITHINK_DEFAULT_HEIGHT_CM', '10')

if app.config['ITHINK_ENABLED']:
    required_ithink_settings = (
        'ITHINK_ACCESS_TOKEN',
        'ITHINK_SECRET_KEY',
        'ITHINK_PICKUP_ADDRESS_ID',
        'ITHINK_RETURN_ADDRESS_ID',
    )
    missing_ithink_settings = [
        name for name in required_ithink_settings if not app.config.get(name)
    ]
    if missing_ithink_settings:
        raise RuntimeError(
            'iThink is enabled but required configuration is missing: '
            + ', '.join(missing_ithink_settings)
        )

app.config['BREVO_API_KEY'] = os.environ.get('BREVO_API_KEY')
app.config['BREVO_SENDER_EMAIL'] = os.environ.get('BREVO_SENDER_EMAIL')
app.config['BREVO_SENDER_NAME'] = os.environ.get('BREVO_SENDER_NAME', 'Zuhraan')

if environment_flag('TRUST_PROXY_HEADERS'):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

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


@app.get('/healthz')
def health_check():
    """Return deployment health without exposing configuration or credentials."""
    try:
        db.session.execute(text('SELECT 1'))
        inspector = inspect(db.engine)
        missing_tables = [
            table_name
            for table_name in sorted(db.metadata.tables)
            if not inspector.has_table(table_name)
        ]
    except SQLAlchemyError:
        db.session.rollback()
        app.logger.exception('Database health check failed.')
        return {'status': 'unhealthy'}, 503
    if missing_tables:
        db.session.rollback()
        app.logger.error(
            'Database schema is incomplete. Missing tables: %s',
            ', '.join(missing_tables),
        )
        return {'status': 'unhealthy'}, 503
    return {'status': 'ok'}, 200

@app.cli.command('gokwik-readiness')
@click.option('--verify-api', is_flag=True, help='Check merchant registration with GoKwik without creating an order.')
def gokwik_readiness(verify_api: bool):
    """Verify local GoKwik configuration, schema, and authenticated routing."""
    checks = {
        'environment': app.config['GOKWIK_ENV'] in {'sandbox', 'production'},
        'merchant_id': bool(app.config.get('GOKWIK_MERCHANT_ID')),
        'app_id': bool(app.config.get('GOKWIK_APP_ID')),
        'app_secret': bool(app.config.get('GOKWIK_APP_SECRET')),
        'server_integration_enabled': bool(app.config.get('GOKWIK_ENABLED')),
    }
    click.echo(f"GoKwik environment: {app.config['GOKWIK_ENV']}")
    if verify_api:
        try:
            verify_gokwik_merchant()
        except GoKwikRequestError as error:
            raise click.ClickException(str(error)) from error
        click.echo('[PASS] remote_merchant_registration (does not verify App ID/App Secret or callbacks)')

    inspector = inspect(db.engine)
    checks['checkout_session_table'] = inspector.has_table('go_kwik_checkout_session')
    if inspector.has_table('go_kwik_checkout_session'):
        checkout_columns = {
            column['name']
            for column in inspector.get_columns('go_kwik_checkout_session')
        }
        checks['checkout_session_columns'] = {
            'status',
            'failure_reason',
            'order_id',
            'expires_at',
        }.issubset(checkout_columns)
    else:
        checks['checkout_session_columns'] = False
    if inspector.has_table('order'):
        order_columns = {column['name'] for column in inspector.get_columns('order')}
        checks['order_columns'] = {
            'payment_method',
            'gokwik_transaction_id',
            'shipping_provider',
            'awb_number',
        }.issubset(order_columns)
        transaction_unique_constraints = {
            column_name
            for constraint in inspector.get_unique_constraints('order')
            for column_name in constraint.get('column_names') or []
        }
        transaction_unique_indexes = {
            column_name
            for index in inspector.get_indexes('order')
            if index.get('unique')
            for column_name in index.get('column_names') or []
        }
        checks['transaction_id_unique'] = 'gokwik_transaction_id' in (
            transaction_unique_constraints | transaction_unique_indexes
        )
    else:
        checks['order_columns'] = False
        checks['transaction_id_unique'] = False

    if inspector.has_table('product'):
        product_columns = {column['name'] for column in inspector.get_columns('product')}
        checks['product_columns'] = {
            'show_top_notes',
            'show_middle_notes',
            'show_base_notes',
            'show_longevity',
            'show_projection',
            'variants_enabled',
        }.issubset(product_columns)
    else:
        checks['product_columns'] = False

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
    if not all(checks.values()):
        raise click.ClickException(
            'GoKwik is not ready. Resolve the failed checks; no credential values were printed.'
        )


@app.cli.command('ithink-readiness')
@click.option(
    '--verify-api',
    is_flag=True,
    help='Verify credentials and warehouse access without creating an order.',
)
def ithink_readiness(verify_api: bool):
    """Verify iThink configuration, schema, and optionally API credentials."""
    from ithink_client import IThinkClient, IThinkRequestError, parcel_dimensions

    checks = {
        'integration_enabled': bool(app.config.get('ITHINK_ENABLED')),
        'access_token': bool(app.config.get('ITHINK_ACCESS_TOKEN')),
        'secret_key': bool(app.config.get('ITHINK_SECRET_KEY')),
        'pickup_address_id': bool(app.config.get('ITHINK_PICKUP_ADDRESS_ID')),
        'return_address_id': bool(app.config.get('ITHINK_RETURN_ADDRESS_ID')),
    }
    try:
        parcel_dimensions(
            app.config['ITHINK_DEFAULT_WEIGHT_KG'],
            app.config['ITHINK_DEFAULT_LENGTH_CM'],
            app.config['ITHINK_DEFAULT_WIDTH_CM'],
            app.config['ITHINK_DEFAULT_HEIGHT_CM'],
        )
        checks['parcel_defaults'] = True
    except ValueError:
        checks['parcel_defaults'] = False

    try:
        inspector = inspect(db.engine)
        checks['database_connection'] = True
        if inspector.has_table('order'):
            order_columns = {column['name'] for column in inspector.get_columns('order')}
            checks['order_columns'] = {
                'shipping_order_reference',
                'shipping_status',
                'shipping_status_code',
                'shipping_tracking_url',
                'shipping_tracking_updated_at',
            }.issubset(order_columns)
        else:
            checks['order_columns'] = False
    except SQLAlchemyError:
        app.logger.exception('iThink readiness database check failed.')
        checks['database_connection'] = False
        checks['order_columns'] = False

    if verify_api:
        try:
            warehouses = IThinkClient().get_warehouses()
            checks['api_authentication'] = isinstance(warehouses.get('data'), (list, dict))
        except IThinkRequestError:
            app.logger.exception('iThink readiness authentication failed.')
            checks['api_authentication'] = False

    for name, passed in checks.items():
        click.echo(f"[{'PASS' if passed else 'FAIL'}] {name}")
    click.echo('[INFO] api_writes_performed=False')
    if not all(checks.values()):
        raise click.ClickException(
            'iThink is not ready. Resolve the failed checks; no credential values were printed.'
        )


@app.cli.command('ithink-sync-tracking')
def ithink_sync_tracking():
    """Refresh active iThink AWBs in batches of ten."""
    from ithink_client import IThinkClient, IThinkRequestError, extract_tracking_data
    from models import Order

    if not app.config.get('ITHINK_ENABLED'):
        raise click.ClickException('iThink integration is disabled.')

    orders = Order.query.filter(
        Order.awb_number.isnot(None),
        Order.status.notin_(['delivered', 'cancelled']),
    ).all()
    client = IThinkClient()
    updated = 0
    failed = 0
    for start in range(0, len(orders), 10):
        batch = orders[start:start + 10]
        try:
            tracking_payload = client.track_awbs([order.awb_number for order in batch])
            gokwik_sync_orders = []
            for order in batch:
                try:
                    tracking = extract_tracking_data(tracking_payload, order.awb_number)
                except IThinkRequestError:
                    failed += 1
                    continue
                shipment_before = (
                    order.shipping_provider,
                    order.awb_number,
                    order.shipping_tracking_url,
                )
                status_changed = apply_ithink_tracking(order, tracking)
                shipment_changed = shipment_before != (
                    order.shipping_provider,
                    order.awb_number,
                    order.shipping_tracking_url,
                )
                if (status_changed or shipment_changed) and is_gokwik_payment_method(order.payment_method):
                    gokwik_sync_orders.append(order)
                updated += 1
            db.session.commit()
            for order in gokwik_sync_orders:
                try:
                    sync_gokwik_order(
                        order.id,
                        order.status,
                        order.shipping_provider,
                        order.awb_number,
                    )
                except GoKwikRequestError:
                    failed += 1
                    app.logger.exception(
                        'iThink tracking was saved but GoKwik synchronization failed.',
                        extra={'order_id': order.id},
                    )
        except (IThinkRequestError, ValueError, GoKwikRequestError):
            db.session.rollback()
            failed += len(batch)
            app.logger.exception('iThink tracking batch failed.')

    click.echo(f'[INFO] orders_checked={len(orders)} orders_updated={updated} failures={failed}')
    if failed:
        raise click.ClickException('Some iThink tracking records could not be refreshed.')

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


@app.context_processor
def inject_meta_pixel_purchase():
    """Expose and consume the one-time browser purchase event payload."""
    return dict(meta_pixel_purchase=session.pop('meta_pixel_purchase', None))

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
    admin_email = os.environ.get('ADMIN_EMAIL')
    admin_password = os.environ.get('ADMIN_PASSWORD')
    if not admin_email or not admin_password:
        raise click.ClickException('ADMIN_EMAIL and ADMIN_PASSWORD are required to create an admin.')
    
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
        'shipping_charge_prepaid': '0',
        'free_shipping_threshold_prepaid': '0',
        'shipping_charge_cod': '0',
        'free_shipping_threshold_cod': '0',
        'cod_enabled': '1',
    }
    for k, v in settings.items():
        if not Setting.query.filter_by(key=k).first():
            db.session.add(Setting(key=k, value=v))

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
    click.echo('Database initialized.')


@app.cli.command('init-contact-messages')
def init_contact_messages():
    """Create the contact message table without changing existing store data."""
    ContactMessage.__table__.create(bind=db.engine, checkfirst=True)
    print('Contact message table is ready.')

if __name__ == '__main__':
    server_host: str = os.environ.get('FLASK_RUN_HOST', '127.0.0.1')
    server_port: int = int(os.environ.get('PORT', '5005'))
    try:
        waitress_threads = int(os.environ.get('WAITRESS_THREADS', '8'))
    except ValueError as error:
        raise RuntimeError('WAITRESS_THREADS must be a positive integer.') from error
    if waitress_threads < 1:
        raise RuntimeError('WAITRESS_THREADS must be a positive integer.')
    click.echo(
        f'Waitress serving Zuhraan on http://{server_host}:{server_port} '
        f'with {waitress_threads} threads'
    )
    serve(
        app,
        host=server_host,
        port=server_port,
        threads=waitress_threads,
    )
