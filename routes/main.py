from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from models import db, Product, Category, ProductVariant, Review, User, Order, OrderItem, Setting, Coupon
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
        return {'valid': False, 'message': f'Minimum order of ${coupon.min_order_amount:.2f} required.'}
    if coupon.discount_type == 'percent':
        discount = round(order_total * coupon.discount_value / 100, 2)
    else:
        discount = min(coupon.discount_value, order_total)
    final = round(order_total - discount, 2)
    return {'valid': True, 'discount': discount, 'final_total': final,
            'message': f'{coupon.discount_value}% off applied!' if coupon.discount_type == 'percent' else f'${coupon.discount_value:.2f} off applied!',
            'coupon_id': coupon.id}

@main_bp.route('/')
def index():
    best_sellers = (Product.query
                    .filter_by(tag='best_seller')
                    .order_by(Product.best_seller_rank.asc().nullslast())
                    .limit(4).all())
    return render_template('index.html', best_sellers=best_sellers)

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
    cat_id = request.args.get('cat', type=int)
    sort = request.args.get('sort', 'newest')
    on_sale = request.args.get('on_sale', type=int)
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    page = request.args.get('page', 1, type=int)
    per_page = 8
    query = Product.query
    if cat_id: query = query.filter_by(category_id=cat_id)
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
    return render_template('shop.html', categories=categories, products=products, active_cat=cat_id, active_sort=sort, active_sale=on_sale, active_min_price=min_price, active_max_price=max_price, pagination=pagination, page=page)

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
    if current_user.is_authenticated: return redirect(url_for('main.account'))
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('main.account'))
        flash('Invalid credentials', 'error')
    return render_template('main/login.html')

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

@main_bp.route('/account')
@login_required
def account():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template('main/account.html', orders=orders)

# --- CHECKOUT ---
@main_bp.route('/checkout', methods=['GET', 'POST'])
def checkout():
    cart = session.get('cart', {})
    if not cart: return redirect(url_for('main.shop'))

    total = 0
    items = []
    for v_id, qty in cart.items():
        variant = ProductVariant.query.get(int(v_id))
        total += variant.price * qty
        items.append({'v': variant, 'q': qty})

    # Load payment method toggles from settings
    settings = {s.key: s.value for s in Setting.query.all()}
    cod_enabled    = settings.get('payment_cod_enabled', '1') == '1'
    online_enabled = settings.get('payment_online_enabled', '0') == '1'

    if request.method == 'POST':
        address_line1 = request.form.get('address_line1', '').strip()
        address_line2 = request.form.get('address_line2', '').strip()
        city          = request.form.get('city', '').strip()
        state         = request.form.get('state', '').strip()
        pincode       = request.form.get('pincode', '').strip()
        country       = request.form.get('country', 'India').strip()
        coupon_code   = request.form.get('coupon_code', '').strip().upper()
        payment_method = request.form.get('payment_method', 'cod')

        final_total = total
        discount = 0.0

        # Apply coupon if given
        if coupon_code:
            result = _validate_coupon(coupon_code, total)
            if result['valid']:
                discount = result['discount']
                final_total = result['final_total']
                # Increment used count
                c = Coupon.query.filter_by(code=coupon_code).first()
                if c:
                    c.used_count += 1
            else:
                flash(result['message'], 'error')
                return redirect(url_for('main.checkout'))

        full_address = ', '.join(filter(None, [address_line1, address_line2, city, state, pincode, country]))

        order = Order(
            user_id=current_user.id if current_user.is_authenticated else None,
            customer_name=request.form.get('name'),
            customer_email=request.form.get('email'),
            customer_phone=request.form.get('phone'),
            shipping_address=full_address,
            address_line1=address_line1,
            address_line2=address_line2,
            city=city,
            state=state,
            pincode=pincode,
            country=country,
            total_amount=final_total
        )
        db.session.add(order)
        db.session.flush()
        for item in items:
            oi = OrderItem(order_id=order.id, variant_id=item['v'].id, quantity=item['q'], price_at_time=item['v'].price)
            db.session.add(oi)
        db.session.commit()
        session['cart'] = {}
        flash('Order placed successfully! We will contact you shortly.', 'success')
        return redirect(url_for('main.index'))

    return render_template('main/checkout.html', total=total, items=items,
                           cod_enabled=cod_enabled, online_enabled=online_enabled)

