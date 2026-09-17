from flask import Blueprint, current_app, jsonify, render_template, request, redirect, url_for, flash, session
from models import db, Product, Category, ProductVariant, Review, User, Order, OrderItem, Setting, Coupon, OfferBanner
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import json
import secrets
import time
import requests
from decimal import Decimal, InvalidOperation

from datetime import datetime

main_bp = Blueprint('main', __name__)

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
    # Homepage media
    media_url = (Setting.query.filter_by(key='homepage_media_url').first() or Setting(value='')).value
    media_type = (Setting.query.filter_by(key='homepage_media_type').first() or Setting(value='')).value
    bottom_banner_url = (Setting.query.filter_by(key='bottom_banner_url').first() or Setting(value='')).value
    return render_template('index.html', best_sellers=best_sellers, categories=categories, offers=offers, hero_products=hero_products, media_url=media_url, media_type=media_type, bottom_banner_url=bottom_banner_url)

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
    return render_template('main/about.html')

@main_bp.route('/contact')
def contact():
    return render_template('main/contact.html')

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
    flash('Thank you for your exquisite review!', 'success')
    return redirect(url_for('main.product_detail', slug=slug))

@main_bp.route('/shop')
def shop():
    cat_slug = request.args.get('cat')
    sort = request.args.get('sort', 'newest')
    on_sale = request.args.get('on_sale', type=int)
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    search_query = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 8
    
    query = Product.query
    
    if search_query:
        query = query.filter(
            (Product.name.ilike(f'%{search_query}%')) | 
            (Product.short_description.ilike(f'%{search_query}%')) |
            (Product.full_description.ilike(f'%{search_query}%')) |
            (Product.tag.ilike(f'%{search_query}%'))
        )
        
    active_category = None
    if cat_slug:
        active_category = Category.query.filter_by(slug=cat_slug).first()
        if active_category:
            query = query.filter_by(category_id=active_category.id)
            
    if on_sale == 1:
        query = query.join(ProductVariant).filter(ProductVariant.original_price > ProductVariant.price)
    if min_price is not None: query = query.join(ProductVariant, isouter=True, aliased=True).filter(ProductVariant.price >= min_price)
    if max_price is not None: query = query.join(ProductVariant, isouter=True, aliased=True).filter(ProductVariant.price <= max_price)
    if sort == 'price_low': query = query.join(ProductVariant, isouter=True).order_by(ProductVariant.price.asc()).distinct()
    elif sort == 'price_high': query = query.join(ProductVariant, isouter=True).order_by(ProductVariant.price.desc()).distinct()
    else: query = query.order_by(Product.created_at.desc())
    
    pagination = query.paginate(page=page, per_page=per_page, error_out=False)
    products = pagination.items
    categories = Category.query.all()
    return render_template('shop.html', categories=categories, products=products, active_cat=cat_slug, active_sort=sort, active_sale=on_sale, active_min_price=min_price, active_max_price=max_price, search_query=search_query, pagination=pagination, page=page)

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

@main_bp.route('/cart/remove/<int:variant_id>')
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

# --- AUTH ---
@main_bp.route('/account/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        next_url = request.args.get('next', '')
        if next_url and next_url.startswith('/'):
            return redirect(next_url)
        return redirect(url_for('main.account'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        next_url = request.form.get('next', '').strip()
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            # Only redirect to safe internal URLs
            if next_url and next_url.startswith('/'):
                return redirect(next_url)
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
        'next_url': next_url if next_url.startswith('/') else ''
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
    next_url = otp_data.get('next_url', '')
    return redirect(next_url if next_url.startswith('/') else url_for('main.account'))

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
        email = request.form.get('email')
        name = request.form.get('name')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')
        
        if password != confirm_password:
            flash('Passwords do not match', 'error')
        elif User.query.filter_by(email=email).first():
            flash('Email already exists', 'error')
        else:
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
            new_user = User(email=email, name=name, password=hashed_pw, role='user')
            db.session.add(new_user)
            db.session.commit()
            login_user(new_user)
            return redirect(url_for('main.account'))
    return render_template('main/register.html')

@main_bp.route('/account/logout')
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

    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template('main/account.html', orders=orders)

# --- CHECKOUT ---
import razorpay
from flask import current_app

def get_razorpay_client():
    return razorpay.Client(auth=(current_app.config['RAZORPAY_KEY_ID'], current_app.config['RAZORPAY_KEY_SECRET']))

@main_bp.route('/api/validate-coupon', methods=['POST'])
def api_validate_coupon():
    data = request.json
    code = data.get('code', '').strip().upper()
    total = data.get('total', 0)
    result = _validate_coupon(code, total)
    return result

@main_bp.route('/checkout', methods=['GET', 'POST'])
def checkout():
    cart = session.get('cart', {})
    if not cart: return redirect(url_for('main.shop'))

    total = 0
    items = []
    for v_id, qty in cart.items():
        variant = ProductVariant.query.get(int(v_id))
        if variant:
            total += variant.price * qty
            items.append({'v': variant, 'q': qty})

    # Load payment method toggles from settings
    settings = {s.key: s.value for s in Setting.query.all()}
    cod_enabled    = settings.get('payment_cod_enabled', '1') == '1'
    online_enabled = settings.get('payment_online_enabled', '1') == '1'

    actual_shipping = calculate_shipping(total, settings)

    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        phone = request.form.get('phone')
        address_line1 = request.form.get('address_line1', '').strip()
        address_line2 = request.form.get('address_line2', '').strip()
        city          = request.form.get('city', '').strip()
        state         = request.form.get('state', '').strip()
        pincode       = request.form.get('pincode', '').strip()
        country       = request.form.get('country', 'India').strip()
        coupon_code   = request.form.get('coupon_code', '').strip().upper()
        payment_method = request.form.get('payment_method', 'cod')

        if current_user.is_authenticated:
            current_user.phone = phone
            current_user.address_line1 = address_line1
            current_user.address_line2 = address_line2
            current_user.city = city
            current_user.state = state
            current_user.pincode = pincode
            current_user.country = country
            db.session.commit()

        final_total = total + actual_shipping
        discount = 0.0

        applied_coupon_id = None
        # Apply coupon if given
        if coupon_code:
            result = _validate_coupon(coupon_code, total)
            if result['valid']:
                discount = result['discount']
                final_total = result['final_total'] + actual_shipping
                applied_coupon_id = result.get('coupon_id')
                # Increment early only if COD
                if payment_method == 'cod' and applied_coupon_id:
                    c = Coupon.query.get(applied_coupon_id)
                    if c:
                        c.used_count += 1
            else:
                flash(result['message'], 'error')
                return redirect(url_for('main.checkout'))

        full_address = ', '.join(filter(None, [address_line1, address_line2, city, state, pincode, country]))

        order = Order(
            user_id=current_user.id if current_user.is_authenticated else None,
            customer_name=name,
            customer_email=email,
            customer_phone=phone,
            shipping_address=full_address,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            state=state,
            pincode=pincode,
            country=country,
            total_amount=final_total,
            shipping_charges=actual_shipping,
            status='pending',
            payment_status='unpaid',
            coupon_id=applied_coupon_id
        )
        db.session.add(order)
        db.session.flush()
        
        for item in items:
            oi = OrderItem(order_id=order.id, variant_id=item['v'].id, quantity=item['q'], price_at_time=item['v'].price)
            db.session.add(oi)
        
        if payment_method == 'online':
            try:
                client = get_razorpay_client()
                # Razorpay amount is in paise (100 paise = 1 unit)
                # Amount must be at least 1.00 INR (100 paise)
                razorpay_amount = max(100, int(final_total * 100))
                
                razorpay_order = client.order.create({
                    "amount": razorpay_amount,
                    "currency": "INR",
                    "payment_capture": 1 # Auto-capture
                })
                order.razorpay_order_id = razorpay_order['id']
                db.session.commit()
                
                return render_template('main/razorpay_checkout.html', 
                                       order=order, 
                                       razorpay_order_id=razorpay_order['id'],
                                       key_id=current_app.config['RAZORPAY_KEY_ID'],
                                       amount=razorpay_amount)
            except Exception as e:
                db.session.rollback()
                flash(f'Razorpay Error: {str(e)}', 'error')
                print(f"Razorpay Error: {str(e)}")
                return redirect(url_for('main.checkout'))
        else:
            db.session.commit()
            session['cart'] = {}
            flash('Order placed successfully! We will contact you shortly.', 'success')
            return redirect(url_for('main.index'))

    return render_template('main/checkout.html', total=total, items=items,
                           cod_enabled=cod_enabled, online_enabled=online_enabled,
                           actual_shipping=actual_shipping)

@main_bp.route('/payment/verify', methods=['POST'])
def verify_payment():
    data = request.form
    razorpay_payment_id = data.get('razorpay_payment_id')
    razorpay_order_id   = data.get('razorpay_order_id')
    razorpay_signature  = data.get('razorpay_signature')

    if not (razorpay_payment_id and razorpay_order_id and razorpay_signature):
        flash('Invalid payment response. Please try again or contact support.', 'error')
        return redirect(url_for('main.checkout'))

    client = get_razorpay_client()

    try:
        # 1. Verify signature authenticity
        client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        })

        # 2. Fetch payment status from Razorpay to confirm
        payment = client.payment.fetch(razorpay_payment_id)
        payment_status = payment.get('status')  # 'authorized', 'captured', 'failed'

        # 3. If authorized but not yet captured, capture it now (fallback)
        if payment_status == 'authorized':
            client.payment.capture(razorpay_payment_id, payment['amount'])
            payment_status = 'captured'

        if payment_status == 'captured':
            order = Order.query.filter_by(razorpay_order_id=razorpay_order_id).first()
            if order:
                order.payment_status = 'paid'
                order.status = 'processing'
                order.razorpay_payment_id = razorpay_payment_id
                
                # Exquisitely increment the coupon usage now that payment is confirmed
                if order.coupon_id:
                    c = Coupon.query.get(order.coupon_id)
                    if c:
                        c.used_count += 1
                        
                db.session.commit()
                session['cart'] = {}
                flash('Payment successful! Your order is being processed.', 'success')
                return redirect(url_for('main.account'))
            else:
                flash('Order not found. Please contact support with Payment ID: ' + razorpay_payment_id, 'error')
        else:
            flash(f'Payment could not be confirmed (status: {payment_status}). Please contact support.', 'error')

    except razorpay.errors.SignatureVerificationError:
        flash('Payment verification failed — signature mismatch. Please contact support.', 'error')
        print(f'[Razorpay] Signature mismatch: order={razorpay_order_id} payment={razorpay_payment_id}')
    except Exception as e:
        flash('An error occurred while confirming your payment. Please contact support.', 'error')
        print(f'[Razorpay] Verify error: {str(e)}')

    return redirect(url_for('main.index'))

from flask import jsonify
@main_bp.route('/payment/webhook', methods=['POST'])
def payment_webhook():
    webhook_body = request.get_data(as_text=True)
    webhook_signature = request.headers.get('X-Razorpay-Signature')
    secret = current_app.config.get('RAZORPAY_WEBHOOK_SECRET')
    client = get_razorpay_client()
    
    try:
        # Verify webhook signature using the client utility
        client.utility.verify_webhook_signature(webhook_body, webhook_signature, secret)
    except Exception as e:
        print(f"[Webhook Error] Invalid signature: {str(e)}")
        return jsonify({"status": "invalid signature"}), 400

    data = request.json
    event = data.get('event')
    
    if event == 'order.paid':
        payload = data.get('payload', {}).get('order', {}).get('entity', {})
        razorpay_order_id = payload.get('id')
        
        # Check if already updated
        order = Order.query.filter_by(razorpay_order_id=razorpay_order_id).first()
        if order and order.payment_status != 'paid':
            order.payment_status = 'paid'
            order.status = 'processing'
            
            if order.coupon_id:
                c = Coupon.query.get(order.coupon_id)
                if c:
                    c.used_count += 1
                    
            db.session.commit()
            print(f"[Webhook] Order {razorpay_order_id} marked as paid.")
            
    return jsonify({"status": "ok"}), 200
