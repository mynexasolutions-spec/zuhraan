from flask import Blueprint, abort, current_app, jsonify, make_response, render_template, request, redirect, url_for, flash, session
from models import db, Product, Category, ProductVariant, Review, User, Order, OrderItem, Setting, Coupon, OfferBanner, AboutPage, ContactMessage
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import json
import razorpay
import secrets
import time
import requests
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from urllib.parse import urlsplit

from datetime import datetime, timedelta
from about_cms import get_assets, get_content
from homepage_reels import get_homepage_reels
from routes.ithink import apply_ithink_shipment, create_or_recover_ithink_shipment
from ithink_client import normalize_indian_phone, normalize_indian_pincode

main_bp = Blueprint('main', __name__)
GUEST_ORDER_SESSION_KEY = 'guest_order_ids'
CHECKOUT_SUBMISSION_SESSION_KEY = 'checkout_submission_token'


def _checkout_submission_token() -> str:
    """Return the one-time browser token used to make checkout idempotent."""
    token = session.get(CHECKOUT_SUBMISSION_SESSION_KEY)
    if isinstance(token, str) and token:
        return token
    token = secrets.token_urlsafe(32)
    session[CHECKOUT_SUBMISSION_SESSION_KEY] = token
    return token


def _add_business_days(start: datetime, business_days: int) -> datetime:
    """Return a date after the requested number of weekdays."""
    result = start
    remaining = business_days
    while remaining > 0:
        result += timedelta(days=1)
        if result.weekday() < 5:
            remaining -= 1
    return result


def _estimated_delivery_window(order: Order) -> str:
    """Build a customer-facing delivery window from the order timestamp."""
    placed_at = order.created_at or datetime.utcnow()
    start = _add_business_days(placed_at, 5)
    end = _add_business_days(placed_at, 7)
    return f'{start:%d %b %Y} – {end:%d %b %Y}'

def _auto_create_ithink_shipment(order: Order) -> bool:
    """Automatically create or recover an iThink shipment for an eligible order."""
    if not current_app.config.get('ITHINK_ENABLED'):
        return False
    if order.awb_number:
        return True
    if order.status in {'cancelled', 'delivered'}:
        return False

    from ithink_client import (
        IThinkClient,
        IThinkRequestError,
        parcel_dimensions,
        payment_mode_for_order,
    )

    try:
        payment_mode_for_order(order)
    except ValueError as error:
        current_app.logger.error(
            'Order is not eligible for iThink because its payment state is invalid.',
            extra={
                'order_id': order.id,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'reason': str(error),
            },
        )
        return False
    try:
        dimensions = parcel_dimensions(
            current_app.config['ITHINK_DEFAULT_WEIGHT_KG'],
            current_app.config['ITHINK_DEFAULT_LENGTH_CM'],
            current_app.config['ITHINK_DEFAULT_WIDTH_CM'],
            current_app.config['ITHINK_DEFAULT_HEIGHT_CM'],
        )
        client = IThinkClient()
        shipment = create_or_recover_ithink_shipment(order, dimensions, client)
        apply_ithink_shipment(order, shipment)
        if not shipment.get('awb_number'):
            if shipment.get('order_reference'):
                db.session.commit()
                current_app.logger.warning(
                    'Auto-created iThink shipment is waiting for an AWB',
                    extra={
                        'order_id': order.id,
                        'shipping_order_reference': order.shipping_order_reference,
                    },
                )
            return False
        db.session.commit()

        current_app.logger.info(
            'Auto-created iThink shipment',
            extra={'order_id': order.id, 'awb_number': order.awb_number},
        )
        return True
    except (IThinkRequestError, ValueError, SQLAlchemyError):
        db.session.rollback()
        try:
            order.shipping_status = 'Shipment pending'
            order.shipping_tracking_updated_at = datetime.utcnow()
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
        current_app.logger.exception(
            'Auto iThink shipment creation failed',
            extra={
                'order_id': order.id,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'total_amount': order.total_amount,
            },
        )
        return False


INVENTORY_TRACKED_PAYMENT_METHODS = frozenset({
    'cod',
    'gokwik_prepaid',
    'legacy_prepaid',
    'razorpay',
})
FULFILLMENT_STEPS = (
    'Order placed',
    'Confirmed',
    'Packed',
    'Shipped',
    'Out for delivery',
    'Delivered',
)


def _status_label(value: str | None, fallback: str) -> str:
    """Turn a stored status into a readable label without inventing a state."""
    normalized = str(value or '').strip()
    if not normalized:
        return fallback
    return normalized.replace('_', ' ').title()


def _payment_method_label(order: Order) -> str:
    payment_method = str(order.payment_method or '').strip().lower()
    if payment_method == 'cod':
        return 'Cash on Delivery'
    if payment_method == 'razorpay':
        return 'Prepaid / Razorpay'
    if payment_method == 'gokwik_prepaid':
        return 'Prepaid / GoKwik'
    if payment_method == 'legacy_prepaid':
        return 'Prepaid'
    return _status_label(payment_method, 'Payment method not recorded')


def _carrier_status_label(value: str | None) -> str | None:
    normalized = str(value or '').strip()
    if not normalized:
        return None
    if 'RTO' in normalized.upper():
        return 'Returned to sender'
    return _status_label(normalized, 'Carrier update')


def _fulfillment_stage(order: Order) -> int:
    """Return the furthest customer-facing fulfillment stage reached by an order."""
    local_status = str(order.status or '').strip().upper()
    carrier_status = str(order.shipping_status or '').strip().upper()

    if local_status == 'CANCELLED':
        return 0
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
    if order.awb_number or any(
        marker in carrier_status
        for marker in ('PACKED', 'MANIFEST', 'READY TO SHIP', 'PICKUP SCHEDULED', 'AWB')
    ):
        return 2
    if order.shipping_order_reference or local_status == 'PROCESSING' or any(
        marker in carrier_status
        for marker in ('NEW', 'ORDER CREATED', 'CONFIRMED', 'PROCESSING')
    ):
        return 1
    return 0


def _fulfillment_view(order: Order) -> dict[str, object]:
    """Build accurate presentation data for the authenticated customer's order card."""
    active_stage = _fulfillment_stage(order)
    latest_update = order.shipping_tracking_updated_at
    local_status = str(order.status or '').strip().lower()
    cancelled = local_status == 'cancelled'
    carrier_status = _carrier_status_label(order.shipping_status)
    returned_to_sender = 'RTO' in str(order.shipping_status or '').upper()
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

    if cancelled:
        display_status = 'Cancelled'
    elif returned_to_sender:
        display_status = 'Returned to sender'
    else:
        display_status = _status_label(order.status, 'Order placed')

    payment_status = str(order.payment_status or '').strip().lower()
    payment_status_label = {
        'paid': 'Paid',
        'unpaid': 'Awaiting payment',
        'failed': 'Payment failed',
        'refunded': 'Refunded',
    }.get(payment_status, _status_label(order.payment_status, 'Payment status not recorded'))
    payment_reference = order.razorpay_payment_id or order.gokwik_transaction_id
    shipment_failed = any(
        marker in str(order.shipping_status or '').strip().lower()
        for marker in ('fail', 'error', 'reject')
    )
    shipment_created = bool(
        (order.shipping_order_reference or order.awb_number)
        and not shipment_failed
    )
    if cancelled:
        shipment_badge = 'Cancelled'
    elif shipment_failed:
        shipment_badge = 'Creation failed'
    elif returned_to_sender:
        shipment_badge = 'Returned to sender'
    elif order.awb_number and not cancelled:
        shipment_badge = 'Tracking active'
    elif shipment_created:
        shipment_badge = 'Shipment created'
    else:
        shipment_badge = 'Not created'

    return {
        'steps': steps,
        'status': display_status,
        'status_class': 'cancelled' if cancelled else ('exception' if returned_to_sender else local_status),
        'cancelled': cancelled,
        'live': bool(order.awb_number and not cancelled and not shipment_failed),
        'shipment_created': shipment_created,
        'shipment_failed': shipment_failed,
        'shipment_badge': shipment_badge,
        'tracking_status': carrier_status,
        'tracking_status_code': order.shipping_status_code,
        'payment_method_label': _payment_method_label(order),
        'payment_status_label': payment_status_label,
        'payment_reference': payment_reference,
        'latest_update': latest_update,
    }


def _order_sync_token(order: Order) -> str:
    """Return the customer-visible state used by the lightweight live refresh."""
    values = (
        order.id,
        order.status,
        order.payment_status,
        order.payment_method,
        order.shipping_provider,
        order.awb_number,
        order.shipping_order_reference,
        order.shipping_status,
        order.shipping_status_code,
        order.shipping_tracking_url,
        order.shipping_tracking_updated_at.isoformat() if order.shipping_tracking_updated_at else '',
    )
    return '|'.join(str(value or '') for value in values)


def _orders_sync_signature(orders: list[Order]) -> str:
    state = '\n'.join(_order_sync_token(order) for order in orders)
    return hashlib.sha256(state.encode('utf-8')).hexdigest()


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


def is_cod_enabled() -> bool:
    """Return whether Cash on Delivery is available at checkout."""
    setting = Setting.query.filter_by(key='cod_enabled').first()
    if setting is None:
        return True
    return str(setting.value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _get_non_negative_setting_amount(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return Decimal('0.00')

    if not amount.is_finite() or amount < 0:
        return Decimal('0.00')
    return amount.quantize(Decimal('0.01'))


def calculate_shipping(subtotal, settings, payment_method='prepaid'):
    """Calculate shipping for a payment method using the configured rate."""
    is_cod = str(payment_method or '').strip().lower() == 'cod'
    charge_key = 'shipping_charge_cod' if is_cod else 'shipping_charge_prepaid'
    threshold_key = (
        'free_shipping_threshold_cod'
        if is_cod
        else 'free_shipping_threshold_prepaid'
    )
    shipping_charge = _get_non_negative_setting_amount(
        settings.get(charge_key, settings.get('shipping_charge'))
    )
    free_shipping_threshold = _get_non_negative_setting_amount(
        settings.get(threshold_key, settings.get('free_shipping_threshold'))
    )
    order_subtotal = _get_non_negative_setting_amount(subtotal)

    if free_shipping_threshold > 0 and order_subtotal >= free_shipping_threshold:
        return 0.0
    return float(shipping_charge)


def validate_checkout_details(form_values: dict[str, str]) -> str | None:
    """Validate and normalize customer data before creating a payable order."""
    required_fields = ('name', 'email', 'phone', 'address_line1', 'city', 'state', 'pincode')
    if any(not form_values.get(field) for field in required_fields):
        return 'Please complete your contact and shipping details.'
    if '@' not in form_values['email']:
        return 'Enter a valid email address.'
    try:
        form_values['phone'] = normalize_indian_phone(form_values['phone'])
        form_values['pincode'] = normalize_indian_pincode(form_values['pincode'])
    except ValueError as error:
        return str(error)
    return None


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
    pixel_contents = []
    pixel_num_items = 0
    total = 0
    for v_id, qty in cart.items():
        variant = ProductVariant.query.get(int(v_id))
        if variant:
            item_total = variant.price * qty
            total += item_total
            cart_items.append({'variant': variant, 'quantity': qty, 'total': item_total})
            pixel_contents.append({
                'id': str(variant.product_id),
                'quantity': qty,
                'item_price': float(variant.price),
            })
            pixel_num_items += qty
    return render_template(
        'main/cart.html',
        cart_items=cart_items,
        total=total,
        pixel_contents=pixel_contents,
        pixel_num_items=pixel_num_items,
        cod_enabled=is_cod_enabled(),
    )

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
    response = make_response(render_template(
        'main/account.html',
        orders=orders,
        fulfillment_by_order=fulfillment_by_order,
        order_sync_signature=_orders_sync_signature(orders),
    ))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@main_bp.get('/account/orders/updates')
@login_required
def account_order_updates():
    """Expose customer-owned order state for fast, cache-free UI refreshes."""
    _link_guest_orders_to_user(current_user)
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    response = jsonify({
        'signature': _orders_sync_signature(orders),
        'checked_at': datetime.utcnow().isoformat(timespec='seconds') + 'Z',
    })
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response

# --- CHECKOUT ---
def reserve_order_inventory(lines):
    """Deduct stock for an order while holding row locks for the transaction."""
    quantities = {}
    for line in lines:
        variant = line['variant']
        quantities[variant.id] = quantities.get(variant.id, 0) + line['quantity']

    variants = (
        ProductVariant.query
        .filter(ProductVariant.id.in_(sorted(quantities)))
        .order_by(ProductVariant.id)
        .with_for_update()
        .all()
    )
    if len(variants) != len(quantities):
        raise RuntimeError('Cannot place the order because an ordered item is missing.')

    for variant in variants:
        quantity = quantities[variant.id]
        if (variant.stock_quantity or 0) < quantity:
            raise RuntimeError(
                f'Only {variant.stock_quantity or 0} unit(s) of {variant.product.name} are available.'
            )

    for variant in variants:
        variant.stock_quantity = (variant.stock_quantity or 0) - quantities[variant.id]


def release_order_inventory(order):
    """Restore inventory for a newly cancelled order exactly once per cancellation."""
    if order.payment_method not in INVENTORY_TRACKED_PAYMENT_METHODS:
        return
    if order.payment_method == 'razorpay' and order.payment_status != 'paid':
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


def get_razorpay_client():
    key_id = current_app.config.get('RAZORPAY_KEY_ID')
    key_secret = current_app.config.get('RAZORPAY_KEY_SECRET')
    if not key_id or not key_secret:
        raise RuntimeError('Razorpay API credentials are not configured.')
    return razorpay.Client(auth=(key_id, key_secret))


def _razorpay_cart_lines():
    cart = session.get('cart', {})
    if not isinstance(cart, dict) or not cart:
        raise ValueError('Your cart is empty.')

    lines = []
    for raw_variant_id, raw_quantity in cart.items():
        try:
            variant_id = int(raw_variant_id)
            quantity = int(raw_quantity)
        except (TypeError, ValueError) as error:
            raise ValueError('Your cart contains invalid data.') from error
        if quantity <= 0:
            raise ValueError('Your cart contains an invalid quantity.')

        variant = ProductVariant.query.filter_by(id=variant_id).first()
        if not variant:
            raise ValueError('A cart item is no longer available.')
        if (variant.stock_quantity or 0) < quantity:
            raise ValueError(
                f'Only {variant.stock_quantity or 0} unit(s) of {variant.product.name} are available.'
            )
        lines.append({
            'variant': variant,
            'quantity': quantity,
            'line_total': Decimal(str(variant.price)) * quantity,
        })
    return lines


def _razorpay_totals(lines, coupon_code='', payment_method='razorpay'):
    subtotal = sum((line['line_total'] for line in lines), Decimal('0.00'))
    settings = {setting.key: setting.value for setting in Setting.query.all()}
    shipping = Decimal(str(calculate_shipping(
        float(subtotal), settings, payment_method,
    ))).quantize(Decimal('0.01'))
    discount = Decimal('0.00')
    coupon_id = None
    if coupon_code:
        result = _validate_coupon(coupon_code, float(subtotal))
        if not result.get('valid'):
            raise ValueError(str(result.get('message') or 'Coupon is not valid.'))
        discount = Decimal(str(result['discount'])).quantize(Decimal('0.01'))
        coupon_id = int(result['coupon_id'])
    total = subtotal - discount + shipping
    if total <= 0:
        raise ValueError('The order total must be greater than zero.')
    return subtotal, discount, shipping, total, coupon_id


def _razorpay_amount_in_paise(amount):
    """Convert a local rupee amount to Razorpay paise consistently."""
    try:
        money = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError('The order amount is invalid.') from error
    if not money.is_finite() or money <= 0:
        raise ValueError('The order amount must be greater than zero.')
    return max(100, int(money * 100))


def _razorpay_provider_amount(value, field_name):
    """Parse a provider amount expressed in paise without accepting fractions."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f'Razorpay returned an invalid {field_name}.') from error
    if not amount.is_finite() or amount != amount.to_integral_value() or amount < 0:
        raise ValueError(f'Razorpay returned an invalid {field_name}.')
    return int(amount)


def _razorpay_checkout_context(lines, coupon_code='', payment_method='razorpay'):
    subtotal, discount, shipping, total, _ = _razorpay_totals(
        lines,
        coupon_code,
        payment_method,
    )
    settings = {setting.key: setting.value for setting in Setting.query.all()}
    shipping_options = {
        'cod': calculate_shipping(float(subtotal), settings, 'cod'),
        'prepaid': calculate_shipping(float(subtotal), settings, 'razorpay'),
    }
    return {
        'items': lines,
        'subtotal': subtotal,
        'discount': discount,
        'shipping': shipping,
        'total': total,
        'shipping_options': shipping_options,
        'payment_method': payment_method,
        'coupon_code': coupon_code,
        'cod_enabled': is_cod_enabled(),
        'submission_token': _checkout_submission_token(),
    }


def mark_order_as_paid(razorpay_order_id, razorpay_payment_id, payment_amount=None):
    order = (
        Order.query
        .filter_by(razorpay_order_id=razorpay_order_id)
        .with_for_update()
        .first()
    )
    if not order:
        return None

    if payment_amount is not None:
        expected_amount = _razorpay_amount_in_paise(order.total_amount)
        provider_amount = _razorpay_provider_amount(payment_amount, 'payment amount')
        if provider_amount != expected_amount:
            raise ValueError('Razorpay payment amount does not match the local order.')

    existing_payment = Order.query.filter_by(razorpay_payment_id=razorpay_payment_id).first()
    if existing_payment and existing_payment.id != order.id:
        raise ValueError('Razorpay payment is already linked to another order.')

    if order.payment_status == 'paid':
        if order.razorpay_payment_id != razorpay_payment_id:
            raise ValueError('The Razorpay payment does not match the paid order.')
        return order
    if order.payment_status == 'refunded':
        raise ValueError('A refunded Razorpay order cannot be marked paid again.')

    quantities = {}
    for item in order.items:
        quantities[item.variant_id] = quantities.get(item.variant_id, 0) + item.quantity
    variants = (
        ProductVariant.query
        .filter(ProductVariant.id.in_(sorted(quantities)))
        .with_for_update()
        .all()
    )
    if len(variants) != len(quantities):
        raise RuntimeError('Cannot confirm payment because an ordered item is missing.')
    for variant in variants:
        quantity = quantities[variant.id]
        if (variant.stock_quantity or 0) < quantity:
            raise RuntimeError(
                f'Cannot confirm payment because {variant.product.name} is out of stock.'
            )

    for variant in variants:
        variant.stock_quantity = (variant.stock_quantity or 0) - quantities[variant.id]

    order.payment_status = 'paid'
    order.status = 'processing'
    order.payment_method = 'razorpay'
    order.razorpay_payment_id = razorpay_payment_id

    if order.coupon_id:
        coupon = Coupon.query.filter_by(id=order.coupon_id).with_for_update().first()
        if not coupon or (coupon.max_uses and coupon.used_count >= coupon.max_uses):
            raise RuntimeError('Cannot confirm payment because the coupon is no longer valid.')
        coupon.used_count = (coupon.used_count or 0) + 1

    db.session.commit()
    _auto_create_ithink_shipment(order)
    return order


def mark_order_payment_failed(razorpay_order_id, razorpay_payment_id):
    """Persist a verified Razorpay failure without overwriting a paid order."""
    order = Order.query.filter_by(razorpay_order_id=razorpay_order_id).first()
    if not order:
        return None
    if order.payment_status != 'paid':
        order.payment_status = 'failed'
        order.payment_method = 'razorpay'
        if razorpay_payment_id:
            order.razorpay_payment_id = razorpay_payment_id
        db.session.commit()
    return order


def mark_order_as_refunded(razorpay_payment_id):
    """Persist a verified Razorpay refund against its captured payment."""
    order = Order.query.filter_by(razorpay_payment_id=razorpay_payment_id).first()
    if not order:
        return None
    order.payment_status = 'refunded'
    db.session.commit()
    return order


def reconcile_razorpay_order(order: Order):
    """Reconcile a locally unpaid order against Razorpay's captured payment record."""
    if order.payment_status == 'paid':
        return order
    if not order.razorpay_order_id:
        raise ValueError('This order has no Razorpay order identifier.')

    client = get_razorpay_client()
    remote_order = client.order.fetch(order.razorpay_order_id)
    expected_amount = _razorpay_amount_in_paise(order.total_amount)
    remote_status = str(remote_order.get('status') or '').strip().lower()
    remote_currency = str(remote_order.get('currency') or '').strip().upper()
    remote_order_amount = _razorpay_provider_amount(
        remote_order.get('amount'),
        'order amount',
    )
    remote_amount_paid = _razorpay_provider_amount(
        remote_order.get('amount_paid'),
        'amount paid',
    )

    if remote_order_amount is not None and remote_order_amount != expected_amount:
        current_app.logger.error(
            'Razorpay order amount does not match the local order during reconciliation.',
            extra={
                'order_id': order.id,
                'razorpay_order_id': order.razorpay_order_id,
                'local_amount_paise': expected_amount,
                'provider_order_amount_paise': remote_order_amount,
                'provider_status': remote_status,
            },
        )
        raise ValueError(
            'Razorpay order amount does not match the local order '
            f'(local={expected_amount} paise, provider={remote_order_amount} paise).'
        )
    if remote_currency and remote_currency != 'INR':
        raise ValueError(
            f'Razorpay order currency does not match INR (provider={remote_currency}).'
        )
    if remote_amount_paid not in (None, 0, expected_amount):
        current_app.logger.error(
            'Razorpay paid amount does not match the local order during reconciliation.',
            extra={
                'order_id': order.id,
                'razorpay_order_id': order.razorpay_order_id,
                'local_amount_paise': expected_amount,
                'provider_amount_paid_paise': remote_amount_paid,
                'provider_status': remote_status,
            },
        )
        raise ValueError(
            'Razorpay paid amount does not match the local order '
            f'(local={expected_amount} paise, provider={remote_amount_paid} paise).'
        )

    payment_response = client.order.payments(order.razorpay_order_id) or {}
    payments = payment_response.get('items', [])
    captured_payment = None
    for payment in payments if isinstance(payments, list) else []:
        if not isinstance(payment, dict):
            continue
        if (
            not payment.get('id')
            or payment.get('order_id') != order.razorpay_order_id
            or str(payment.get('status') or '').strip().lower() != 'captured'
            or str(payment.get('currency') or '').strip().upper() not in ('', 'INR')
        ):
            continue
        try:
            payment_amount = _razorpay_provider_amount(payment.get('amount'), 'payment amount')
        except ValueError:
            current_app.logger.warning(
                'Ignoring Razorpay payment with an invalid amount during reconciliation.',
                extra={
                    'order_id': order.id,
                    'razorpay_order_id': order.razorpay_order_id,
                    'razorpay_payment_id': payment.get('id'),
                },
            )
            continue
        if payment_amount == expected_amount:
            captured_payment = payment
            break
    if not captured_payment:
        current_app.logger.error(
            'Razorpay has no captured payment matching the local order.',
            extra={
                'order_id': order.id,
                'razorpay_order_id': order.razorpay_order_id,
                'local_amount_paise': expected_amount,
                'provider_status': remote_status,
                'provider_amount_paid_paise': remote_amount_paid,
            },
        )
        raise ValueError(
            'Razorpay has no captured payment matching the local order '
            f'(expected={expected_amount} paise, provider_status={remote_status or "missing"}, '
            f'provider_amount_paid={remote_amount_paid if remote_amount_paid is not None else "missing"}).'
        )

    if remote_status != 'paid' or remote_amount_paid == 0:
        current_app.logger.warning(
            'Razorpay order snapshot lagged its captured payment; reconciling from the payment record.',
            extra={
                'order_id': order.id,
                'razorpay_order_id': order.razorpay_order_id,
                'local_amount_paise': expected_amount,
                'provider_status': remote_status,
                'provider_amount_paid_paise': remote_amount_paid,
            },
        )
    return mark_order_as_paid(
        order.razorpay_order_id,
        str(captured_payment['id']),
        captured_payment.get('amount'),
    )


def _finish_razorpay_browser_payment(order):
    _remember_guest_order(order)
    if current_user.is_authenticated:
        _link_guest_orders_to_user(current_user)

    purchase_contents = [
        {
            'id': str(item.variant.product_id),
            'quantity': item.quantity,
            'item_price': float(item.price_at_time),
        }
        for item in order.items
    ]
    session['meta_pixel_purchase'] = {
        'event_id': f'purchase-{order.id}',
        'value': float(order.total_amount),
        'currency': 'INR',
        'content_ids': [item['id'] for item in purchase_contents],
        'contents': purchase_contents,
        'content_type': 'product',
        'num_items': sum(item['quantity'] for item in purchase_contents),
    }
    session['cart'] = {}
    session.pop(CHECKOUT_SUBMISSION_SESSION_KEY, None)
    session['order_confirmation_id'] = order.id


@main_bp.get('/order/confirmation/<int:order_id>')
def order_confirmation(order_id: int):
    """Render the confirmation for the order just completed in this browser."""
    order = db.session.get(Order, order_id)
    if not order or session.get('order_confirmation_id') != order.id:
        abort(404)

    if current_user.is_authenticated:
        _link_guest_orders_to_user(current_user)
        if order.user_id != current_user.id:
            abort(404)
    else:
        remembered_ids = session.get(GUEST_ORDER_SESSION_KEY, [])
        if order.user_id is not None or order.id not in remembered_ids:
            abort(404)

    if order.payment_method != 'cod' and order.payment_status != 'paid':
        abort(404)

    response = make_response(render_template(
        'main/order_confirmation.html',
        order=order,
        estimated_delivery=_estimated_delivery_window(order),
    ))
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    return response


@main_bp.route('/api/validate-coupon', methods=['POST'])
def api_validate_coupon():
    data = request.json
    code = data.get('code', '').strip().upper()
    total = data.get('total', 0)
    result = _validate_coupon(code, total)
    return result


@main_bp.route('/razorpay/checkout', methods=['GET', 'POST'])
def razorpay_checkout():
    online_payment_enabled = bool(current_app.config.get('RAZORPAY_ENABLED'))
    cod_enabled = is_cod_enabled()
    if not online_payment_enabled and not cod_enabled:
        flash('Checkout is temporarily unavailable. Please try again shortly.', 'error')
        return redirect(url_for('main.cart'))

    if request.method == 'POST':
        submitted_token = request.form.get('checkout_token', '').strip()
        if submitted_token:
            existing_order = Order.query.filter_by(checkout_token=submitted_token).first()
            if existing_order:
                if (
                    existing_order.payment_method == 'cod'
                    or existing_order.payment_status == 'paid'
                ):
                    _finish_razorpay_browser_payment(existing_order)
                    return redirect(url_for(
                        'main.order_confirmation',
                        order_id=existing_order.id,
                    ))
                if existing_order.razorpay_order_id:
                    return render_template(
                        'main/razorpay_payment.html',
                        order=existing_order,
                        razorpay_order_id=existing_order.razorpay_order_id,
                        key_id=current_app.config['RAZORPAY_KEY_ID'],
                        amount=_razorpay_amount_in_paise(existing_order.total_amount),
                    )

    try:
        lines = _razorpay_cart_lines()
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('main.cart'))

    form_values = {
        'name': (current_user.name or '') if current_user.is_authenticated else '',
        'email': (current_user.email or '') if current_user.is_authenticated else '',
        'phone': (current_user.phone or '') if current_user.is_authenticated else '',
        'address_line1': (current_user.address_line1 or '') if current_user.is_authenticated else '',
        'address_line2': (current_user.address_line2 or '') if current_user.is_authenticated else '',
        'city': (current_user.city or '') if current_user.is_authenticated else '',
        'state': (current_user.state or '') if current_user.is_authenticated else '',
        'pincode': (current_user.pincode or '') if current_user.is_authenticated else '',
        'country': (current_user.country or 'India') if current_user.is_authenticated else 'India',
        'coupon_code': '',
        'payment_method': 'razorpay' if online_payment_enabled else 'cod',
    }

    if request.method == 'GET':
        try:
            context = _razorpay_checkout_context(lines, payment_method=form_values['payment_method'])
        except ValueError as error:
            flash(str(error), 'error')
            return redirect(url_for('main.cart'))
        return render_template('main/razorpay_checkout.html', **context, form_values=form_values)

    for field in form_values:
        form_values[field] = request.form.get(field, '').strip()
    form_values['country'] = form_values['country'] or 'India'
    submission_token = request.form.get('checkout_token', '').strip()
    if not submission_token:
        submission_token = _checkout_submission_token()
    stored_submission_token = session.get(CHECKOUT_SUBMISSION_SESSION_KEY)
    if (
        stored_submission_token
        and submission_token != stored_submission_token
    ):
        flash('This checkout session has expired. Please start checkout again.', 'error')
        return redirect(url_for('main.razorpay_checkout'))
    session[CHECKOUT_SUBMISSION_SESSION_KEY] = submission_token
    payment_method = form_values['payment_method'].lower() or (
        'razorpay' if online_payment_enabled else 'cod'
    )
    form_values['payment_method'] = payment_method

    if payment_method not in {'cod', 'razorpay'}:
        flash('Please choose a valid payment method.', 'error')
        form_values['payment_method'] = 'razorpay' if online_payment_enabled else 'cod'
        try:
            context = _razorpay_checkout_context(
                lines,
                form_values['coupon_code'].upper(),
                form_values['payment_method'],
            )
        except ValueError:
            context = _razorpay_checkout_context(lines, payment_method=form_values['payment_method'])
        return render_template(
            'main/razorpay_checkout.html',
            **context,
            form_values=form_values,
        )
    if payment_method == 'cod' and not cod_enabled:
        flash('Cash on Delivery is currently unavailable.', 'error')
        form_values['payment_method'] = 'razorpay' if online_payment_enabled else 'cod'
        try:
            context = _razorpay_checkout_context(
                lines,
                form_values['coupon_code'].upper(),
                form_values['payment_method'],
            )
        except ValueError:
            context = _razorpay_checkout_context(lines, payment_method=form_values['payment_method'])
        return render_template(
            'main/razorpay_checkout.html',
            **context,
            form_values=form_values,
        )
    if payment_method == 'razorpay' and not online_payment_enabled:
        flash('Online payment is temporarily unavailable. Please choose Cash on Delivery.', 'error')
        form_values['payment_method'] = 'cod'
        try:
            context = _razorpay_checkout_context(
                lines,
                form_values['coupon_code'].upper(),
                form_values['payment_method'],
            )
        except ValueError:
            context = _razorpay_checkout_context(lines, payment_method=form_values['payment_method'])
        return render_template(
            'main/razorpay_checkout.html',
            **context,
            form_values=form_values,
        )

    validation_error = validate_checkout_details(form_values)
    if validation_error:
        flash(validation_error, 'error')
        try:
            context = _razorpay_checkout_context(
                lines,
                form_values['coupon_code'].upper(),
                form_values['payment_method'],
            )
        except ValueError:
            context = _razorpay_checkout_context(lines, payment_method=form_values['payment_method'])
        return render_template(
            'main/razorpay_checkout.html',
            **context,
            form_values=form_values,
        )

    coupon_code = form_values['coupon_code'].upper()
    try:
        _, _, shipping, total, coupon_id = _razorpay_totals(
            lines,
            coupon_code,
            payment_method,
        )
    except ValueError as error:
        flash(str(error), 'error')
        try:
            context = _razorpay_checkout_context(
                lines,
                payment_method=form_values['payment_method'],
            )
        except ValueError:
            return redirect(url_for('main.cart'))
        return render_template(
            'main/razorpay_checkout.html',
            **context,
            form_values=form_values,
        )

    try:
        order = Order(
            user_id=current_user.id if current_user.is_authenticated else None,
            customer_name=form_values['name'],
            customer_email=form_values['email'].lower(),
            customer_phone=form_values['phone'],
            shipping_address=', '.join(filter(None, [
                form_values['address_line1'], form_values['address_line2'], form_values['city'],
                form_values['state'], form_values['pincode'], form_values['country'],
            ])),
            address_line1=form_values['address_line1'],
            address_line2=form_values['address_line2'],
            city=form_values['city'],
            state=form_values['state'],
            pincode=form_values['pincode'],
            country=form_values['country'],
            total_amount=float(total),
            shipping_charges=float(shipping),
            status='pending',
            payment_status='unpaid',
            payment_method=payment_method,
            checkout_token=submission_token,
            coupon_id=coupon_id,
        )
        db.session.add(order)
        db.session.flush()
        for line in lines:
            db.session.add(OrderItem(
                order_id=order.id,
                variant_id=line['variant'].id,
                quantity=line['quantity'],
                price_at_time=float(line['variant'].price),
            ))

        razorpay_amount = None
        razorpay_order_id = None
        if payment_method == 'cod':
            reserve_order_inventory(lines)
            if coupon_id:
                coupon = Coupon.query.filter_by(id=coupon_id).with_for_update().first()
                if not coupon or (coupon.max_uses and coupon.used_count >= coupon.max_uses):
                    raise RuntimeError('Cannot place the order because the coupon is no longer valid.')
                coupon.used_count = (coupon.used_count or 0) + 1
        else:
            razorpay_amount = _razorpay_amount_in_paise(total)
            razorpay_order = get_razorpay_client().order.create({
                'amount': razorpay_amount,
                'currency': 'INR',
                'payment_capture': 1,
                'notes': {'local_order_id': str(order.id)},
            })
            razorpay_order_id = str(razorpay_order.get('id') or '').strip()
            if not razorpay_order_id:
                raise RuntimeError('Razorpay returned no order identifier.')
            order.razorpay_order_id = razorpay_order_id

        if current_user.is_authenticated:
            current_user.phone = order.customer_phone
            current_user.address_line1 = order.address_line1
            current_user.address_line2 = order.address_line2
            current_user.city = order.city
            current_user.state = order.state
            current_user.pincode = order.pincode
            current_user.country = order.country
        db.session.commit()
    except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError, razorpay.errors.ServerError):
        db.session.rollback()
        current_app.logger.exception('Unable to create Razorpay order.')
        flash('Online payment is currently unavailable. Please try again shortly.', 'error')
        return redirect(url_for('main.cart'))
    except IntegrityError:
        db.session.rollback()
        existing_order = Order.query.filter_by(checkout_token=submission_token).first()
        if existing_order:
            if (
                existing_order.payment_method == 'cod'
                or existing_order.payment_status == 'paid'
            ):
                _finish_razorpay_browser_payment(existing_order)
                return redirect(url_for(
                    'main.order_confirmation',
                    order_id=existing_order.id,
                ))
            if existing_order.razorpay_order_id:
                return render_template(
                    'main/razorpay_payment.html',
                    order=existing_order,
                    razorpay_order_id=existing_order.razorpay_order_id,
                    key_id=current_app.config['RAZORPAY_KEY_ID'],
                    amount=_razorpay_amount_in_paise(existing_order.total_amount),
                )
        current_app.logger.exception('Checkout submission conflicted with an existing order.')
        flash('Unable to prepare your order. Please try again.', 'error')
        return redirect(url_for('main.cart'))
    except (RuntimeError, SQLAlchemyError, ValueError):
        db.session.rollback()
        current_app.logger.exception('Unable to prepare Razorpay order.')
        flash('Unable to prepare your order. Please try again.', 'error')
        return redirect(url_for('main.cart'))

    if payment_method == 'cod':
        _auto_create_ithink_shipment(order)
        _finish_razorpay_browser_payment(order)
        return redirect(url_for('main.order_confirmation', order_id=order.id))

    return render_template(
        'main/razorpay_payment.html',
        order=order,
        razorpay_order_id=razorpay_order_id,
        key_id=current_app.config['RAZORPAY_KEY_ID'],
        amount=razorpay_amount,
    )


@main_bp.route('/payment/verify', methods=['POST'])
def verify_payment():
    razorpay_payment_id = request.form.get('razorpay_payment_id', '').strip()
    razorpay_order_id = request.form.get('razorpay_order_id', '').strip()
    razorpay_signature = request.form.get('razorpay_signature', '').strip()
    payment_url = url_for('main.razorpay_checkout')

    if not all((razorpay_payment_id, razorpay_order_id, razorpay_signature)):
        flash('Invalid payment response. Please try again or contact support.', 'error')
        return redirect(payment_url)

    try:
        client = get_razorpay_client()
        client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature,
        })
        order = Order.query.filter_by(razorpay_order_id=razorpay_order_id).first()
        if not order:
            flash('Order not found. Please contact support with your payment ID.', 'error')
            return redirect(payment_url)

        payment = client.payment.fetch(razorpay_payment_id)
        if (
            payment.get('id') != razorpay_payment_id
            or payment.get('order_id') != razorpay_order_id
            or payment.get('currency') not in (None, 'INR')
        ):
            raise ValueError('Razorpay payment did not match the local order.')

        payment_status = payment.get('status')
        if payment_status == 'authorized':
            client.payment.capture(razorpay_payment_id, payment['amount'])
            payment_status = 'captured'
        if payment_status != 'captured':
            flash('Payment could not be confirmed. Please try again.', 'error')
            return redirect(payment_url)

        order = mark_order_as_paid(
            razorpay_order_id,
            razorpay_payment_id,
            payment.get('amount'),
        )
        if not order:
            flash('Order not found. Please contact support with your payment ID.', 'error')
            return redirect(payment_url)
        _finish_razorpay_browser_payment(order)
        return redirect(url_for('main.order_confirmation', order_id=order.id))
    except razorpay.errors.SignatureVerificationError:
        current_app.logger.warning(
            'Rejected Razorpay payment signature.',
            extra={'razorpay_order_id': razorpay_order_id, 'razorpay_payment_id': razorpay_payment_id},
        )
        flash('Payment verification failed. Please contact support if you were charged.', 'error')
    except (razorpay.errors.BadRequestError, razorpay.errors.GatewayError, razorpay.errors.ServerError, RuntimeError, SQLAlchemyError, ValueError):
        db.session.rollback()
        current_app.logger.exception(
            'Unable to verify Razorpay payment.',
            extra={'razorpay_order_id': razorpay_order_id, 'razorpay_payment_id': razorpay_payment_id},
        )
        flash('Payment could not be confirmed. Please contact support if you were charged.', 'error')
    return redirect(payment_url)


@main_bp.route('/payment/webhook', methods=['POST'])
def payment_webhook():
    webhook_body = request.get_data(cache=True, as_text=True)
    webhook_signature = request.headers.get('X-Razorpay-Signature', '').strip()
    webhook_secret = current_app.config.get('RAZORPAY_WEBHOOK_SECRET')
    if not webhook_secret or not webhook_signature:
        return jsonify({'status': 'invalid signature'}), 400

    try:
        get_razorpay_client().utility.verify_webhook_signature(
            webhook_body, webhook_signature, webhook_secret
        )
    except razorpay.errors.SignatureVerificationError:
        current_app.logger.warning('Rejected Razorpay webhook with invalid signature.')
        return jsonify({'status': 'invalid signature'}), 400

    try:
        payload = json.loads(webhook_body)
    except json.JSONDecodeError:
        return jsonify({'status': 'invalid payload'}), 400

    event = payload.get('event')
    payment = payload.get('payload', {}).get('payment', {}).get('entity', {})
    razorpay_order = payload.get('payload', {}).get('order', {}).get('entity', {})
    refund = payload.get('payload', {}).get('refund', {}).get('entity', {})
    if event == 'payment.failed':
        razorpay_order_id = payment.get('order_id')
        razorpay_payment_id = payment.get('id')
        if not razorpay_order_id or not razorpay_payment_id:
            return jsonify({'status': 'invalid payload'}), 400
        try:
            order = mark_order_payment_failed(
                str(razorpay_order_id),
                str(razorpay_payment_id),
            )
            if not order:
                current_app.logger.warning(
                    'Razorpay failure webhook referenced an unknown order.',
                    extra={'razorpay_order_id': razorpay_order_id},
                )
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception(
                'Unable to persist Razorpay payment failure.',
                extra={'razorpay_order_id': razorpay_order_id},
            )
            return jsonify({'status': 'temporary error'}), 500
        return jsonify({'status': 'ok'}), 200
    if event == 'refund.created':
        razorpay_payment_id = refund.get('payment_id')
        if not razorpay_payment_id:
            return jsonify({'status': 'invalid payload'}), 400
        try:
            order = mark_order_as_refunded(str(razorpay_payment_id))
            if not order:
                current_app.logger.warning(
                    'Razorpay refund webhook referenced an unknown payment.',
                    extra={'razorpay_payment_id': razorpay_payment_id},
                )
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception(
                'Unable to persist Razorpay refund.',
                extra={'razorpay_payment_id': razorpay_payment_id},
            )
            return jsonify({'status': 'temporary error'}), 500
        return jsonify({'status': 'ok'}), 200
    if event == 'payment.captured':
        razorpay_order_id = payment.get('order_id')
    elif event == 'order.paid':
        razorpay_order_id = razorpay_order.get('id')
    else:
        return jsonify({'status': 'ignored'}), 200

    razorpay_payment_id = payment.get('id')
    if not razorpay_order_id or not razorpay_payment_id:
        return jsonify({'status': 'invalid payload'}), 400

    try:
        order = mark_order_as_paid(
            str(razorpay_order_id),
            str(razorpay_payment_id),
            payment.get('amount') or razorpay_order.get('amount'),
        )
        if not order:
            current_app.logger.warning(
                'Razorpay webhook referenced an unknown order.',
                extra={'razorpay_order_id': razorpay_order_id},
            )
    except (RuntimeError, SQLAlchemyError, ValueError):
        db.session.rollback()
        current_app.logger.exception(
            'Unable to reconcile Razorpay webhook.',
            extra={'razorpay_order_id': razorpay_order_id, 'razorpay_payment_id': razorpay_payment_id},
        )
        return jsonify({'status': 'temporary error'}), 500
    return jsonify({'status': 'ok'}), 200


@main_bp.route('/checkout', methods=['GET'])
def checkout():
    """Redirect legacy checkout URLs to the cart's available secure checkouts."""
    if not session.get('cart'):
        return redirect(url_for('main.shop'))
    if current_app.config.get('RAZORPAY_ENABLED'):
        flash('Use the secure Razorpay checkout button in your cart to complete this purchase.', 'info')
    else:
        flash('Secure checkout is temporarily unavailable. Please try again shortly.', 'error')
    return redirect(url_for('main.cart'))
# End of public checkout routes.
