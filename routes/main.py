from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash, session
from models import db, Product, Category, ProductVariant, Review, User, Order, Setting, Coupon, OfferBanner, AboutPage, ContactMessage
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import json
import secrets
import time
import requests
from decimal import Decimal, InvalidOperation
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import SQLAlchemyError
from urllib.parse import urlsplit

from datetime import datetime
from about_cms import get_assets, get_content
from homepage_reels import get_homepage_reels

main_bp = Blueprint('main', __name__)
GUEST_ORDER_SESSION_KEY = 'guest_order_ids'

def _auto_create_shiprocket_shipment(order: Order) -> bool:
    """Automatically create Shiprocket shipment for eligible orders."""
    if not current_app.config.get('SHIPROCKET_ENABLED'):
        return False
    if order.shiprocket_shipment_id:
        return True
    if order.status in {'cancelled', 'delivered'}:
        return False

    from shiprocket_client import (
        COD_PAYMENT_METHODS,
        ShiprocketClient,
        ShiprocketRequestError,
        build_shiprocket_order_payload,
        parcel_dimensions,
    )

    is_cod = order.payment_method in COD_PAYMENT_METHODS
    is_prepaid_paid = order.payment_method not in COD_PAYMENT_METHODS and order.payment_status == 'paid'
    if not (is_cod or is_prepaid_paid):
        return False

    try:
        from routes.admin import _shiprocket_operational_settings
        from models import utcnow_naive

        settings = _shiprocket_operational_settings()
        dimensions = parcel_dimensions(
            settings['weight'], settings['length'], settings['breadth'], settings['height']
        )
        client = ShiprocketClient()

        payload = build_shiprocket_order_payload(order, settings['pickup_location'], dimensions)
        created = client.create_order(payload)
        shiprocket_order_id = str(created.get('order_id') or '').strip()
        shipment_id = str(created.get('shipment_id') or '').strip()
        if not shiprocket_order_id or not shipment_id:
            raise ShiprocketRequestError(
                'Shiprocket created no usable order or shipment identifier.'
            )

        order.shiprocket_order_id = shiprocket_order_id
        order.shiprocket_shipment_id = shipment_id
        order.shiprocket_status = str(created.get('status') or 'ORDER CREATED')[:100]
        order.status = 'processing'
        order.shiprocket_tracking_updated_at = utcnow_naive()
        db.session.commit()

        current_app.logger.info(
            'Auto-created Shiprocket order',
            extra={'order_id': order.id, 'shipment_id': order.shiprocket_shipment_id},
        )
        return True
    except (ShiprocketRequestError, ValueError, SQLAlchemyError):
        db.session.rollback()
        current_app.logger.exception(
            'Auto Shiprocket shipment creation failed',
            extra={'order_id': order.id},
        )
        return False


INVENTORY_TRACKED_PAYMENT_METHODS = frozenset({
    'cod',
    'gokwik_prepaid',
    'legacy_prepaid',
})
FULFILLMENT_STEPS = (
    'Order placed',
    'Confirmed',
    'Packed',
    'Shipped',
    'Out for delivery',
    'Delivered',
)


def _fulfillment_stage(order: Order) -> int:
    """Return the furthest customer-facing fulfillment stage reached by an order."""
    local_status = str(order.status or '').strip().upper()
    carrier_status = str(order.shiprocket_status or '').strip().upper()

    if (
        local_status == 'DELIVERED'
        or ('DELIVERED' in carrier_status and 'RTO' not in carrier_status)
    ):
        return 5
    if 'OUT FOR DELIVERY' in carrier_status:
        return 4
    if local_status == 'SHIPPED' or any(
        marker in carrier_status
        for marker in (
            'PICKED UP',
            'IN TRANSIT',
            'SHIPPED',
            'REACHED DESTINATION',
            'DISPATCHED',
            'RTO',
        )
    ):
        return 3
    if order.awb_number or order.shiprocket_pickup_scheduled or any(
        marker in carrier_status
        for marker in ('PACKED', 'MANIFEST', 'READY TO SHIP', 'PICKUP SCHEDULED', 'AWB')
    ):
        return 2
    if order.shiprocket_shipment_id or local_status == 'PROCESSING' or any(
        marker in carrier_status
        for marker in ('NEW', 'ORDER CREATED', 'CONFIRMED', 'PROCESSING')
    ):
        return 1
    return 0


def _fulfillment_view(order: Order) -> dict[str, object]:
    """Build accurate presentation data for the authenticated customer's order card."""
    active_stage = _fulfillment_stage(order)
    latest_update = order.shiprocket_tracking_updated_at
    steps: list[dict[str, object]] = []
    for index, label in enumerate(FULFILLMENT_STEPS):
        if index == 0:
            detail = order.created_at.strftime('%d %b %Y, %I:%M %p')
        elif index < active_stage:
            detail = 'Completed'
        elif index == active_stage:
            detail = (
                latest_update.strftime('%d %b %Y, %I:%M %p')
                if latest_update
                else 'Current stage'
            )
        else:
            detail = 'Awaiting update'
        steps.append({
            'label': label,
            'detail': detail,
            'complete': index <= active_stage,
            'current': index == active_stage,
        })

    raw_status = str(order.shiprocket_status or order.status or 'Order placed').strip()
    display_status = raw_status.replace('_', ' ').title()
    if raw_status.upper() in {'AWB ASSIGNED', 'RTO'}:
        display_status = raw_status.upper()

    return {
        'steps': steps,
        'status': display_status,
        'cancelled': str(order.status or '').lower() == 'cancelled',
        'live': bool(order.shiprocket_shipment_id),
        'latest_update': latest_update,
    }


def safe_internal_redirect(value):
    """Return a local redirect target, or an empty string for external URLs."""
    target = str(value or '').strip()
    parsed = urlsplit(target)
    if not target.startswith('/') or target.startswith('//') or parsed.scheme or parsed.netloc:
        return ''
    return target

# ── COUPON VALIDATION HELPER ─────────────────────────────────
def _validate_coupon(code, order_total):
    """Returns dict: {valid, discount, final_total, message}"""
    now = datetime.utcnow()
    coupon = Coupon.query.filter_by(code=code, is_active=True).first()
    if not coupon:
        return {'valid': False, 'message': 'Invalid or inactive coupon code.'}
    if coupon.expires_at and coupon.expires_at < now:
        return {'valid': False, 'message': 'This coupon has expired.'}
    if coupon.max_uses and coupon.used_count >= coupon.max_uses:
        return {'valid': False, 'message': 'This coupon has reached its usage limit.'}
    if order_total < coupon.min_order_amount:
        return {'valid': False, 'message': f'Minimum order of ₹{coupon.min_order_amount:.2f} required.'}
    if coupon.discount_type == 'percent':
        discount = round(order_total * coupon.discount_value / 100, 2)
    else:
        discount = min(coupon.discount_value, order_total)
    final = round(order_total - discount, 2)
    return {'valid': True, 'discount': discount, 'final_total': final,
            'message': f'{coupon.discount_value}% off applied!' if coupon.discount_type == 'percent' else f'₹{coupon.discount_value:.2f} off applied!',
            'coupon_id': coupon.id}

def _get_non_negative_setting_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal('0.00')

    if not amount.is_finite() or amount < 0:
        return Decimal('0.00')
    return amount.quantize(Decimal('0.01'))


def calculate_shipping(subtotal, settings):
    shipping_charge = _get_non_negative_setting_amount(settings.get('shipping_charge'))
    free_shipping_threshold = _get_non_negative_setting_amount(settings.get('free_shipping_threshold'))
    order_subtotal = _get_non_negative_setting_amount(subtotal)

    if free_shipping_threshold > 0 and order_subtotal >= free_shipping_threshold:
        return 0.0
    return float(shipping_charge)


@main_bp.route('/')
def index():
    best_sellers = (Product.query
                    .filter_by(tag='best_seller')
                    .order_by(Product.best_seller_rank.asc().nullslast())
                    .limit(4).all())
    categories = Category.query.all()
    offers = OfferBanner.query.filter_by(is_active=True).order_by(OfferBanner.created_at.desc()).all()
    # Products for the hero right-side card transition
    hero_products = Product.query.order_by(Product.created_at.desc()).limit(8).all()
    # Featured products for "Pick Your Favorite One" section
    favorite_products = Product.query.filter_by(tag='featured').limit(8).all()
    # Homepage media
    media_url = (Setting.query.filter_by(key='homepage_media_url').first() or Setting(value='')).value
    media_type = (Setting.query.filter_by(key='homepage_media_type').first() or Setting(value='')).value
    bottom_banner_url = (Setting.query.filter_by(key='bottom_banner_url').first() or Setting(value='')).value
    hero_about_image_url = (Setting.query.filter_by(key='hero_about_image_url').first() or Setting(value='')).value
    # About page data for home page About Us image - force fresh query from DB
    db.session.expire_all()
    about_page = db.session.query(AboutPage).populate_existing().first()
    story_images_setting = Setting.query.filter_by(key='home_story_images').first()
    try:
        story_images = json.loads(story_images_setting.value) if story_images_setting and story_images_setting.value else []
    except json.JSONDecodeError:
        story_images = []
    story_images = [image for image in story_images if isinstance(image, dict) and image.get('url')] if isinstance(story_images, list) else []
    reel_slots = get_homepage_reels(url_for('static', filename='videos/reel1.mp4'))
    reels = [reel for reel in reel_slots if reel is not None]
    return render_template('index.html', best_sellers=best_sellers, categories=categories, offers=offers, hero_products=hero_products, favorite_products=favorite_products, media_url=media_url, media_type=media_type, bottom_banner_url=bottom_banner_url, hero_about_image_url=hero_about_image_url, about_page=about_page, story_images=story_images, reels=reels)

@main_bp.route('/privacy-policy')
def privacy():
    return render_template('main/privacy.html')

@main_bp.route('/terms-and-conditions')
def terms():
    return render_template('main/terms.html')

@main_bp.route('/shipping-and-delivery')
def shipping():
    return render_template('main/shipping.html')

@main_bp.route('/cancellation-and-refunds')
def cancellation():
    return render_template('main/cancellation.html')

@main_bp.route('/about')
def about():
    return render_template('main/about.html', content=get_content(), assets=get_assets())

@main_bp.route('/contact', methods=['GET', 'POST'])
def contact():
    if request.method == 'GET':
        return render_template('main/contact.html')

    topic = request.form.get('topic', '').strip()
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone', '').strip()
    message = request.form.get('message', '').strip()
    valid_topics = {'Scent Advice', 'Gifts & Custom', 'Order Status', 'General Query'}

    if topic not in valid_topics:
        flash('Please select a valid topic.', 'error')
        return redirect(url_for('main.contact') + '#contact-form')
    if not name or len(name) > 100:
        flash('Enter a name of up to 100 characters.', 'error')
        return redirect(url_for('main.contact') + '#contact-form')
    if not email or len(email) > 120 or '@' not in email or email.startswith('@') or email.endswith('@'):
        flash('Enter a valid email address.', 'error')
        return redirect(url_for('main.contact') + '#contact-form')
    if len(phone) > 30:
        flash('Enter a phone number of up to 30 characters.', 'error')
        return redirect(url_for('main.contact') + '#contact-form')
    if not message or len(message) > 500:
        flash('Enter a message of up to 500 characters.', 'error')
        return redirect(url_for('main.contact') + '#contact-form')

    contact_message = ContactMessage(
        topic=topic,
        name=name,
        email=email,
        phone=phone or None,
        message=message,
    )
    try:
        db.session.add(contact_message)
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception('Failed to save contact message.')
        flash('We could not save your message. Please try again shortly.', 'error')
        return redirect(url_for('main.contact') + '#contact-form')

    flash('Your message has been received. Our support team will reply soon.', 'success')
    return redirect(url_for('main.contact') + '#contact-form')

@main_bp.route('/product/<string:slug>')
def product_detail(slug):
    product = Product.query.filter_by(slug=slug).first_or_404()
    related_products = (Product.query
                       .filter(Product.category_id == product.category_id, 
                               Product.id != product.id)
                       .limit(4).all())
    return render_template('product_detail.html', product=product, related_products=related_products)

@main_bp.route('/product/<string:slug>/review', methods=['POST'])
def submit_review(slug):
    product = Product.query.filter_by(slug=slug).first_or_404()
    name = request.form.get('name')
    rating = request.form.get('rating', type=int)
    comment = request.form.get('comment')
    if not name or not rating or not comment:
        flash('All fields are required', 'info')
        return redirect(url_for('main.product_detail', slug=slug))
    new_review = Review(
        product_id=product.id,
        user_id=current_user.id if current_user.is_authenticated else None,
        customer_name=name,
        rating=rating,
        comment=comment
    )
    db.session.add(new_review)
    db.session.commit()
    flash('Thank you for your wonderful review!', 'success')
    return redirect(url_for('main.product_detail', slug=slug))

@main_bp.route('/shop')
def shop():
    category_value = request.args.get('cat', '').strip()
    sort = request.args.get('sort', 'newest')
    if sort not in {'newest', 'price_low', 'price_high'}:
        sort = 'newest'

    on_sale = 1 if request.args.get('on_sale') == '1' else None
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    search_query = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 8

    if min_price is not None and max_price is not None and min_price > max_price:
        flash('Minimum price cannot be greater than maximum price.', 'error')
    
    query = Product.query
    
    if search_query:
        query = query.filter(
            (Product.name.ilike(f'%{search_query}%')) | 
            (Product.short_description.ilike(f'%{search_query}%')) |
            (Product.full_description.ilike(f'%{search_query}%')) |
            (Product.tag.ilike(f'%{search_query}%'))
        )
        
    active_category = None
    if category_value:
        # Category IDs are the canonical shop-filter value.  Accept slugs too so
        # existing category-card links and bookmarked URLs keep working.
        if category_value.isdecimal():
            active_category = db.session.get(Category, int(category_value))
        if active_category is None:
            active_category = Category.query.filter_by(slug=category_value).first()
        if active_category:
            query = query.filter_by(category_id=active_category.id)

    variant_filters = []
    if on_sale:
        variant_filters.append(ProductVariant.original_price > ProductVariant.price)
    if min_price is not None:
        variant_filters.append(ProductVariant.price >= min_price)
    if max_price is not None:
        variant_filters.append(ProductVariant.price <= max_price)
    if variant_filters:
        # A product is included only when one of its variants meets every active
        # price/deal condition.  EXISTS avoids duplicate products and conflicting
        # joins when filters are combined.
        query = query.filter(Product.variants.any(and_(*variant_filters)))

    if sort in {'price_low', 'price_high'}:
        price_aggregate = func.min if sort == 'price_low' else func.max
        product_price = (
            db.session.query(price_aggregate(ProductVariant.price))
            .filter(ProductVariant.product_id == Product.id)
            .correlate(Product)
            .scalar_subquery()
        )
        price_order = product_price.asc() if sort == 'price_low' else product_price.desc()
        query = query.order_by(product_price.is_(None), price_order, Product.created_at.desc())
    else:
        query = query.order_by(Product.created_at.desc())
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items
    categories = Category.query.all()
    return render_template(
        'shop.html',
        categories=categories,
        products=products,
        active_cat=active_category.id if active_category else None,
        active_sort=sort,
        active_sale=on_sale,
        active_min_price=min_price,
        active_max_price=max_price,
        search_query=search_query,
        pagination=pagination,
        page=page,
    )

# --- CART LOGIC ---
@main_bp.route('/cart/add', methods=['POST'])
def add_to_cart():
    variant_id = request.form.get('variant_id', type=int)
    quantity = request.form.get('quantity', 1, type=int)
    wants_json = request.accept_mimetypes.best == 'application/json'

    if not variant_id or not quantity or quantity < 1:
        message = 'Please select a valid product quantity.'
        if wants_json:
            return jsonify({'message': message}), 400
        flash(message, 'error')
        return redirect(request.referrer or url_for('main.shop'))

    variant = ProductVariant.query.get_or_404(variant_id)
    cart = session.get('cart', {})
    v_id_str = str(variant_id)
    available_stock = variant.stock_quantity or 0
    existing_quantity = cart.get(v_id_str, 0)

    if available_stock <= 0:
        message = f'{variant.product.name} is sold out.'
        if wants_json:
            return jsonify({'message': message}), 409
        flash(message, 'error')
        return redirect(request.referrer or url_for('main.shop'))

    if existing_quantity + quantity > available_stock:
        message = f'Only {available_stock} unit(s) of {variant.product.name} are available.'
        if wants_json:
            return jsonify({'message': message}), 409
        flash(message, 'error')
        return redirect(request.referrer or url_for('main.shop'))

    cart[v_id_str] = existing_quantity + quantity
    session['cart'] = cart
    message = f'{variant.product.name} added to cart!'
    if wants_json:
        return jsonify({'message': message, 'cart_count': sum(cart.values())})
    flash(message, 'success')
    return redirect(request.referrer or url_for('main.shop'))

@main_bp.route('/cart')
def cart():
    cart = session.get('cart', {})
    cart_items = []
    total = 0
    for v_id, qty in cart.items():
        variant = ProductVariant.query.get(int(v_id))
        if variant:
            item_total = variant.price * qty
            total += item_total
            cart_items.append({'variant': variant, 'quantity': qty, 'total': item_total})
    return render_template('main/cart.html', cart_items=cart_items, total=total)

@main_bp.route('/cart/remove/<int:variant_id>', methods=['POST'])
def remove_from_cart(variant_id):
    cart = session.get('cart', {})
    v_id_str = str(variant_id)
    if v_id_str in cart:
        del cart[v_id_str]
        session['cart'] = cart
    return redirect(url_for('main.cart'))
@main_bp.route('/cart/update/<int:variant_id>', methods=['POST'])
def update_cart(variant_id):
    action = request.form.get('action')  # 'increase' or 'decrease'
    cart = session.get('cart', {})
    v_id_str = str(variant_id)
    if v_id_str in cart:
        if action == 'increase':
            variant = ProductVariant.query.get_or_404(variant_id)
            available_stock = variant.stock_quantity or 0
            if cart[v_id_str] < available_stock:
                cart[v_id_str] += 1
            else:
                flash(f'Only {available_stock} unit(s) are available.', 'error')
        elif action == 'decrease':
            if cart[v_id_str] > 1:
                cart[v_id_str] -= 1
            else:
                del cart[v_id_str]
    session['cart'] = cart
    return redirect(url_for('main.cart'))

def _remember_guest_order(order: Order) -> None:
    """Remember a guest purchase in the signed browser session."""
    if order.user_id is not None:
        return
    remembered_ids = session.get(GUEST_ORDER_SESSION_KEY, [])
    if not isinstance(remembered_ids, list):
        remembered_ids = []
    normalized_ids = [
        order_id
        for order_id in remembered_ids
        if isinstance(order_id, int) and order_id > 0
    ]
    if order.id not in normalized_ids:
        normalized_ids.append(order.id)
    session[GUEST_ORDER_SESSION_KEY] = normalized_ids[-20:]


def _link_guest_orders_to_user(user: User) -> None:
    """Link verified-email and same-browser guest orders to an account."""
    remembered_ids = session.get(GUEST_ORDER_SESSION_KEY, [])
    normalized_ids = [
        order_id
        for order_id in remembered_ids
        if isinstance(order_id, int) and order_id > 0
    ] if isinstance(remembered_ids, list) else []
    normalized_email = str(user.email or '').strip().lower()
    ownership_conditions = [
        func.lower(func.trim(Order.customer_email)) == normalized_email,
    ]
    if normalized_ids:
        ownership_conditions.append(Order.id.in_(normalized_ids))

    guest_orders = Order.query.filter(
        Order.user_id.is_(None),
        or_(*ownership_conditions),
    ).all()
    for order in guest_orders:
        order.user_id = user.id
    if guest_orders:
        db.session.commit()
    session.pop(GUEST_ORDER_SESSION_KEY, None)


# --- AUTH ---
@main_bp.route('/account/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        next_url = request.args.get('next', '')
        safe_next_url = safe_internal_redirect(next_url)
        if safe_next_url:
            return redirect(safe_next_url)
        return redirect(url_for('main.account'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password')
        next_url = request.form.get('next', '').strip()
        user = User.query.filter(
            func.lower(func.trim(User.email)) == email,
        ).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            _link_guest_orders_to_user(user)
            # Only redirect to safe internal URLs
            safe_next_url = safe_internal_redirect(next_url)
            if safe_next_url:
                return redirect(safe_next_url)
            return redirect(url_for('main.account'))
        flash('Invalid credentials', 'error')
    next_url = request.args.get('next', '')
    return render_template('main/login.html', next_url=next_url)

@main_bp.route('/account/request-otp', methods=['POST'])
def request_otp():
    email = request.form.get('email', '').strip().lower()
    next_url = request.form.get('next', '').strip()
    if not email:
        flash('Enter your email address to receive an OTP.', 'error')
        return redirect(url_for('main.login', next=next_url))

    now = time.time()
    last_sent = session.get('login_otp_sent_at', 0)
    if now - last_sent < 60:
        flash('Please wait before requesting another OTP.', 'error')
        return redirect(url_for('main.login', next=next_url))

    user = User.query.filter_by(email=email).first()
    api_key = current_app.config.get('BREVO_API_KEY')
    sender_email = current_app.config.get('BREVO_SENDER_EMAIL')
    if not user:
        flash('If an account exists for this email, an OTP will be sent.', 'info')
        return redirect(url_for('main.login', next=next_url))
    if not api_key or not sender_email:
        current_app.logger.error('Brevo OTP settings are not configured.')
        flash('Email login is temporarily unavailable. Please use your password.', 'error')
        return redirect(url_for('main.login', next=next_url))

    otp = f'{secrets.randbelow(1000000):06d}'
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    session['login_otp'] = {
        'email': email,
        'hash': otp_hash,
        'expires_at': now + 600,
        'attempts': 0,
        'next_url': safe_internal_redirect(next_url),
    }

    payload = {
        'sender': {
            'name': current_app.config.get('BREVO_SENDER_NAME', 'Zuhraan'),
            'email': sender_email
        },
        'to': [{'email': email}],
        'subject': 'Your Zuhraan login OTP',
        'textContent': f'Your Zuhraan login OTP is {otp}. It expires in 10 minutes.'
    }
    try:
        response = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'accept': 'application/json', 'api-key': api_key, 'content-type': 'application/json'},
            json=payload,
            timeout=10
        )
        response.raise_for_status()
    except requests.RequestException:
        session.pop('login_otp', None)
        current_app.logger.exception('Brevo failed to send login OTP.')
        flash('We could not send the OTP. Please try again or use your password.', 'error')
        return redirect(url_for('main.login', next=next_url))

    session['login_otp_sent_at'] = now
    flash('A login OTP has been sent to your email.', 'success')
    return redirect(url_for('main.login', otp_sent=1, next=next_url))

@main_bp.route('/account/verify-otp', methods=['POST'])
def verify_otp():
    entered_otp = request.form.get('otp', '').strip()
    otp_data = session.get('login_otp')
    if not otp_data or time.time() > otp_data.get('expires_at', 0):
        session.pop('login_otp', None)
        flash('Your OTP has expired. Please request a new one.', 'error')
        return redirect(url_for('main.login'))
    if otp_data.get('attempts', 0) >= 5:
        session.pop('login_otp', None)
        flash('Too many incorrect attempts. Please request a new OTP.', 'error')
        return redirect(url_for('main.login'))

    otp_data['attempts'] += 1
    session['login_otp'] = otp_data
    entered_hash = hashlib.sha256(entered_otp.encode()).hexdigest()
    if not secrets.compare_digest(entered_hash, otp_data['hash']):
        flash('Invalid OTP.', 'error')
        return redirect(url_for('main.login', otp_sent=1, next=otp_data.get('next_url', '')))

    user = User.query.filter_by(email=otp_data['email']).first()
    session.pop('login_otp', None)
    session.pop('login_otp_sent_at', None)
    if not user:
        flash('Unable to complete email login.', 'error')
        return redirect(url_for('main.login'))
    login_user(user)
    _link_guest_orders_to_user(user)
    next_url = otp_data.get('next_url', '')
    return redirect(safe_internal_redirect(next_url) or url_for('main.account'))

@main_bp.route('/account/forgot-password', methods=['POST'])
def forgot_password():
    email = request.form.get('email', '').strip().lower()
    if not email:
        flash('Enter your email address to reset your password.', 'error')
        return redirect(url_for('main.login', mode='forgot'))

    user = User.query.filter_by(email=email).first()
    api_key = current_app.config.get('BREVO_API_KEY')
    sender_email = current_app.config.get('BREVO_SENDER_EMAIL')
    if not user:
        flash('If an account exists for this email, a reset OTP will be sent.', 'info')
        return redirect(url_for('main.login', mode='forgot'))
    if not api_key or not sender_email:
        current_app.logger.error('Brevo password reset settings are not configured.')
        flash('Password reset is temporarily unavailable. Please contact support.', 'error')
        return redirect(url_for('main.login', mode='forgot'))

    otp = f'{secrets.randbelow(1000000):06d}'
    reset_data = {
        'email': email,
        'hash': hashlib.sha256(otp.encode()).hexdigest(),
        'expires_at': time.time() + 600,
        'attempts': 0
    }
    payload = {
        'sender': {
            'name': current_app.config.get('BREVO_SENDER_NAME', 'Zuhraan'),
            'email': sender_email
        },
        'to': [{'email': email}],
        'subject': 'Reset your Zuhraan password',
        'textContent': f'Your Zuhraan password reset OTP is {otp}. It expires in 10 minutes.'
    }
    try:
        response = requests.post(
            'https://api.brevo.com/v3/smtp/email',
            headers={'accept': 'application/json', 'api-key': api_key, 'content-type': 'application/json'},
            json=payload,
            timeout=10
        )
        response.raise_for_status()
    except requests.RequestException:
        current_app.logger.exception('Brevo failed to send password reset OTP.')
        flash('We could not send the reset OTP. Please try again.', 'error')
        return redirect(url_for('main.login', mode='forgot'))

    session['password_reset_otp'] = reset_data
    flash('A password reset OTP has been sent to your email.', 'success')
    return redirect(url_for('main.login', mode='forgot', reset_sent=1))

@main_bp.route('/account/reset-password', methods=['POST'])
def reset_password():
    reset_data = session.get('password_reset_otp')
    otp = request.form.get('otp', '').strip()
    password = request.form.get('password', '')
    confirm_password = request.form.get('confirm_password', '')
    if not reset_data or time.time() > reset_data.get('expires_at', 0):
        session.pop('password_reset_otp', None)
        flash('Your reset OTP has expired. Please request a new one.', 'error')
        return redirect(url_for('main.login', mode='forgot'))
    if reset_data.get('attempts', 0) >= 5:
        session.pop('password_reset_otp', None)
        flash('Too many incorrect attempts. Please request a new OTP.', 'error')
        return redirect(url_for('main.login', mode='forgot'))

    reset_data['attempts'] += 1
    session['password_reset_otp'] = reset_data
    entered_hash = hashlib.sha256(otp.encode()).hexdigest()
    if not secrets.compare_digest(entered_hash, reset_data['hash']):
        flash('Invalid reset OTP.', 'error')
        return redirect(url_for('main.login', mode='forgot', reset_sent=1))
    if len(password) < 8:
        flash('Password must be at least 8 characters.', 'error')
        return redirect(url_for('main.login', mode='forgot', reset_sent=1))
    if password != confirm_password:
        flash('Passwords do not match.', 'error')
        return redirect(url_for('main.login', mode='forgot', reset_sent=1))

    user = User.query.filter_by(email=reset_data['email']).first()
    if not user:
        session.pop('password_reset_otp', None)
        flash('Unable to reset the password for this account.', 'error')
        return redirect(url_for('main.login'))
    user.password = generate_password_hash(password, method='pbkdf2:sha256')
    db.session.commit()
    session.pop('password_reset_otp', None)
    flash('Your password has been reset. You can now sign in.', 'success')
    return redirect(url_for('main.login'))

@main_bp.route('/account/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated: return redirect(url_for('main.account'))
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        name = request.form.get('name')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
        elif User.query.filter(
            func.lower(func.trim(User.email)) == email,
        ).first():
            flash('Email already exists', 'error')
        else:
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
            new_user = User(email=email, name=name, password=hashed_pw, role='user')
            db.session.add(new_user)
            db.session.commit()
            _link_guest_orders_to_user(new_user)
            login_user(new_user)
            return redirect(url_for('main.account'))
    return render_template('main/register.html')

@main_bp.route('/account/logout', methods=['POST'])
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.index'))

@main_bp.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    if request.method == 'POST':
        current_user.phone = request.form.get('phone')
        current_user.address_line1 = request.form.get('address_line1')
        current_user.address_line2 = request.form.get('address_line2')
        current_user.city = request.form.get('city')
        current_user.state = request.form.get('state')
        current_user.pincode = request.form.get('pincode')
        current_user.country = request.form.get('country')
        db.session.commit()
        flash('Address updated successfully', 'success')
        return redirect(url_for('main.account'))

    _link_guest_orders_to_user(current_user)
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    fulfillment_by_order = {order.id: _fulfillment_view(order) for order in orders}
    return render_template(
        'main/account.html',
        orders=orders,
        fulfillment_by_order=fulfillment_by_order,
    )

# --- CHECKOUT ---
def release_order_inventory(order):
    """Restore inventory for a newly cancelled order exactly once per cancellation."""
    if order.payment_method not in INVENTORY_TRACKED_PAYMENT_METHODS:
        return
    quantities = {}
    for order_item in order.items:
        quantities[order_item.variant_id] = (
            quantities.get(order_item.variant_id, 0) + order_item.quantity
        )
    if not quantities:
        return

    variants = (
        ProductVariant.query
        .filter(ProductVariant.id.in_(sorted(quantities)))
        .order_by(ProductVariant.id)
        .with_for_update()
        .all()
    )
    if len(variants) != len(quantities):
        raise RuntimeError('Cannot restore inventory because an ordered variant is missing.')
    for variant in variants:
        variant.stock_quantity = (variant.stock_quantity or 0) + quantities[variant.id]


@main_bp.route('/api/validate-coupon', methods=['POST'])
def api_validate_coupon():
    data = request.json
    code = data.get('code', '').strip().upper()
    total = data.get('total', 0)
    result = _validate_coupon(code, total)
    return result

@main_bp.route('/checkout', methods=['GET'])
def checkout():
    """Redirect legacy checkout URLs to the GoKwik-powered cart."""
    if not session.get('cart'):
        return redirect(url_for('main.shop'))
    flash('Use the secure GoKwik checkout button in your cart to complete this purchase.', 'info')
    return redirect(url_for('main.cart'))
# End of public checkout routes.
