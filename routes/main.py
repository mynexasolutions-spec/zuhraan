from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from models import db, Product, Category, ProductVariant, Review, User, Order, OrderItem, Setting, Coupon, OfferBanner
from flask_login import current_user, login_user, logout_user, login_required
from werkzeug.security import generate_password_hash, check_password_hash
import json

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


@main_bp.route('/')
def index():
    best_sellers = (Product.query
                    .filter_by(tag='best_seller')
                    .order_by(Product.best_seller_rank.asc().nullslast())
                    .limit(4).all())
    categories = Category.query.all()
    offers = OfferBanner.query.filter_by(is_active=True).order_by(OfferBanner.created_at.desc()).all()
    return render_template('index.html', best_sellers=best_sellers, categories=categories, offers=offers)

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
            (Product.description.ilike(f'%{search_query}%')) |
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
    variant = ProductVariant.query.get_or_404(variant_id)
    cart = session.get('cart', {})
    v_id_str = str(variant_id)
    if v_id_str in cart: cart[v_id_str] += quantity
    else: cart[v_id_str] = quantity
    session['cart'] = cart
    flash(f'{variant.product.name} added to cart!', 'success')
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
            cart[v_id_str] += 1
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

        final_total = total
        discount = 0.0

        applied_coupon_id = None
        # Apply coupon if given
        if coupon_code:
            result = _validate_coupon(coupon_code, total)
            if result['valid']:
                discount = result['discount']
                final_total = result['final_total']
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
                           cod_enabled=cod_enabled, online_enabled=online_enabled)

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
