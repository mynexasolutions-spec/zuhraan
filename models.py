from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=True) # Added for personalized narrative
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user') # 'admin' or 'user'
    
    # Saved Address Fields
    phone = db.Column(db.String(20))
    address_line1 = db.Column(db.String(255))
    address_line2 = db.Column(db.String(255))
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    pincode = db.Column(db.String(20))
    country = db.Column(db.String(100), default='India')
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Category(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    image = db.Column(db.String(255), nullable=True) # Category cover image/Cloudinary URL
    image_pub_id = db.Column(db.String(255), nullable=True)
    products = db.relationship('Product', backref='category', lazy=True)

class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    slug = db.Column(db.String(150), unique=True, nullable=True) # SEO Friendly URL
    category_id = db.Column(db.Integer, db.ForeignKey('category.id'), nullable=False)
    
    short_description = db.Column(db.Text)
    full_description = db.Column(db.Text)
    
    # Fragrance specific notes
    top_notes = db.Column(db.String(255))
    middle_notes = db.Column(db.String(255))
    base_notes = db.Column(db.String(255))
    
    # Stats
    longevity = db.Column(db.String(50)) # e.g. "Long Lasting", "Moderate"
    projection = db.Column(db.String(50)) # e.g. "Strong", "Intimate"
    
    # Images - Store multiple image paths as a JSON or comma-separated string
    images = db.Column(db.Text) 
    image_pub_ids = db.Column(db.Text) # Comma-separated public ids for bulk delete
    
    # Special tag for homepage features: 'best_seller', 'new_arrival', 'featured'
    tag = db.Column(db.String(50), nullable=True)
    # Rank 1-4 controls order in Crown Jewels; only meaningful when tag='best_seller'
    best_seller_rank = db.Column(db.Integer, nullable=True)
    
    variants = db.relationship('ProductVariant', backref='product', lazy=True, cascade="all, delete-orphan")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class ProductVariant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    size = db.Column(db.String(20)) # "50ml" or "100ml"
    price = db.Column(db.Float, nullable=False) # This is the current/sale price
    original_price = db.Column(db.Float) # The MSRP/Original price for sale comparison
    stock_quantity = db.Column(db.Integer, default=0)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Null for guest orders
    customer_name = db.Column(db.String(100))
    customer_email = db.Column(db.String(120))
    customer_phone = db.Column(db.String(20))
    
    # --- Granular Address Fields ---
    shipping_address = db.Column(db.Text)           # kept for legacy data
    address_line1 = db.Column(db.String(255))       # House/Flat no., Street
    address_line2 = db.Column(db.String(255))       # Landmark, Area (optional)
    city = db.Column(db.String(100))
    state = db.Column(db.String(100))
    pincode = db.Column(db.String(20))
    country = db.Column(db.String(100), default='India')

    status = db.Column(db.String(50), default='pending') # pending, processing, shipped, delivered, cancelled
    total_amount = db.Column(db.Float, nullable=False)
    shipping_charges = db.Column(db.Float, default=0.0)
    
    payment_status = db.Column(db.String(50), default='unpaid') # unpaid, paid
    razorpay_order_id = db.Column(db.String(100))
    razorpay_payment_id = db.Column(db.String(100))
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id'), nullable=True) # To safely increment usage
    
    items = db.relationship('OrderItem', backref='order', lazy=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    variant_id = db.Column(db.Integer, db.ForeignKey('product_variant.id'), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price_at_time = db.Column(db.Float, nullable=False) # Snapshot price

    variant = db.relationship('ProductVariant')

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text)

class Review(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey('product.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) # Guest reviews allow
    customer_name = db.Column(db.String(100), nullable=False)
    rating = db.Column(db.Integer, nullable=False) # 1-5
    comment = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_approved = db.Column(db.Boolean, default=True) # Auto-approve for now, can be changed

    product = db.relationship('Product', backref=db.backref('reviews', lazy=True, cascade="all, delete-orphan"))

class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)           # e.g. "SAVE20"
    discount_type = db.Column(db.String(10), nullable=False, default='percent')  # 'percent' or 'fixed'
    discount_value = db.Column(db.Float, nullable=False)                   # 20 (= 20% or ₹20)
    min_order_amount = db.Column(db.Float, default=0.0)                    # minimum cart value
    max_uses = db.Column(db.Integer, nullable=True)                        # null = unlimited
    used_count = db.Column(db.Integer, default=0)
    is_active = db.Column(db.Boolean, default=True)
    expires_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class OfferBanner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    image = db.Column(db.String(255), nullable=False)
    image_pub_id = db.Column(db.String(255), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

import cloudinary.uploader
from sqlalchemy import event

def auto_delete_category_image(mapper, connection, target):
    if target.image_pub_id:
        try: cloudinary.uploader.destroy(target.image_pub_id)
        except: pass

def auto_delete_product_images(mapper, connection, target):
    if target.image_pub_ids:
        pub_ids = target.image_pub_ids.split(',')
        for pid in pub_ids:
            if pid:
                try: cloudinary.uploader.destroy(pid)
                except: pass

def auto_delete_offer_image(mapper, connection, target):
    if target.image_pub_id:
        try: cloudinary.uploader.destroy(target.image_pub_id)
        except: pass

event.listen(Category, 'after_delete', auto_delete_category_image)
event.listen(Product, 'after_delete', auto_delete_product_images)
event.listen(OfferBanner, 'after_delete', auto_delete_offer_image)
