import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from functools import wraps
from models import db, Product, Category, ProductVariant, Order, User, Setting, Review, Coupon, OfferBanner, OrderItem, AboutPage, ContactMessage
import cloudinary.uploader
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from about_cms import ASSET_SPECS, MAX_IMAGE_MB, delete_asset_from_cloudinary, get_assets, get_content, save_assets, save_content, upload_asset

def slugify(text):
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    text = text.strip('-')
    return text

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'admin':
            flash('Unauthorized access', 'error')
            return redirect(url_for('main.index'))
        return f(*args, **kwargs)
    return decorated_function

admin_bp = Blueprint('admin', __name__, url_prefix='/admin')

UPLOAD_FOLDER = 'static/images/products'

@admin_bp.route('/')
@admin_bp.route('/dashboard')
@admin_required
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

@admin_bp.route('/messages')
@admin_required
def manage_contact_messages():
    messages = ContactMessage.query.order_by(
        ContactMessage.is_read.asc(),
        ContactMessage.created_at.desc(),
    ).all()
    return render_template('admin/contact_messages.html', messages=messages)

@admin_bp.route('/message/<int:message_id>/read', methods=['POST'])
@admin_required
def mark_contact_message_read(message_id):
    contact_message = db.session.get(ContactMessage, message_id)
    if contact_message is None:
        flash('Message not found.', 'error')
        return redirect(url_for('admin.manage_contact_messages'))

    contact_message.is_read = True
    db.session.commit()
    flash('Message marked as read.', 'success')
    return redirect(url_for('admin.manage_contact_messages'))

@admin_bp.route('/message/<int:message_id>/delete', methods=['POST'])
@admin_required
def delete_contact_message(message_id):
    contact_message = db.session.get(ContactMessage, message_id)
    if contact_message is None:
        flash('Message not found.', 'error')
        return redirect(url_for('admin.manage_contact_messages'))

    db.session.delete(contact_message)
    db.session.commit()
    flash('Customer message deleted.', 'success')
    return redirect(url_for('admin.manage_contact_messages'))

# PRODUCTS MANAGEMENT
@admin_bp.route('/products')
@admin_required
def manage_products():
    products = Product.query.all()
    return render_template('admin/products.html', products=products)

@admin_bp.route('/product/new', methods=['GET', 'POST'])
@admin_required
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
        image_pubs = []
        ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
        
        for file in uploaded_files:
            if file and file.filename:
                ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
                if ext in ALLOWED_EXTENSIONS:
                    try:
                        res = cloudinary.uploader.upload(file, folder='products')
                        image_paths.append(res.get('secure_url'))
                        image_pubs.append(res.get('public_id'))
                    except Exception as e:
                        flash(f'Upload failed: {str(e)}', 'error')
                else:
                    flash(f'Invalid file type: {file.filename}. Only images are allowed.', 'error')
        
        # Generate unique slug
        base_slug = slugify(name)
        slug = base_slug
        counter = 1
        while Product.query.filter_by(slug=slug).first():
            slug = f"{base_slug}-{counter}"
            counter += 1

        # Create Product
        new_product = Product(
            name=name,
            slug=slug,
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
            images=','.join(image_paths) if image_paths else '',
            image_pub_ids=','.join(image_pubs) if image_pubs else ''
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

@admin_bp.route('/product/<int:product_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_product(product_id):
    product = Product.query.get_or_404(product_id)
    if request.method == 'POST':
        new_name = request.form.get('name')
        if product.name != new_name:
            product.name = new_name
            base_slug = slugify(new_name)
            slug = base_slug
            counter = 1
            while Product.query.filter(Product.slug == slug, Product.id != product.id).first():
                slug = f"{base_slug}-{counter}"
                counter += 1
            product.slug = slug
            
        product.category_id = int(request.form.get('category'))
        product.short_description = request.form.get('short_description')
        product.full_description = request.form.get('full_description')
        product.top_notes = request.form.get('top_notes')
        product.middle_notes = request.form.get('middle_notes')
        product.base_notes = request.form.get('base_notes')
        product.longevity = request.form.get('longevity')
        product.projection = request.form.get('projection')
        product.tag = request.form.get('tag') or None
        rank_raw = request.form.get('best_seller_rank')
        product.best_seller_rank = int(rank_raw) if rank_raw else None
        
        # New Images addition
        uploaded_files = request.files.getlist('images')
        image_paths = product.images.split(',') if product.images else []
        image_pubs = product.image_pub_ids.split(',') if product.image_pub_ids else []
        ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
        
        for file in uploaded_files:
            if file and file.filename:
                ext = file.filename.rsplit('.', 1)[1].lower() if '.' in file.filename else ''
                if ext in ALLOWED_EXTENSIONS:
                    try:
                        res = cloudinary.uploader.upload(file, folder='products')
                        image_paths.append(res.get('secure_url'))
                        image_pubs.append(res.get('public_id'))
                    except Exception as e:
                        flash(f'Upload error: {str(e)}', 'error')
        
        product.images = ','.join(image_paths) if image_paths else ''
        product.image_pub_ids = ','.join(image_pubs) if image_pubs else ''
        
        # Variants update
        variants = ProductVariant.query.filter_by(product_id=product.id).order_by(ProductVariant.id).all()
        var1 = variants[0] if len(variants) > 0 else None
        var2 = variants[1] if len(variants) > 1 else None

        # Variant 1
        p1 = request.form.get('price_1') or request.form.get('price_50ml')
        op1 = request.form.get('orig_price_1') or request.form.get('orig_price_50ml')
        s1 = request.form.get('stock_1') or request.form.get('stock_50ml')
        size1 = request.form.get('size_1', '50ml')
        if p1:
            if var1:
                var1.size = size1
                var1.price = float(p1)
                var1.original_price = float(op1) if op1 else None
                var1.stock_quantity = int(s1 or 0)
            else:
                db.session.add(ProductVariant(product_id=product.id, size=size1, price=float(p1), original_price=float(op1) if op1 else None, stock_quantity=int(s1 or 0)))
        
        # Variant 2
        p2 = request.form.get('price_2') or request.form.get('price_100ml')
        op2 = request.form.get('orig_price_2') or request.form.get('orig_price_100ml')
        s2 = request.form.get('stock_2') or request.form.get('stock_100ml')
        size2 = request.form.get('size_2', '100ml')
        if p2:
            if var2:
                var2.size = size2
                var2.price = float(p2)
                var2.original_price = float(op2) if op2 else None
                var2.stock_quantity = int(s2 or 0)
            else:
                db.session.add(ProductVariant(product_id=product.id, size=size2, price=float(p2), original_price=float(op2) if op2 else None, stock_quantity=int(s2 or 0)))

        db.session.commit()
        flash('Product updated successfully', 'success')
        return redirect(url_for('admin.manage_products'))

    categories = Category.query.all()
    variants = ProductVariant.query.filter_by(product_id=product.id).order_by(ProductVariant.id).all()
    var1 = variants[0] if len(variants) > 0 else None
    var2 = variants[1] if len(variants) > 1 else None
    return render_template('admin/edit_product.html', product=product, categories=categories, var1=var1, var2=var2)

@admin_bp.route('/product/<int:product_id>/image/<int:image_index>/delete', methods=['POST'])
@admin_required
def delete_product_image(product_id, image_index):
    product = Product.query.get_or_404(product_id)
    image_paths = product.images.split(',') if product.images else []
    image_public_ids = product.image_pub_ids.split(',') if product.image_pub_ids else []

    if image_index < 0 or image_index >= len(image_paths):
        flash('Image not found.', 'error')
        return redirect(url_for('admin.edit_product', product_id=product.id))

    public_id = image_public_ids[image_index] if image_index < len(image_public_ids) else ''
    if public_id:
        try:
            deletion_result = cloudinary.uploader.destroy(public_id)
        except Exception:
            current_app.logger.exception('Failed to delete product image from Cloudinary.', extra={'product_id': product.id})
            flash('Could not delete the image from storage. Please try again.', 'error')
            return redirect(url_for('admin.edit_product', product_id=product.id))

        if deletion_result.get('result') not in {'ok', 'not found'}:
            current_app.logger.error(
                'Cloudinary rejected product image deletion.',
                extra={'product_id': product.id, 'deletion_result': deletion_result},
            )
            flash('Could not delete the image from storage. Please try again.', 'error')
            return redirect(url_for('admin.edit_product', product_id=product.id))

    image_paths.pop(image_index)
    if image_index < len(image_public_ids):
        image_public_ids.pop(image_index)

    product.images = ','.join(image_paths)
    product.image_pub_ids = ','.join(image_public_ids)
    db.session.commit()
    flash('Product image deleted successfully.', 'success')
    return redirect(url_for('admin.edit_product', product_id=product.id))

@admin_bp.route('/product/<int:product_id>/delete')
@admin_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    
    # First, get all variant IDs for this product
    variant_ids = [v.id for v in product.variants]
    
    # Delete order items that reference these variants
    if variant_ids:
        OrderItem.query.filter(OrderItem.variant_id.in_(variant_ids)).delete(synchronize_session=False)
    
    # Now delete the product (cascade will delete variants due to cascade="all, delete-orphan")
    db.session.delete(product)
    db.session.commit()
    flash(f'{product.name} deleted successfully.', 'success')
    return redirect(url_for('admin.manage_products'))
# CATEGORY MANAGEMENT
@admin_bp.route('/categories', methods=['GET', 'POST'])
@admin_required
def manage_categories():
    if request.method == 'POST':
        name = request.form.get('name')
        if name:
            # Generate unique slug
            base_slug = slugify(name)
            slug = base_slug
            counter = 1
            while Category.query.filter_by(slug=slug).first():
                slug = f"{base_slug}-{counter}"
                counter += 1
                
            new_cat = Category(name=name, slug=slug)
            
            # Handle image upload
            image_file = request.files.get('image')
            if image_file and image_file.filename:
                ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
                ext = image_file.filename.rsplit('.', 1)[1].lower() if '.' in image_file.filename else ''
                if ext in ALLOWED_EXTENSIONS:
                    try:
                        res = cloudinary.uploader.upload(image_file, folder='categories')
                        new_cat.image = res.get('secure_url')
                        new_cat.image_pub_id = res.get('public_id')
                    except Exception as e:
                        flash(f'Upload error: {str(e)}', 'error')
                else:
                    flash(f'Invalid file type: {image_file.filename}. Only images are allowed.', 'error')
            
            db.session.add(new_cat)
            db.session.commit()
            flash('Category added successfully', 'success')
            
    categories = Category.query.all()
    return render_template('admin/categories.html', categories=categories)

@admin_bp.route('/category/<int:category_id>/delete', methods=['POST'])
@admin_required
def delete_category(category_id):
    cat = Category.query.get_or_404(category_id)
    
    # Safely handle related products: unassign them from this category
    # This preserves product data while removing the category association
    for product in cat.products:
        product.category_id = None
    
    db.session.delete(cat)
    db.session.commit()
    flash(f'Category "{cat.name}" deleted successfully. Associated products are now uncategorized.', 'success')
    return redirect(url_for('admin.manage_categories'))

@admin_bp.route('/category/<int:category_id>/edit', methods=['GET', 'POST'])
@admin_required
def edit_category(category_id):
    cat = Category.query.get_or_404(category_id)
    if request.method == 'POST':
        name = request.form.get('name')
        if name:
            if cat.name != name:
                cat.name = name
                base_slug = slugify(name)
                slug = base_slug
                counter = 1
                while Category.query.filter(Category.slug == slug, Category.id != cat.id).first():
                    slug = f"{base_slug}-{counter}"
                    counter += 1
                cat.slug = slug
            
            image_file = request.files.get('image')
            if image_file and image_file.filename:
                ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
                ext = image_file.filename.rsplit('.', 1)[1].lower() if '.' in image_file.filename else ''
                if ext in ALLOWED_EXTENSIONS:
                    try:
                        # Auto delete old image from Cloudinary to save space
                        if hasattr(cat, 'image_pub_id') and cat.image_pub_id:
                            try: cloudinary.uploader.destroy(cat.image_pub_id)
                            except: pass
                        res = cloudinary.uploader.upload(image_file, folder='categories')
                        cat.image = res.get('secure_url')
                        cat.image_pub_id = res.get('public_id')
                    except Exception as e:
                        flash(f'Upload error: {str(e)}', 'error')
                else:
                    flash(f'Invalid file type: {image_file.filename}. Only images are allowed.', 'error')
            
            db.session.commit()
            flash(f'Category "{cat.name}" updated successfully.', 'success')
            return redirect(url_for('admin.manage_categories'))

    return render_template('admin/edit_category.html', category=cat)

# ORDER MANAGEMENT
@admin_bp.route('/orders')
@admin_required
def manage_orders():
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template('admin/orders.html', orders=orders)

@admin_bp.route('/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get('status')
    if new_status in ['pending', 'processing', 'shipped', 'delivered', 'cancelled']:
        order.status = new_status
        db.session.commit()
        flash(f'Order #{order_id} status updated to {new_status}.', 'success')
    return redirect(url_for('admin.manage_orders'))

# SETTINGS (Shipping, etc)
def _parse_shipping_amount(value, label):
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError):
        raise ValueError(f'{label} must be a valid amount.')

    if not amount.is_finite() or amount < 0:
        raise ValueError(f'{label} must be zero or a positive amount.')

    return format(amount.quantize(Decimal('0.01')), 'f')


@admin_bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def manage_settings():
    ALLOWED_SETTINGS = {'shipping_charge', 'free_shipping_threshold', 'payment_cod_enabled', 'payment_online_enabled'}
    if request.method == 'POST':
        try:
            shipping_settings = {
                'shipping_charge': _parse_shipping_amount(request.form.get('shipping_charge'), 'Standard shipping charge'),
                'free_shipping_threshold': _parse_shipping_amount(request.form.get('free_shipping_threshold'), 'Free shipping threshold'),
            }
        except ValueError as error:
            flash(str(error), 'error')
            return redirect(url_for('admin.manage_settings'))

        submitted_settings = {
            **shipping_settings,
            'payment_cod_enabled': '1' if request.form.get('payment_cod_enabled') == '1' else '0',
            'payment_online_enabled': '1' if request.form.get('payment_online_enabled') == '1' else '0',
        }
        for key, value in submitted_settings.items():
            if key in ALLOWED_SETTINGS:
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
@admin_required
def manage_reviews():
    reviews = Review.query.order_by(Review.created_at.desc()).all()
    return render_template('admin/reviews.html', reviews=reviews)

@admin_bp.route('/review/<int:review_id>/delete')
@admin_required
def delete_review(review_id):
    review = Review.query.get_or_404(review_id)
    db.session.delete(review)
    db.session.commit()
    flash('Review deleted successfully', 'success')
    return redirect(url_for('admin.manage_reviews'))

# ── COUPON MANAGEMENT ──────────────────────────────────────────
@admin_bp.route('/coupons')
@admin_required
def manage_coupons():
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    coupons = Coupon.query.order_by(Coupon.created_at.desc()).all()
    return render_template('admin/coupons.html', coupons=coupons)

@admin_bp.route('/coupons/new', methods=['POST'])
@admin_required
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

@admin_bp.route('/coupons/<int:coupon_id>/toggle', methods=['POST'])
@admin_required
def toggle_coupon(coupon_id):
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    coupon = Coupon.query.get_or_404(coupon_id)
    coupon.is_active = not coupon.is_active
    db.session.commit()
    state = 'activated' if coupon.is_active else 'deactivated'
    flash(f'Coupon "{coupon.code}" {state}.', 'success')
    return redirect(url_for('admin.manage_coupons'))

@admin_bp.route('/coupons/<int:coupon_id>/delete', methods=['POST'])
@admin_required
def delete_coupon(coupon_id):
    if current_user.role != 'admin': return redirect(url_for('main.index'))
    coupon = Coupon.query.get_or_404(coupon_id)
    Order.query.filter_by(coupon_id=coupon.id).update(
        {Order.coupon_id: None},
        synchronize_session=False,
    )
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

# --- OFFERS MANAGEMENT ---
@admin_bp.route('/offers', methods=['GET', 'POST'])
@admin_required
def manage_offers():
    if request.method == 'POST':
        image_file = request.files.get('image')
        if image_file and image_file.filename:
            ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp', 'gif'}
            ext = image_file.filename.rsplit('.', 1)[1].lower() if '.' in image_file.filename else ''
            if ext in ALLOWED_EXTENSIONS:
                try:
                    res = cloudinary.uploader.upload(image_file, folder='offers')
                    new_offer = OfferBanner(image=res.get('secure_url'), image_pub_id=res.get('public_id'), is_active=True)
                    db.session.add(new_offer)
                    db.session.commit()
                    flash('Offer banner added successfully.', 'success')
                except Exception as e:
                    flash(f'Upload error: {str(e)}', 'error')
            else:
                flash(f'Invalid file type: {image_file.filename}. Only images are allowed.', 'error')
        return redirect(url_for('admin.manage_offers'))
        
    offers = OfferBanner.query.order_by(OfferBanner.created_at.desc()).all()
    return render_template('admin/offers.html', offers=offers)

@admin_bp.route('/offers/<int:offer_id>/delete')
@admin_required
def delete_offer(offer_id):
    offer = OfferBanner.query.get_or_404(offer_id)
    db.session.delete(offer)
    db.session.commit()
    flash('Offer banner deleted successfully.', 'success')
    return redirect(url_for('admin.manage_offers'))

# --- HOMEPAGE MEDIA (Unified Media + Bottom Banner) ---
@admin_bp.route('/homepage-media', methods=['GET', 'POST'])
@admin_required
def homepage_media():
    if request.method == 'POST':
        action = request.form.get('action')
        
        # --- Unified Multimedia Upload (Video or Image) ---
        if action == 'upload_media':
            media_file = request.files.get('media')
            if media_file and media_file.filename:
                ext = media_file.filename.rsplit('.', 1)[1].lower() if '.' in media_file.filename else ''
                ALLOWED_IMG = {'png', 'jpg', 'jpeg', 'webp'}
                ALLOWED_VID = {'mp4', 'webm', 'mov', 'avi'}
                
                is_img = ext in ALLOWED_IMG
                is_vid = ext in ALLOWED_VID
                
                if is_img or is_vid:
                    # Check file size (10 MB limit for videos, 5 MB for images)
                    media_file.seek(0, 2)  # Seek to end
                    file_size = media_file.tell()
                    media_file.seek(0)  # Reset to beginning
                    
                    MAX_VIDEO_SIZE = 10 * 1024 * 1024  # 10 MB
                    MAX_IMAGE_SIZE = 5 * 1024 * 1024   # 5 MB
                    
                    if is_vid and file_size > MAX_VIDEO_SIZE:
                        flash('Video file exceeds 10 MB limit. Please upload a smaller video.', 'error')
                        return redirect(url_for('admin.homepage_media'))
                    if is_img and file_size > MAX_IMAGE_SIZE:
                        flash('Image file exceeds 5 MB limit. Please upload a smaller image.', 'error')
                        return redirect(url_for('admin.homepage_media'))
                    
                    try:
                        # 1. Clean up old media first
                        old_pub_id = Setting.query.filter_by(key='homepage_media_pub_id').first()
                        old_type = Setting.query.filter_by(key='homepage_media_type').first()
                        if old_pub_id and old_pub_id.value:
                            res_type = 'video' if (old_type and old_type.value == 'video') else 'image'
                            try: cloudinary.uploader.destroy(old_pub_id.value, resource_type=res_type)
                            except: pass
                        
                        # 2. Upload new media - preserve original quality
                        res_type = 'video' if is_vid else 'image'
                        upload_params = {
                            'folder': 'homepage',
                            'resource_type': res_type,
                        }
                        # For videos: disable transformation/compression to preserve quality
                        if is_vid:
                            upload_params.update({
                                'quality': 'auto:best',
                                'fetch_format': 'auto',
                            })
                        res = cloudinary.uploader.upload(media_file, **upload_params)
                        
                        # 3. Update settings
                        def set_val(k, v):
                            s = Setting.query.filter_by(key=k).first()
                            if not s:
                                s = Setting(key=k, value=v)
                                db.session.add(s)
                            else:
                                s.value = v
                        
                        set_val('homepage_media_url', res.get('secure_url'))
                        set_val('homepage_media_pub_id', res.get('public_id'))
                        set_val('homepage_media_type', res_type)
                        
                        db.session.commit()
                        flash(f'Homepage {res_type} uploaded successfully.', 'success')
                    except Exception as e:
                        flash(f'Upload error: {str(e)}', 'error')
                else:
                    flash('Invalid file type. Allowed: Image (png, jpg, webp) or Video (mp4, webm, mov)', 'error')

        # --- Delete Multimedia ---
        elif action == 'delete_media':
            pub_id_setting = Setting.query.filter_by(key='homepage_media_pub_id').first()
            type_setting = Setting.query.filter_by(key='homepage_media_type').first()
            if pub_id_setting and pub_id_setting.value:
                res_type = type_setting.value if type_setting else 'image'
                try: cloudinary.uploader.destroy(pub_id_setting.value, resource_type=res_type)
                except: pass
            
            for key in ['homepage_media_url', 'homepage_media_pub_id', 'homepage_media_type']:
                s = Setting.query.filter_by(key=key).first()
                if s: s.value = ''
            db.session.commit()
            flash('Homepage multimedia removed.', 'success')
        
        # --- Banner Upload ---
        elif action == 'upload_banner':
            banner_file = request.files.get('banner')
            if banner_file and banner_file.filename:
                ALLOWED_IMG_EXT = {'png', 'jpg', 'jpeg', 'webp'}
                ext = banner_file.filename.rsplit('.', 1)[1].lower() if '.' in banner_file.filename else ''
                if ext in ALLOWED_IMG_EXT:
                    try:
                        old_pub_id = Setting.query.filter_by(key='bottom_banner_pub_id').first()
                        if old_pub_id and old_pub_id.value:
                            try: cloudinary.uploader.destroy(old_pub_id.value)
                            except: pass
                        
                        res = cloudinary.uploader.upload(banner_file, folder='homepage')
                        
                        def set_val(k, v):
                            s = Setting.query.filter_by(key=k).first()
                            if not s:
                                s = Setting(key=k, value=v)
                                db.session.add(s)
                            else:
                                s.value = v

                        set_val('bottom_banner_url', res.get('secure_url'))
                        set_val('bottom_banner_pub_id', res.get('public_id'))
                        
                        db.session.commit()
                        flash('Bottom banner uploaded successfully.', 'success')
                    except Exception as e:
                        flash(f'Banner upload error: {str(e)}', 'error')
                else:
                    flash(f'Invalid image type. Allowed: png, jpg, jpeg, webp', 'error')
        
        # --- Delete Banner ---
        elif action == 'delete_banner':
            pub_id_setting = Setting.query.filter_by(key='bottom_banner_pub_id').first()
            if pub_id_setting and pub_id_setting.value:
                try: cloudinary.uploader.destroy(pub_id_setting.value)
                except: pass
            
            for key in ['bottom_banner_url', 'bottom_banner_pub_id']:
                s = Setting.query.filter_by(key=key).first()
                if s:
                    s.value = ''
            db.session.commit()
            flash('Bottom banner removed.', 'success')

        # --- Hero About Us Image Upload ---
        elif action == 'upload_hero_about_image':
            image_file = request.files.get('hero_about_image')
            if image_file and image_file.filename:
                ALLOWED_IMG_EXT = {'png', 'jpg', 'jpeg', 'webp'}
                ext = image_file.filename.rsplit('.', 1)[1].lower() if '.' in image_file.filename else ''
                if ext in ALLOWED_IMG_EXT:
                    try:
                        # Delete old image from Cloudinary
                        old_pub_id = Setting.query.filter_by(key='hero_about_image_pub_id').first()
                        if old_pub_id and old_pub_id.value:
                            try: cloudinary.uploader.destroy(old_pub_id.value)
                            except: pass
                        
                        res = cloudinary.uploader.upload(image_file, folder='homepage')
                        
                        def set_val(k, v):
                            s = Setting.query.filter_by(key=k).first()
                            if not s:
                                s = Setting(key=k, value=v)
                                db.session.add(s)
                            else:
                                s.value = v
                        
                        set_val('hero_about_image_url', res.get('secure_url'))
                        set_val('hero_about_image_pub_id', res.get('public_id'))
                        
                        db.session.commit()
                        flash('Hero About Us image uploaded successfully.', 'success')
                    except Exception as e:
                        flash(f'Upload error: {str(e)}', 'error')
                else:
                    flash(f'Invalid image type. Allowed: png, jpg, jpeg, webp', 'error')

        # --- Delete Hero About Us Image ---
        elif action == 'delete_hero_about_image':
            pub_id_setting = Setting.query.filter_by(key='hero_about_image_pub_id').first()
            if pub_id_setting and pub_id_setting.value:
                try: cloudinary.uploader.destroy(pub_id_setting.value)
                except: pass
            
            for key in ['hero_about_image_url', 'hero_about_image_pub_id']:
                s = Setting.query.filter_by(key=key).first()
                if s:
                    s.value = ''
            db.session.commit()
            flash('Hero About Us image removed.', 'success')
        
        return redirect(url_for('admin.homepage_media'))
    
    media_url = (Setting.query.filter_by(key='homepage_media_url').first() or Setting(value='')).value
    media_type = (Setting.query.filter_by(key='homepage_media_type').first() or Setting(value='')).value
    banner_url = (Setting.query.filter_by(key='bottom_banner_url').first() or Setting(value='')).value
    hero_about_image_url = (Setting.query.filter_by(key='hero_about_image_url').first() or Setting(value='')).value
    return render_template('admin/homepage_media.html', media_url=media_url, media_type=media_type, banner_url=banner_url, hero_about_image_url=hero_about_image_url)

def _about_text(field_name, maximum, required=False):
    value = request.form.get(field_name, '').strip()
    if required and not value:
        raise ValueError(f'{field_name.replace("_", " ").title()} is required.')
    if len(value) > maximum:
        raise ValueError(f'{field_name.replace("_", " ").title()} must be {maximum} characters or fewer.')
    return value


def _about_content_from_form(existing_content):
    pillars = []
    values = []
    for index in range(1, 5):
        pillars.append({
            'icon': existing_content['story']['pillars'][index - 1]['icon'],
            'label': _about_text(f'story_pillar_{index}_label', 80, required=True),
        })
        values.append({
            'icon': existing_content['values']['items'][index - 1]['icon'],
            'title': _about_text(f'value_{index}_title', 100, required=True),
            'description': _about_text(f'value_{index}_description', 220, required=True),
        })

    return {
        'seo_title': _about_text('seo_title', 200, required=True),
        'hero': {
            'eyebrow': _about_text('hero_eyebrow', 80, required=True),
            'heading': _about_text('hero_heading', 160, required=True),
            'description': _about_text('hero_description', 700, required=True),
            'button_label': existing_content['hero']['button_label'],
            'button_href': existing_content['hero']['button_href'],
        },
        'story': {
            'eyebrow': _about_text('story_eyebrow', 80, required=True),
            'heading': _about_text('story_heading', 200, required=True),
            'description': _about_text('story_description', 1200, required=True),
            'pillars': pillars,
        },
        'founder': {
            'eyebrow': _about_text('founder_eyebrow', 80, required=True),
            'heading': _about_text('founder_heading', 200, required=True),
            'description': _about_text('founder_description', 900, required=True),
            'quote': _about_text('founder_quote', 350, required=True),
            'attribution': _about_text('founder_attribution', 100, required=True),
            'script': _about_text('founder_script', 120, required=True),
            'portrait_alt': _about_text('founder_portrait_alt', 160, required=True),
        },
        'values': {
            'eyebrow': _about_text('values_eyebrow', 80, required=True),
            'heading': _about_text('values_heading', 160, required=True),
            'description': _about_text('values_description', 700, required=True),
            'items': values,
        },
        'purpose': {
            'eyebrow': _about_text('purpose_eyebrow', 80, required=True),
            'heading': _about_text('purpose_heading', 160, required=True),
            'description': _about_text('purpose_description', 700, required=True),
            'button_label': existing_content['purpose']['button_label'],
            'button_href': existing_content['purpose']['button_href'],
        },
    }


# --- ABOUT PAGE MANAGEMENT ---
@admin_bp.route('/about', methods=['GET', 'POST'])
@admin_required
def manage_about():
    if request.method == 'POST':
        action = request.form.get('action', '')
        try:
            if action == 'save_about_content':
                save_content(_about_content_from_form(get_content()))
                flash('About page content saved. The public page is updated immediately.', 'success')
            elif action == 'upload_about_asset':
                slot = request.form.get('slot', '')
                image_file = request.files.get('image')
                if image_file is None or not image_file.filename:
                    raise ValueError('Choose an image to upload.')
                assets = get_assets()
                previous_asset = assets.get(slot, {})
                assets[slot] = upload_asset(slot, image_file)
                save_assets(assets)
                try:
                    delete_asset_from_cloudinary(previous_asset.get('public_id', ''))
                except Exception:
                    current_app.logger.warning('About image replaced but the previous Cloudinary asset could not be deleted.', exc_info=True)
                flash(f'{ASSET_SPECS[slot]["label"]} uploaded and optimized successfully.', 'success')
            elif action == 'delete_about_asset':
                slot = request.form.get('slot', '')
                if slot not in ASSET_SPECS:
                    raise ValueError('Unknown About-page image slot.')
                assets = get_assets()
                asset = assets.get(slot, {})
                if not asset:
                    raise ValueError('This image has already been removed.')
                delete_asset_from_cloudinary(asset.get('public_id', ''))
                assets.pop(slot, None)
                save_assets(assets)
                flash(f'{ASSET_SPECS[slot]["label"]} removed.', 'success')
            else:
                raise ValueError('Unknown About-page update request.')
        except (ValueError, RuntimeError) as error:
            db.session.rollback()
            flash(str(error), 'error')
        except Exception:
            db.session.rollback()
            current_app.logger.exception('About page update failed.', extra={'action': action})
            flash('The About page could not be updated. Please try again.', 'error')
        return redirect(url_for('admin.manage_about'))

    return render_template('admin/about.html', content=get_content(), assets=get_assets(), asset_specs=ASSET_SPECS, max_image_mb=MAX_IMAGE_MB)
