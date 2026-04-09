import os
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from models import db, Product, Category, ProductVariant, Order, User, Setting, Review, Coupon
from datetime import datetime, timedelta

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

UPLOAD_FOLDER = 'static/images/products'

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@login_required
def dashboard():
    if current_user.role != 'admin':
        flash('Unauthorized access', 'error')
        return redirect(url_for('main.index'))
    # Statistics for dashboard
    total_revenue = db.session.query(db.func.sum(Order.total_amount)).filter(Order.payment_status == 'paid').scalar() or 0
    total_orders = Order.query.count()
    pending_orders = Order.query.filter_by(status='pending').count()
    active_products = Product.query.count()
    customers_count = User.query.filter_by(role='user').count()
    
    # Low stock alert (threshold < 5)
    low_stock_variants = ProductVariant.query.filter(ProductVariant.stock_quantity < 5).all()
    
    # Recent Orders
    recent_orders = Order.query.order_by(Order.created_at.desc()).limit(5).all()
    
    return render_template('admin/dashboard.html', 
                            revenue=total_revenue,
                            orders_count=total_orders,
                            pending=pending_orders,
                            products_count=active_products,
                            customers=customers_count,
                            low_stock=low_stock_variants,
                            recent_orders=recent_orders)

# PRODUCTS MANAGEMENT
@admin_bp.route('/products')
@login_required
def manage_products():
    products = Product.query.all()
    return render_template('admin/products.html', products=products)

@admin_bp.route('/product/new', methods=['GET', 'POST'])
@login_required
def add_product():
    if request.method == 'POST':
        name = request.form.get('name')
        category_id = request.form.get('category_id')
        short_desc = request.form.get('short_description')
        full_desc = request.form.get('full_description')
        
        # Fragrance Notes
        top = request.form.get('top_notes')
        middle = request.form.get('middle_notes')
        base = request.form.get('base_notes')
        
        # Stats
        long = request.form.get('longevity')
        proj = request.form.get('projection')
        
        # Special Tag
        tag = request.form.get('tag') or None
        rank_raw = request.form.get('best_seller_rank')
        best_seller_rank = int(rank_raw) if rank_raw else None
        
        # Processing Images
        uploaded_files = request.files.getlist('images')
        image_paths = []
        for file in uploaded_files:
            if file and file.filename:
                filename = secure_filename(file.filename)
                file.save(os.path.join(UPLOAD_FOLDER, filename))
                image_paths.append(f'images/products/{filename}')
        
        # Create Product
        new_product = Product(
            name=name,
            category_id=category_id,
            short_description=short_desc,
            full_description=full_desc,
            top_notes=top,
            middle_notes=middle,
            base_notes=base,
            longevity=long,
            projection=proj,
            tag=tag,
            best_seller_rank=best_seller_rank,
            images=','.join(image_paths) if image_paths else ''
        )
        db.session.add(new_product)
        db.session.flush() # To get ID for variants
        
        # Create Variants (50ml & 100ml)
        # 50ml
        p50 = request.form.get('price_50ml')
        op50 = request.form.get('orig_price_50ml')
        s50 = request.form.get('stock_50ml')
        if p50:
            var50 = ProductVariant(product_id=new_product.id, size='50ml', 
                                   price=float(p50), 
                                   original_price=float(op50) if op50 else None,
                                   stock_quantity=int(s50 or 0))
            db.session.add(var50)
            
        # 100ml
        p100 = request.form.get('price_100ml')
        op100 = request.form.get('orig_price_100ml')
        s100 = request.form.get('stock_100ml')
        if p100:
            var100 = ProductVariant(product_id=new_product.id, size='100ml', 
                                    price=float(p100), 
                                    original_price=float(op100) if op100 else None,
                                    stock_quantity=int(s100 or 0))
            db.session.add(var100)
            
        db.session.commit()
        flash('Product added successfully', 'success')
        return redirect(url_for('admin.manage_products'))
        
    categories = Category.query.all()
    return render_template('admin/add_product.html', categories=categories)

# CATEGORY MANAGEMENT
@admin_bp.route('/categories', methods=['GET', 'POST'])
@login_required
def manage_categories():
    if request.method == 'POST':
        name = request.form.get('name')
        if name:
            new_cat = Category(name=name)
            db.session.add(new_cat)
            db.session.commit()
            flash('Category added', 'success')
            
    categories = Category.query.all()
    return render_template('admin/categories.html', categories=categories)

# ORDER MANAGEMENT
@admin_bp.route('/orders')
@login_required
def manage_orders():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template('admin/orders.html', orders=orders)

# SETTINGS (Shipping, etc)
@admin_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def manage_settings():
    if request.method == 'POST':
        for key, value in request.form.items():
            setting = Setting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                db.session.add(Setting(key=key, value=value))
        db.session.commit()
        flash('Settings updated', 'success')
        
    all_settings = {s.key: s.value for s in Setting.query.all()}
    return render_template('admin/settings.html', settings=all_settings)

@admin_bp.route('/reviews')
@login_required
def manage_reviews():
    reviews = Review.query.order_by(Review.created_at.desc()).all()
    return render_template('admin/reviews.html', reviews=reviews)

@admin_bp.route('/review/<int:review_id>/delete')
@login_required
def delete_review(review_id):
    review = Review.query.get_or_404(review_id)
    db.session.delete(review)
    db.session.commit()
    flash('Review deleted successfully', 'success')
    return redirect(url_for('admin.manage_reviews'))

# ── COUPON MANAGEMENT ──────────────────────────────────────────
@admin_bp.route('/coupons')
@login_required
def manage_coupons():
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).all()
    return render_template('admin/coupons.html', coupons=coupons)

@admin_bp.route('/coupons/new', methods=['POST'])
@login_required
def create_coupon():
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    code = request.form.get('code', '').strip().upper()
    discount_type  = request.form.get('discount_type', 'percent')
    discount_value = float(request.form.get('discount_value', 0))
    min_order      = float(request.form.get('min_order_amount', 0) or 0)
    max_uses       = request.form.get('max_uses')
    expires_raw    = request.form.get('expires_at')

    if not code or discount_value <= 0:
        flash('Code and a positive discount value are required.', 'error')
        return redirect(url_for('admin.manage_coupons'))
    if Coupon.query.filter_by(code=code).first():
        flash(f'Coupon code "{code}" already exists.', 'error')
        return redirect(url_for('admin.manage_coupons'))

    coupon = Coupon(
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        min_order_amount=min_order,
        max_uses=int(max_uses) if max_uses else None,
        expires_at=datetime.strptime(expires_raw, '%Y-%m-%d') if expires_raw else None,
    )
    db.session.add(coupon)
    db.session.commit()
    flash(f'Coupon "{code}" created successfully!', 'success')
    return redirect(url_for('admin.manage_coupons'))

@admin_bp.route('/coupons/<int:coupon_id>/toggle')
@login_required
def toggle_coupon(coupon_id):
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    coupon = Coupon.query.get_or_404(coupon_id)
    coupon.is_active = not coupon.is_active
    db.session.commit()
    state = 'activated' if coupon.is_active else 'deactivated'
    flash(f'Coupon "{coupon.code}" {state}.', 'success')
    return redirect(url_for('admin.manage_coupons'))

@admin_bp.route('/coupons/<int:coupon_id>/delete')
@login_required
def delete_coupon(coupon_id):
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    coupon = Coupon.query.get_or_404(coupon_id)
    db.session.delete(coupon)
    db.session.commit()
    flash(f'Coupon deleted.', 'success')
    return redirect(url_for('admin.manage_coupons'))

# ── COUPON AJAX VALIDATE (used by checkout) ─────────────────────
from flask import jsonify
@admin_bp.route('/api/coupon/validate', methods=['POST'])
def validate_coupon_api():
    from routes.main import _validate_coupon
    code = request.json.get('code', '').strip().upper()
    total = float(request.json.get('total', 0))
    result = _validate_coupon(code, total)
    return jsonify(result)
