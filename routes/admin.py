import json
import os
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
from werkzeug.datastructures import FileStorage
from werkzeug.utils import secure_filename
from functools import wraps
from models import db, Product, Category, ProductVariant, Order, User, Setting, Review, Coupon, OfferBanner, OrderItem, AboutPage, ContactMessage, utcnow_naive
import cloudinary.uploader
from cloudinary.exceptions import Error as CloudinaryError
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import re
from sqlalchemy.exc import SQLAlchemyError
from about_cms import ASSET_SPECS, MAX_IMAGE_MB, delete_asset_from_cloudinary, get_assets, get_content, save_assets, save_content, upload_asset
from gokwik_client import GoKwikRequestError, is_gokwik_payment_method, sync_gokwik_order
from homepage_reels import REEL_TITLES, get_homepage_reels, save_homepage_reels, set_max_reels, _get_max_reels
from routes.main import reconcile_razorpay_order, release_order_inventory
from routes.ithink import (
    apply_ithink_shipment,
    apply_ithink_tracking,
    create_or_recover_ithink_shipment,
)
from ithink_client import (
    IThinkClient,
    IThinkRequestError,
    extract_tracking_data,
    normalize_indian_phone,
    normalize_indian_pincode,
    parcel_dimensions,
    payment_mode_for_order,
)


HOME_STORY_IMAGES_KEY = 'home_story_images'
MAX_HOME_STORY_IMAGES = 5
MAX_REEL_VIDEO_BYTES = 10 * 1024 * 1024
ALLOWED_REEL_VIDEO_EXTENSIONS = frozenset({'mp4', 'webm', 'mov'})


def get_home_story_images():
    setting = Setting.query.filter_by(key=HOME_STORY_IMAGES_KEY).first()
    if setting is None or not setting.value:
        return []
    try:
        saved_images = json.loads(setting.value)
    except json.JSONDecodeError:
        return []
    if not isinstance(saved_images, list):
        return []
    return [
        {'url': image['url'], 'public_id': image.get('public_id', '')}
        for image in saved_images
        if isinstance(image, dict) and isinstance(image.get('url'), str) and image['url']
    ]


def save_home_story_images(images):
    setting = Setting.query.filter_by(key=HOME_STORY_IMAGES_KEY).first()
    value = json.dumps(images)
    if setting is None:
        db.session.add(Setting(key=HOME_STORY_IMAGES_KEY, value=value))
    else:
        setting.value = value


def _reel_index_from_form() -> int:
    try:
        reel_index = int(request.form.get('reel_index', ''))
    except ValueError as error:
        raise ValueError('Select a valid reel slot.') from error
    max_reels = _get_max_reels()
    if reel_index < 0 or reel_index >= max_reels:
        raise ValueError('Select a valid reel slot.')
    return reel_index


def _validate_reel_video(video_file: FileStorage | None) -> None:
    if video_file is None or not video_file.filename:
        raise ValueError('Choose a reel video to upload.')
    extension = video_file.filename.rsplit('.', 1)[1].lower() if '.' in video_file.filename else ''
    if extension not in ALLOWED_REEL_VIDEO_EXTENSIONS:
        raise ValueError('Invalid reel format. Upload an MP4, WEBM, or MOV video.')
    video_file.seek(0, os.SEEK_END)
    file_size = video_file.tell()
    video_file.seek(0)
    if file_size <= 0:
        raise ValueError('The selected reel video is empty.')
    if file_size > MAX_REEL_VIDEO_BYTES:
        raise ValueError('Reel video exceeds the 10 MB limit.')


def _reel_title_from_form() -> str:
    title = request.form.get('reel_title', '').strip()
    if not title:
        raise ValueError('Reel text is required.')
    if len(title) > 80:
        raise ValueError('Reel text must be 80 characters or fewer.')
    return title


def _delete_cloudinary_reel(public_id: str) -> None:
    if not public_id:
        return
    cloudinary.uploader.destroy(public_id, resource_type='video', invalidate=True)

def slugify(text):
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_-]+', '-', text)
    text = text.strip('-')
    return text


def _variant_rows_from_form():
    sizes = request.form.getlist('variant_size')
    original_prices = request.form.getlist('variant_original_price')
    prices = request.form.getlist('variant_price')
    stocks = request.form.getlist('variant_stock')
    ids = request.form.getlist('variant_id')

    rows = []
    for index, size in enumerate(sizes):
        size = size.strip()
        original_price = original_prices[index] if index < len(original_prices) else ''
        price = prices[index] if index < len(prices) else ''
        stock = stocks[index] if index < len(stocks) else ''
        variant_id = ids[index] if index < len(ids) else ''
        if not size and not price and not original_price and not stock:
            continue
        if not size or not price:
            raise ValueError('Each variant must include a volume and sale price.')
        try:
            sale_price = float(price)
            msrp = float(original_price) if original_price else None
            stock_quantity = int(stock or 0)
        except (TypeError, ValueError):
            raise ValueError('Variant prices and stock quantity must be valid numbers.')
        if sale_price < 0 or (msrp is not None and msrp < 0) or stock_quantity < 0:
            raise ValueError('Variant prices and stock quantity cannot be negative.')
        rows.append({
            'id': int(variant_id) if variant_id else None,
            'size': size,
            'price': sale_price,
            'original_price': msrp,
            'stock_quantity': stock_quantity,
        })
    return rows


def _sync_product_variants(product_id):
    submitted_rows = _variant_rows_from_form()
    existing_variants = {
        variant.id: variant
        for variant in ProductVariant.query.filter_by(product_id=product_id).all()
    }
    submitted_ids = set()

    for row in submitted_rows:
        variant_id = row.pop('id')
        if variant_id is not None:
            variant = existing_variants.get(variant_id)
            if variant is None:
                raise ValueError('One of the selected variants no longer exists.')
            submitted_ids.add(variant_id)
        else:
            variant = ProductVariant(product_id=product_id)
            db.session.add(variant)
        variant.size = row['size']
        variant.price = row['price']
        variant.original_price = row['original_price']
        variant.stock_quantity = row['stock_quantity']

    for variant_id, variant in existing_variants.items():
        if variant_id not in submitted_ids:
            if OrderItem.query.filter_by(variant_id=variant_id).first():
                raise ValueError(
                    f'Variant "{variant.size}" belongs to an existing order and cannot be deleted.'
                )
            db.session.delete(variant)


def _sync_single_product_variant(product_id):
    price = (request.form.getlist('variant_price') or [''])[0].strip()
    original_price = (request.form.getlist('variant_original_price') or [''])[0].strip()
    stock = (request.form.getlist('variant_stock') or [''])[0].strip()
    if not price:
        raise ValueError('A sale price is required when variants are disabled.')
    try:
        price_value = float(price)
        original_price_value = float(original_price) if original_price else None
        stock_value = int(stock or 0)
    except (TypeError, ValueError):
        raise ValueError('Price and stock quantity must be valid numbers.')
    if price_value < 0 or (original_price_value is not None and original_price_value < 0) or stock_value < 0:
        raise ValueError('Price and stock quantity cannot be negative.')

    variants = ProductVariant.query.filter_by(product_id=product_id).order_by(ProductVariant.id).all()
    variant = variants[0] if variants else ProductVariant(product_id=product_id)
    variant.size = 'Standard'
    variant.price = price_value
    variant.original_price = original_price_value
    variant.stock_quantity = stock_value
    db.session.add(variant)
    for extra_variant in variants[1:]:
        if OrderItem.query.filter_by(variant_id=extra_variant.id).first():
            raise ValueError(
                f'Variant "{extra_variant.size}" belongs to an existing order and cannot be deleted.'
            )
        db.session.delete(extra_variant)


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
        show_top_notes = 'show_top_notes' in request.form
        show_middle_notes = 'show_middle_notes' in request.form
        show_base_notes = 'show_base_notes' in request.form
        
        # Stats
        long = request.form.get('longevity')
        proj = request.form.get('projection')
        show_longevity = 'show_longevity' in request.form
        show_projection = 'show_projection' in request.form
        variants_enabled = 'variants_enabled' in request.form
        
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
            show_top_notes=show_top_notes,
            show_middle_notes=show_middle_notes,
            show_base_notes=show_base_notes,
            show_longevity=show_longevity,
            show_projection=show_projection,
            variants_enabled=variants_enabled,
            tag=tag,
            best_seller_rank=best_seller_rank,
            images=','.join(image_paths) if image_paths else '',
            image_pub_ids=','.join(image_pubs) if image_pubs else ''
        )
        db.session.add(new_product)
        db.session.flush() # To get ID for variants
        
        try:
            if variants_enabled:
                _sync_product_variants(new_product.id)
            else:
                _sync_single_product_variant(new_product.id)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
            return redirect(url_for('admin.add_product'))
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
        product.show_top_notes = 'show_top_notes' in request.form
        product.show_middle_notes = 'show_middle_notes' in request.form
        product.show_base_notes = 'show_base_notes' in request.form
        product.show_longevity = 'show_longevity' in request.form
        product.show_projection = 'show_projection' in request.form
        product.variants_enabled = 'variants_enabled' in request.form
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
        
        try:
            if product.variants_enabled:
                _sync_product_variants(product.id)
            else:
                _sync_single_product_variant(product.id)
            db.session.commit()
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), 'error')
            return redirect(url_for('admin.edit_product', product_id=product.id))
        flash('Product updated successfully', 'success')
        return redirect(url_for('admin.manage_products'))

    categories = Category.query.all()
    variants = ProductVariant.query.filter_by(product_id=product.id).order_by(ProductVariant.id).all()
    return render_template('admin/edit_product.html', product=product, categories=categories, variants=variants)

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

@admin_bp.route('/product/<int:product_id>/delete', methods=['POST'])
@admin_required
def delete_product(product_id):
    product = Product.query.get_or_404(product_id)
    variant_ids = [v.id for v in product.variants]
    if variant_ids and OrderItem.query.filter(OrderItem.variant_id.in_(variant_ids)).first():
        flash(
            'This product belongs to an existing order and cannot be deleted. '
            'It must be kept to preserve order history.',
            'error',
        )
        return redirect(url_for('admin.manage_products'))

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
    settings = {setting.key: setting.value for setting in Setting.query.all()}
    ithink_defaults = {
        'weight': settings.get(
            'ithink_default_weight_kg',
            current_app.config.get('ITHINK_DEFAULT_WEIGHT_KG', '0.5'),
        ),
        'length': settings.get(
            'ithink_default_length_cm',
            current_app.config.get('ITHINK_DEFAULT_LENGTH_CM', '10'),
        ),
        'width': settings.get(
            'ithink_default_width_cm',
            current_app.config.get('ITHINK_DEFAULT_WIDTH_CM', '10'),
        ),
        'height': settings.get(
            'ithink_default_height_cm',
            current_app.config.get('ITHINK_DEFAULT_HEIGHT_CM', '10'),
        ),
    }
    return render_template(
        'admin/orders.html',
        orders=orders,
        ithink_enabled=current_app.config.get('ITHINK_ENABLED'),
        ithink_defaults=ithink_defaults,
    )


@admin_bp.post('/orders/<int:order_id>/details')
@admin_required
def update_order_details(order_id):
    """Correct delivery details before retrying shipment creation."""
    order = Order.query.get_or_404(order_id)
    fields = {
        'customer_name': request.form.get('customer_name', '').strip(),
        'customer_email': request.form.get('customer_email', '').strip().lower(),
        'customer_phone': request.form.get('customer_phone', '').strip(),
        'address_line1': request.form.get('address_line1', '').strip(),
        'address_line2': request.form.get('address_line2', '').strip(),
        'city': request.form.get('city', '').strip(),
        'state': request.form.get('state', '').strip(),
        'pincode': request.form.get('pincode', '').strip(),
        'country': request.form.get('country', '').strip() or 'India',
    }
    if not all(fields[name] for name in ('customer_name', 'customer_email', 'customer_phone', 'address_line1', 'city', 'state', 'pincode')):
        flash('Complete all required delivery details before saving.', 'error')
        return redirect(url_for('admin.manage_orders'))
    if '@' not in fields['customer_email']:
        flash('Enter a valid customer email address.', 'error')
        return redirect(url_for('admin.manage_orders'))
    try:
        fields['customer_phone'] = normalize_indian_phone(fields['customer_phone'])
        fields['pincode'] = normalize_indian_pincode(fields['pincode'])
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('admin.manage_orders'))

    order.customer_name = fields['customer_name']
    order.customer_email = fields['customer_email']
    order.customer_phone = fields['customer_phone']
    order.address_line1 = fields['address_line1']
    order.address_line2 = fields['address_line2'] or None
    order.city = fields['city']
    order.state = fields['state']
    order.pincode = fields['pincode']
    order.country = fields['country']
    order.shipping_address = ', '.join(filter(None, [
        fields['address_line1'],
        fields['address_line2'],
        fields['city'],
        fields['state'],
        fields['pincode'],
        fields['country'],
    ]))
    try:
        db.session.commit()
    except SQLAlchemyError:
        db.session.rollback()
        current_app.logger.exception(
            'Unable to update order delivery details.',
            extra={'order_id': order_id},
        )
        flash('The delivery details could not be saved. Please try again.', 'error')
        return redirect(url_for('admin.manage_orders'))
    flash(f'Delivery details for order #{order_id} were updated.', 'success')
    return redirect(url_for('admin.manage_orders'))


@admin_bp.post('/orders/<int:order_id>/payment/reconcile')
@admin_required
def reconcile_order_payment(order_id):
    """Recover a missed Razorpay callback using the provider's paid record."""
    order = Order.query.get_or_404(order_id)
    if order.payment_method != 'razorpay':
        flash('Only Razorpay orders can be reconciled here.', 'error')
        return redirect(url_for('admin.manage_orders'))
    try:
        reconciled = reconcile_razorpay_order(order)
        if reconciled.payment_status != 'paid':
            raise RuntimeError('The order could not be marked as paid.')
    except (RuntimeError, SQLAlchemyError, ValueError) as error:
        db.session.rollback()
        current_app.logger.exception(
            'Unable to reconcile Razorpay payment.',
            extra={
                'order_id': order_id,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'total_amount': order.total_amount,
                'razorpay_order_id': order.razorpay_order_id,
            },
        )
        flash(f'Payment reconciliation failed: {str(error)[:250]}', 'error')
        return redirect(url_for('admin.manage_orders'))
    flash(f'Razorpay payment for order #{order_id} was reconciled successfully.', 'success')
    return redirect(url_for('admin.manage_orders'))


@admin_bp.route('/orders/<int:order_id>/status', methods=['POST'])
@admin_required
def update_order_status(order_id):
    order = Order.query.get_or_404(order_id)
    new_status = request.form.get('status')
    valid_statuses = {'pending', 'processing', 'shipped', 'delivered', 'cancelled'}
    if new_status not in valid_statuses:
        flash('Invalid order status.', 'error')
        return redirect(url_for('admin.manage_orders'))
    if order.status == 'cancelled' and new_status != 'cancelled':
        flash('A cancelled order cannot be reopened because its inventory was restored.', 'error')
        return redirect(url_for('admin.manage_orders'))
    old_status = order.status
    shipping_provider = request.form.get('shipping_provider', '').strip() or (order.shipping_provider or '')
    awb_number = request.form.get('awb_number', '').strip() or (order.awb_number or '')
    if bool(shipping_provider) != bool(awb_number):
        flash('Shipping provider and AWB number must be supplied together.', 'error')
        return redirect(url_for('admin.manage_orders'))
    if new_status in {'shipped', 'delivered'} and not shipping_provider:
        flash('Shipped and delivered orders require a shipping provider and AWB number.', 'error')
        return redirect(url_for('admin.manage_orders'))
    if (
        new_status == 'cancelled'
        and old_status != 'cancelled'
        and order.shipping_order_reference
        and order.awb_number
        and current_app.config.get('ITHINK_ENABLED')
    ):
        try:
            IThinkClient().cancel_order(order.awb_number)
        except IThinkRequestError:
            current_app.logger.exception(
                'iThink cancellation failed; local order was not cancelled.',
                extra={'order_id': order.id},
            )
            flash('iThink rejected the cancellation. The local order was not changed.', 'error')
            return redirect(url_for('admin.manage_orders'))
    try:
        if new_status == 'cancelled' and old_status != 'cancelled':
            release_order_inventory(order)
        order.status = new_status
        order.shipping_provider = shipping_provider or None
        order.awb_number = awb_number or None
        if shipping_provider and awb_number:
            order.shipping_tracking_updated_at = utcnow_naive()
            if new_status == 'shipped':
                order.shipping_status = 'Shipped'
            elif new_status == 'delivered':
                order.shipping_status = 'Delivered'
            elif not order.shipping_status:
                order.shipping_status = 'AWB assigned'
        db.session.commit()
    except (RuntimeError, SQLAlchemyError):
        db.session.rollback()
        current_app.logger.exception(
            'Unable to update order status.',
            extra={'order_id': order_id, 'requested_status': new_status},
        )
        flash('The order could not be updated safely. Please try again.', 'error')
        return redirect(url_for('admin.manage_orders'))

    if is_gokwik_payment_method(order.payment_method):
        try:
            synced = sync_gokwik_order(
                order.id,
                order.status,
                order.shipping_provider,
                order.awb_number,
            )
        except GoKwikRequestError:
            current_app.logger.exception(
                'Order was updated locally but GoKwik synchronization failed.',
                extra={'gokwik_order_id': order.id},
            )
            flash(
                f'Order #{order_id} was updated locally, but GoKwik synchronization failed. Retry it.',
                'error',
            )
            return redirect(url_for('admin.manage_orders'))
        if not synced and current_app.config.get('GOKWIK_ENABLED'):
            flash(f'Order #{order_id} updated; no GoKwik status mapping was required.', 'success')
            return redirect(url_for('admin.manage_orders'))

    flash(f'Order #{order_id} status updated to {new_status}.', 'success')
    return redirect(url_for('admin.manage_orders'))


def _ithink_operational_settings() -> dict[str, str]:
    stored = {
        setting.key: setting.value
        for setting in Setting.query.filter(Setting.key.like('ithink_%')).all()
    }
    return {
        'weight': stored.get(
            'ithink_default_weight_kg',
            current_app.config.get('ITHINK_DEFAULT_WEIGHT_KG', '0.5'),
        ),
        'length': stored.get(
            'ithink_default_length_cm',
            current_app.config.get('ITHINK_DEFAULT_LENGTH_CM', '10'),
        ),
        'width': stored.get(
            'ithink_default_width_cm',
            current_app.config.get('ITHINK_DEFAULT_WIDTH_CM', '10'),
        ),
        'height': stored.get(
            'ithink_default_height_cm',
            current_app.config.get('ITHINK_DEFAULT_HEIGHT_CM', '10'),
        ),
    }


def _ithink_available() -> bool:
    if current_app.config.get('ITHINK_ENABLED'):
        return True
    flash('iThink Logistics is disabled in the server environment.', 'error')
    return False


@admin_bp.post('/orders/<int:order_id>/ithink/create')
@admin_required
def create_ithink_shipment(order_id):
    if not _ithink_available():
        return redirect(url_for('admin.manage_orders'))

    order = Order.query.filter_by(id=order_id).with_for_update().first_or_404()
    if order.status in {'cancelled', 'delivered'}:
        flash('Cancelled or delivered orders cannot create a new shipment.', 'error')
        return redirect(url_for('admin.manage_orders'))
    try:
        payment_mode_for_order(order)
    except ValueError as error:
        current_app.logger.warning(
            'Rejected iThink shipment because the order payment state is invalid.',
            extra={
                'order_id': order.id,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'total_amount': order.total_amount,
                'reason': str(error),
            },
        )
        flash(str(error), 'error')
        return redirect(url_for('admin.manage_orders'))

    settings = _ithink_operational_settings()
    try:
        dimensions = parcel_dimensions(
            request.form.get('weight', settings['weight']),
            request.form.get('length', settings['length']),
            request.form.get('width', settings['width']),
            request.form.get('height', settings['height']),
        )
    except ValueError as error:
        flash(str(error), 'error')
        return redirect(url_for('admin.manage_orders'))

    try:
        client = IThinkClient()
        if order.awb_number:
            flash(f'iThink shipment for order #{order.id} already exists.', 'success')
            return redirect(url_for('admin.manage_orders'))
        shipment = create_or_recover_ithink_shipment(order, dimensions, client)
        apply_ithink_shipment(order, shipment)
        if not shipment.get('awb_number'):
            db.session.commit()
            message = (
                f'iThink saved order #{order.id} as {shipment.get("order_reference")} '
                'but did not assign an AWB yet.'
            )
            if shipment.get('remark'):
                message += f" iThink said: {shipment['remark']}"
            flash(f'{message} Retry the shipment action.', 'error')
            return redirect(url_for('admin.manage_orders'))
        db.session.commit()
    except (IThinkRequestError, ValueError, SQLAlchemyError) as error:
        db.session.rollback()
        current_app.logger.exception(
            'Unable to create iThink fulfillment.',
            extra={
                'order_id': order_id,
                'payment_method': order.payment_method,
                'payment_status': order.payment_status,
                'total_amount': order.total_amount,
            },
        )
        flash(
            f'iThink could not create this shipment: {str(error)[:300]}',
            'error',
        )
        return redirect(url_for('admin.manage_orders'))

    if is_gokwik_payment_method(order.payment_method):
        try:
            sync_gokwik_order(
                order.id,
                order.status,
                order.shipping_provider,
                order.awb_number,
            )
        except GoKwikRequestError:
            current_app.logger.exception(
                'iThink shipment was created but GoKwik synchronization failed.',
                extra={'order_id': order.id},
            )
            flash('Shipment created, but GoKwik synchronization failed.', 'error')
            return redirect(url_for('admin.manage_orders'))

    flash(f'iThink shipment for order #{order.id} is ready.', 'success')
    return redirect(url_for('admin.manage_orders'))


@admin_bp.post('/orders/<int:order_id>/ithink/refresh')
@admin_required
def refresh_ithink_tracking(order_id):
    if not _ithink_available():
        return redirect(url_for('admin.manage_orders'))
    order = Order.query.get_or_404(order_id)
    if not order.awb_number:
        flash('This order has no iThink AWB to track.', 'error')
        return redirect(url_for('admin.manage_orders'))
    try:
        tracking_payload = IThinkClient().track_awbs([order.awb_number])
        tracking = extract_tracking_data(tracking_payload, order.awb_number)
        status_changed = apply_ithink_tracking(order, tracking)
        db.session.commit()
    except (IThinkRequestError, ValueError):
        db.session.rollback()
        current_app.logger.exception(
            'Unable to refresh iThink tracking.',
            extra={'order_id': order_id},
        )
        flash('iThink tracking could not be refreshed. Try again.', 'error')
        return redirect(url_for('admin.manage_orders'))
    if status_changed and is_gokwik_payment_method(order.payment_method):
        try:
            sync_gokwik_order(
                order.id,
                order.status,
                order.shipping_provider,
                order.awb_number,
            )
        except GoKwikRequestError:
            current_app.logger.exception(
                'iThink tracking was refreshed but GoKwik synchronization failed.',
                extra={'order_id': order.id},
            )
            flash('Tracking refreshed, but GoKwik synchronization failed.', 'error')
            return redirect(url_for('admin.manage_orders'))
    flash(f'Tracking refreshed for order #{order.id}.', 'success')
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
    ALLOWED_SETTINGS = {
        'shipping_charge',
        'free_shipping_threshold',
        'ithink_default_weight_kg',
        'ithink_default_length_cm',
        'ithink_default_width_cm',
        'ithink_default_height_cm',
    }
    if request.method == 'POST':
        current_ithink_settings = _ithink_operational_settings()
        try:
            shipping_settings = {
                'shipping_charge': _parse_shipping_amount(request.form.get('shipping_charge'), 'Standard shipping charge'),
                'free_shipping_threshold': _parse_shipping_amount(request.form.get('free_shipping_threshold'), 'Free shipping threshold'),
            }
            ithink_dimensions = parcel_dimensions(
                request.form.get(
                    'ithink_default_weight_kg',
                    current_ithink_settings['weight'],
                ),
                request.form.get(
                    'ithink_default_length_cm',
                    current_ithink_settings['length'],
                ),
                request.form.get(
                    'ithink_default_width_cm',
                    current_ithink_settings['width'],
                ),
                request.form.get(
                    'ithink_default_height_cm',
                    current_ithink_settings['height'],
                ),
            )
        except ValueError as error:
            flash(str(error), 'error')
            return redirect(url_for('admin.manage_settings'))

        submitted_settings = {
            **shipping_settings,
            'ithink_default_weight_kg': str(ithink_dimensions['weight']),
            'ithink_default_length_cm': str(ithink_dimensions['length']),
            'ithink_default_width_cm': str(ithink_dimensions['width']),
            'ithink_default_height_cm': str(ithink_dimensions['height']),
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
    ithink_defaults = _ithink_operational_settings()
    all_settings.setdefault('ithink_default_weight_kg', ithink_defaults['weight'])
    all_settings.setdefault('ithink_default_length_cm', ithink_defaults['length'])
    all_settings.setdefault('ithink_default_width_cm', ithink_defaults['width'])
    all_settings.setdefault('ithink_default_height_cm', ithink_defaults['height'])
    return render_template('admin/settings.html', settings=all_settings)

@admin_bp.route('/reviews')
@admin_required
def manage_reviews():
    reviews = Review.query.order_by(Review.created_at.desc()).all()
    return render_template('admin/reviews.html', reviews=reviews)

@admin_bp.route('/review/<int:review_id>/delete', methods=['POST'])
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

@admin_bp.route('/offers/<int:offer_id>/delete', methods=['POST'])
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

        # --- The Zuhraan Story Image Gallery ---
        elif action == 'upload_home_story_images':
            image_files = [image_file for image_file in request.files.getlist('home_story_images') if image_file.filename]
            allowed_image_extensions = {'png', 'jpg', 'jpeg', 'webp'}
            invalid_image = next(
                (image_file for image_file in image_files
                 if '.' not in image_file.filename or image_file.filename.rsplit('.', 1)[1].lower() not in allowed_image_extensions),
                None,
            )
            if not image_files:
                flash('Choose at least one image to upload.', 'error')
            elif invalid_image:
                flash('Invalid image type. Allowed: PNG, JPG, JPEG, WEBP.', 'error')
            else:
                try:
                    about_page = AboutPage.query.first()
                    saved_images = get_home_story_images()
                    legacy_image = None
                    if not saved_images and about_page and about_page.home_about_image:
                        legacy_image = {
                            'url': about_page.home_about_image,
                            'public_id': about_page.home_about_image_pub_id or '',
                        }

                    image_count = len(saved_images) + len(image_files) + (1 if legacy_image else 0)
                    if image_count > MAX_HOME_STORY_IMAGES:
                        flash(f'The Zuhraan Story supports a maximum of {MAX_HOME_STORY_IMAGES} images.', 'error')
                    else:
                        uploaded_images = [
                            {
                                'url': uploaded_image.get('secure_url'),
                                'public_id': uploaded_image.get('public_id'),
                            }
                            for uploaded_image in (
                                cloudinary.uploader.upload(image_file, folder='homepage')
                                for image_file in image_files
                            )
                        ]
                        if legacy_image:
                            saved_images.append(legacy_image)
                            about_page.home_about_image = None
                            about_page.home_about_image_pub_id = None
                        saved_images.extend(uploaded_images)
                        save_home_story_images(saved_images)
                        db.session.commit()
                        flash(f'{len(uploaded_images)} image(s) added to The Zuhraan Story.', 'success')
                except Exception as error:
                    db.session.rollback()
                    flash(f'Image upload error: {str(error)}', 'error')

        elif action == 'delete_home_story_image':
            try:
                image_index = int(request.form.get('image_index', ''))
            except ValueError:
                image_index = -1

            saved_images = get_home_story_images()
            if saved_images and 0 <= image_index < len(saved_images):
                removed_image = saved_images.pop(image_index)
                if removed_image['public_id']:
                    try:
                        cloudinary.uploader.destroy(removed_image['public_id'])
                    except Exception:
                        current_app.logger.warning('The Zuhraan Story image was removed from the site, but the Cloudinary asset could not be deleted.', exc_info=True)
                save_home_story_images(saved_images)
                db.session.commit()
                flash('The Zuhraan Story image removed.', 'success')
            else:
                about_page = AboutPage.query.first()
                if about_page and image_index == 0 and about_page.home_about_image:
                    if about_page.home_about_image_pub_id:
                        try:
                            cloudinary.uploader.destroy(about_page.home_about_image_pub_id)
                        except Exception:
                            current_app.logger.warning('The Zuhraan Story legacy image was removed from the site, but the Cloudinary asset could not be deleted.', exc_info=True)
                    about_page.home_about_image = None
                    about_page.home_about_image_pub_id = None
                    db.session.commit()
                    flash('The Zuhraan Story image removed.', 'success')
                else:
                    flash('The selected story image no longer exists.', 'error')

        # --- Homepage Reels ---
        elif action == 'upload_reel':
            uploaded_public_id = ''
            try:
                reel_index = _reel_index_from_form()
                reel_video = request.files.get('reel_video')
                _validate_reel_video(reel_video)
                reel_slots = get_homepage_reels(url_for('static', filename='videos/reel1.mp4'))
                previous_reel = reel_slots[reel_index]
                upload_result = cloudinary.uploader.upload(
                    reel_video,
                    folder='zuhraan/homepage/reels',
                    resource_type='video',
                    use_filename=True,
                    unique_filename=True,
                    overwrite=False,
                )
                uploaded_url = upload_result.get('secure_url')
                uploaded_public_id = upload_result.get('public_id')
                if not uploaded_url or not uploaded_public_id:
                    raise RuntimeError('Cloudinary did not return a reel URL and public ID.')

                reel_slots[reel_index] = {
                    'url': uploaded_url,
                    'public_id': uploaded_public_id,
                    'title': previous_reel['title'] if previous_reel else REEL_TITLES[reel_index],
                }
                save_homepage_reels(reel_slots)
                db.session.commit()

                if previous_reel and previous_reel['public_id']:
                    try:
                        _delete_cloudinary_reel(previous_reel['public_id'])
                    except CloudinaryError:
                        current_app.logger.warning(
                            'The previous homepage reel could not be deleted from Cloudinary.',
                            extra={'reel_index': reel_index, 'public_id': previous_reel['public_id']},
                            exc_info=True,
                        )
                flash(f'Reel {reel_index + 1} uploaded successfully.', 'success')
            except (ValueError, RuntimeError, SQLAlchemyError, CloudinaryError) as error:
                db.session.rollback()
                if uploaded_public_id:
                    try:
                        _delete_cloudinary_reel(uploaded_public_id)
                    except CloudinaryError:
                        current_app.logger.warning(
                            'A failed homepage reel upload could not be removed from Cloudinary.',
                            extra={'public_id': uploaded_public_id},
                            exc_info=True,
                        )
                flash(f'Reel upload error: {error}', 'error')

        elif action == 'delete_reel':
            try:
                reel_index = _reel_index_from_form()
                reel_slots = get_homepage_reels(url_for('static', filename='videos/reel1.mp4'))
                removed_reel = reel_slots[reel_index]
                if removed_reel is None:
                    raise ValueError('The selected reel no longer exists.')
                reel_slots[reel_index] = None
                save_homepage_reels(reel_slots)
                db.session.commit()

                if removed_reel['public_id']:
                    try:
                        _delete_cloudinary_reel(removed_reel['public_id'])
                    except CloudinaryError:
                        current_app.logger.warning(
                            'The homepage reel was removed from the site, but its Cloudinary asset could not be deleted.',
                            extra={'reel_index': reel_index, 'public_id': removed_reel['public_id']},
                            exc_info=True,
                        )
                flash(f'Reel {reel_index + 1} removed.', 'success')
            except (ValueError, SQLAlchemyError) as error:
                db.session.rollback()
                flash(f'Unable to remove reel: {error}', 'error')

        elif action == 'update_reel_title':
            try:
                reel_index = _reel_index_from_form()
                reel_title = _reel_title_from_form()
                reel_slots = get_homepage_reels(url_for('static', filename='videos/reel1.mp4'))
                reel = reel_slots[reel_index]
                if reel is None:
                    raise ValueError('Upload the reel video before editing its text.')
                reel_slots[reel_index] = {
                    'url': reel['url'],
                    'public_id': reel['public_id'],
                    'title': reel_title,
                }
                save_homepage_reels(reel_slots)
                db.session.commit()
                flash(f'Reel {reel_index + 1} text updated.', 'success')
            except (ValueError, SQLAlchemyError) as error:
                db.session.rollback()
                flash(f'Unable to update reel text: {error}', 'error')
        
        # --- Update Max Reels ---
        elif action == 'update_max_reels':
            try:
                new_max = int(request.form.get('max_reels', ''))
                if new_max < 1 or new_max > 20:
                    raise ValueError('Max reels must be between 1 and 20.')
                current_max = _get_max_reels()
                if new_max != current_max:
                    set_max_reels(new_max)
                    db.session.commit()
                    flash(f'Maximum reel slots updated to {new_max}.', 'success')
                else:
                    flash('No change in maximum reel slots.', 'info')
            except (ValueError, SQLAlchemyError) as error:
                db.session.rollback()
                flash(f'Unable to update max reels: {error}', 'error')

        return redirect(url_for('admin.homepage_media'))

    media_url = (Setting.query.filter_by(key='homepage_media_url').first() or Setting(value='')).value
    media_type = (Setting.query.filter_by(key='homepage_media_type').first() or Setting(value='')).value
    banner_url = (Setting.query.filter_by(key='bottom_banner_url').first() or Setting(value='')).value
    hero_about_image_url = (Setting.query.filter_by(key='hero_about_image_url').first() or Setting(value='')).value
    about_page = AboutPage.query.first()
    home_story_images = get_home_story_images()
    if not home_story_images and about_page and about_page.home_about_image:
        home_story_images = [{'url': about_page.home_about_image, 'public_id': about_page.home_about_image_pub_id or ''}]
    homepage_reels = get_homepage_reels(url_for('static', filename='videos/reel1.mp4'))
    return render_template(
        'admin/homepage_media.html',
        media_url=media_url,
        media_type=media_type,
        banner_url=banner_url,
        hero_about_image_url=hero_about_image_url,
        home_story_images=home_story_images,
        max_home_story_images=MAX_HOME_STORY_IMAGES,
        homepage_reels=homepage_reels,
        homepage_reel_count=sum(reel is not None for reel in homepage_reels),
        max_homepage_reels=_get_max_reels(),
    )

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
