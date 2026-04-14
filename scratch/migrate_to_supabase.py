import os
from sqlalchemy import create_engine, MetaData, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

# Load Env
load_dotenv()

local_url = 'sqlite:///instance/zuhraan_v2.db'
remote_url = os.environ.get('DATABASE_URL')
cloudinary_url = os.environ.get('CLOUDINARY_URL')

if not remote_url or not cloudinary_url:
    print("Database URL or Cloudinary URL not found in .env")
    exit(1)

import cloudinary.uploader

# Engines
local_engine = create_engine(local_url)
remote_engine = create_engine(remote_url)

# Reflect local DB
local_meta = MetaData()
local_meta.reflect(bind=local_engine)

# To create tables in remote, we need to import models and bind them
# But app context is required for db.create_all()
from app import app
from models import db, User, Category, Product, ProductVariant, Review, Order, OrderItem, Setting, Coupon, OfferBanner

app.config['SQLALCHEMY_DATABASE_URI'] = remote_url

print("Starting Migration...")

with app.app_context():
    # Setup remote DB structure
    db.create_all()
    
    def upload_to_cloudinary(local_rel_path, folder_name="zuhraan"):
        try:
            if not local_rel_path: return None, None
            # Construct absolute path to local static folder
            abs_path = os.path.join(app.root_path, 'static', local_rel_path.replace('/', os.sep))
            if not os.path.exists(abs_path):
                print(f"Warning: Local file not found {abs_path}")
                # Try handling if it's already a URL
                if local_rel_path.startswith('http'):
                    return local_rel_path, None
                return None, None
                
            upload_result = cloudinary.uploader.upload(abs_path, folder=folder_name)
            print(f"Uploaded {local_rel_path} -> {upload_result.get('secure_url')}")
            return upload_result.get('secure_url'), upload_result.get('public_id')
        except Exception as e:
            print(f"Cloudinary upload failed for {local_rel_path}: {str(e)}")
            return None, None
    
    with local_engine.connect() as local_conn, remote_engine.begin() as remote_conn:
        def exists_remotely(table, row_id):
            return remote_conn.execute(text(f"SELECT 1 FROM \"{table}\" WHERE id = :id"), {"id": row_id}).scalar() is not None

        # Migrate Categories
        print("-- Migrating Categories")
        for row in local_conn.execute(text("SELECT * FROM category")).fetchall():
            if exists_remotely("category", row.id): continue
            img_url, img_pub = upload_to_cloudinary(row.image, "categories")
            remote_conn.execute(text("INSERT INTO category (id, name, image, image_pub_id) VALUES (:id, :name, :image, :image_pub_id)"), 
                {"id": row.id, "name": row.name, "image": img_url, "image_pub_id": img_pub})

        # Migrate Products
        print("-- Migrating Products")
        for row in local_conn.execute(text("SELECT * FROM product")).fetchall():
            if exists_remotely("product", row.id): continue
            local_images = row.images.split(',') if row.images else []
            remote_images, remote_pubs = [], []
            for img in local_images:
                if img:
                    url, pub = upload_to_cloudinary(img, "products")
                    if url: remote_images.append(url); remote_pubs.append(pub)
            remote_conn.execute(text("""INSERT INTO product (id, name, slug, category_id, short_description, full_description, 
                                        top_notes, middle_notes, base_notes, longevity, projection, images, image_pub_ids, 
                                        tag, best_seller_rank, created_at) VALUES (:id, :name, :slug, :cat_id, :sd, :fd, :tn, :mn, :bn, :l, :p, :im, :ip, :tag, :bsr, :ca)"""),
               {"id": row.id, "name": row.name, "slug": row.slug, "cat_id": row.category_id, "sd": row.short_description, "fd": row.full_description,
                "tn": row.top_notes, "mn": row.middle_notes, "bn": row.base_notes, "l": row.longevity, "p": row.projection,
                "im": ','.join(remote_images), "ip": ','.join(filter(None, remote_pubs)), "tag": row.tag, "bsr": row.best_seller_rank, "ca": row.created_at})

        # Migrate ProductVariants
        print("-- Migrating Variants")
        for row in local_conn.execute(text("SELECT * FROM product_variant")).fetchall():
            if exists_remotely("product_variant", row.id): continue
            remote_conn.execute(text("INSERT INTO product_variant (id, product_id, size, price, original_price, stock_quantity) VALUES (:id, :pid, :size, :price, :op, :sq)"),
                {"id": row.id, "pid": row.product_id, "size": row.size, "price": row.price, "op": row.original_price, "sq": row.stock_quantity})

        # Migrate Users
        print("-- Migrating Users")
        for row in local_conn.execute(text("SELECT * FROM \"user\"")).fetchall():
            if exists_remotely("user", row.id): continue
            remote_conn.execute(text("""INSERT INTO "user" (id, email, name, password, role, phone, address_line1, address_line2, city, state, pincode, country, created_at)
                                        VALUES (:id, :email, :name, :pw, :role, :ph, :a1, :a2, :city, :st, :pc, :c, :ca)"""),
                {"id": row.id, "email": row.email, "name": row.name, "pw": row.password, "role": row.role, "ph": row.phone, "a1": row.address_line1, "a2": row.address_line2, 
                 "city": row.city, "st": row.state, "pc": row.pincode, "c": row.country, "ca": row.created_at})

        # Migrate Orders
        print("-- Migrating Orders")
        for row in local_conn.execute(text("SELECT * FROM \"order\"")).fetchall():
            if exists_remotely("order", row.id): continue
            remote_conn.execute(text("""INSERT INTO "order" (id, user_id, total_amount, status, payment_status, razorpay_order_id, razorpay_payment_id, shipping_address, customer_name, customer_email, customer_phone, created_at)
                                        VALUES (:id, :uid, :ta, :st, :ps, :roi, :rpi, :sa, :cn, :ce, :cp, :ca)"""),
                {"id": row.id, "uid": row.user_id, "ta": row.total_amount, "st": row.status, "ps": row.payment_status, "roi": row.razorpay_order_id, "rpi": row.razorpay_payment_id,
                 "sa": row.shipping_address, "cn": getattr(row, 'customer_name', ''), "ce": getattr(row, 'customer_email', ''), "cp": getattr(row, 'customer_phone', ''), "ca": row.created_at})

        # Migrate OrderItems
        print("-- Migrating Order Items")
        for row in local_conn.execute(text("SELECT * FROM order_item")).fetchall():
            if exists_remotely("order_item", row.id): continue
            remote_conn.execute(text("INSERT INTO order_item (id, order_id, variant_id, quantity, price_at_time) VALUES (:id, :oid, :vid, :q, :pat)"),
                {"id": row.id, "oid": row.order_id, "vid": row.variant_id, "q": row.quantity, "pat": row.price_at_time})

        # Migrate Reviews
        print("-- Migrating Reviews")
        for row in local_conn.execute(text("SELECT * FROM review")).fetchall():
            if exists_remotely("review", row.id): continue
            remote_conn.execute(text("INSERT INTO review (id, product_id, user_id, customer_name, rating, comment, is_approved, created_at) VALUES (:id, :pid, :uid, :cn, :r, :c, :ia, :ca)"),
                {"id": row.id, "pid": row.product_id, "uid": row.user_id, "cn": row.customer_name, "r": row.rating, "c": row.comment, "ia": bool(row.is_approved), "ca": row.created_at})

        # Migrate Settings
        print("-- Migrating Settings")
        for row in local_conn.execute(text("SELECT * FROM setting")).fetchall():
            if exists_remotely("setting", row.id): continue
            remote_conn.execute(text("INSERT INTO setting (id, key, value) VALUES (:id, :k, :v)"), {"id": row.id, "k": row.key, "v": row.value})

        # Migrate Coupons
        print("-- Migrating Coupons")
        for row in local_conn.execute(text("SELECT * FROM coupon")).fetchall():
            if exists_remotely("coupon", row.id): continue
            remote_conn.execute(text("""INSERT INTO coupon (id, code, discount_type, discount_value, min_order_amount, max_uses, used_count, is_active, expires_at, created_at)
                                        VALUES (:id, :c, :dt, :dv, :moa, :mu, :uc, :ia, :ea, :ca)"""),
                {"id": row.id, "c": row.code, "dt": row.discount_type, "dv": row.discount_value, "moa": row.min_order_amount, "mu": row.max_uses, "uc": row.used_count, "ia": bool(row.is_active), "ea": row.expires_at, "ca": row.created_at})

        # Migrate OfferBanners
        print("-- Migrating OffferBanners")
        for row in local_conn.execute(text("SELECT * FROM offer_banner")).fetchall():
            if exists_remotely("offer_banner", row.id): continue
            url, pub = upload_to_cloudinary(row.image, "offers")
            remote_conn.execute(text("INSERT INTO offer_banner (id, image, image_pub_id, is_active, created_at) VALUES (:id, :im, :ip, :ia, :ca)"),
                {"id": row.id, "im": url or row.image, "ip": pub, "ia": bool(row.is_active), "ca": row.created_at})
        
        # Postgres sequence update
        print("-- Updating Postgres sequences")
        tables = [
            ('category', 'id'), ('product', 'id'), ('product_variant', 'id'), 
            ('user', 'id'), ('order', 'id'), ('order_item', 'id'), 
            ('review', 'id'), ('setting', 'id'), ('coupon', 'id'), ('offer_banner', 'id')
        ]
        for table, col in tables:
            remote_conn.execute(text(f"SELECT setval('\"{table}_{col}_seq\"', COALESCE((SELECT MAX({col}) FROM \"{table}\")+1, 1), false);"))
        
    print("Migration Complete!")
