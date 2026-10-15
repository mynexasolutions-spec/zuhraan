import os
import json
from io import BytesIO

os.environ['APP_ENV'] = 'testing'
os.environ['SUPABASE_DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'
os.environ['SHIPROCKET_ENABLED'] = '0'

import pytest
from werkzeug.security import generate_password_hash

from app import app
from models import Category, Coupon, Order, OrderItem, Product, ProductVariant, Setting, User, db
from routes.main import calculate_shipping, safe_internal_redirect
import routes.admin as admin_routes


@pytest.fixture(autouse=True)
def isolated_database():
    app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client():
    return app.test_client()


def create_product(price):
    category = Category(name='Test Category', slug='test-category')
    db.session.add(category)
    db.session.flush()
    product = Product(name='Test Perfume', slug='test-perfume', category_id=category.id)
    db.session.add(product)
    db.session.flush()
    variant = ProductVariant(product_id=product.id, size='50ml', price=price, stock_quantity=10)
    db.session.add(variant)
    db.session.flush()
    variant_id = variant.id
    db.session.commit()
    return variant_id


def add_admin_session(client, admin_id):
    with client.session_transaction() as session:
        session['_user_id'] = str(admin_id)
        session['_fresh'] = True


def test_admin_can_replace_and_remove_homepage_reel(client, monkeypatch):
    with app.app_context():
        admin = User(email='reels-admin@example.test', password='test-password', role='admin')
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id

    uploaded_assets = iter([
        {
            'secure_url': 'https://res.cloudinary.com/example/video/upload/reel-one.mp4',
            'public_id': 'zuhraan/homepage/reels/reel-one',
        },
        {
            'secure_url': 'https://res.cloudinary.com/example/video/upload/reel-two.mp4',
            'public_id': 'zuhraan/homepage/reels/reel-two',
        },
    ])
    deleted_assets = []

    def fake_upload(_file, **_options):
        return next(uploaded_assets)

    def fake_destroy(public_id, **options):
        deleted_assets.append((public_id, options))
        return {'result': 'ok'}

    monkeypatch.setattr(admin_routes.cloudinary.uploader, 'upload', fake_upload)
    monkeypatch.setattr(admin_routes.cloudinary.uploader, 'destroy', fake_destroy)
    add_admin_session(client, admin_id)

    admin_page = client.get('/admin/homepage-media')
    assert admin_page.status_code == 200
    assert admin_page.get_data(as_text=True).count('name="reel_video"') == 5
    assert admin_page.get_data(as_text=True).count('name="reel_title"') == 5

    title_response = client.post(
        '/admin/homepage-media',
        data={
            'action': 'update_reel_title',
            'reel_index': '0',
            'reel_title': 'A Custom Fragrance Story',
        },
    )
    assert title_response.status_code == 302

    homepage_with_custom_title = client.get('/').get_data(as_text=True)
    assert 'A Custom Fragrance Story' in homepage_with_custom_title

    upload_response = client.post(
        '/admin/homepage-media',
        data={
            'action': 'upload_reel',
            'reel_index': '0',
            'reel_video': (BytesIO(b'first-video'), 'first-reel.mp4'),
        },
        content_type='multipart/form-data',
    )
    assert upload_response.status_code == 302

    with app.app_context():
        saved_reels = json.loads(Setting.query.filter_by(key='homepage_reels').one().value)
        assert saved_reels[0]['url'].endswith('/reel-one.mp4')
        assert saved_reels[0]['public_id'] == 'zuhraan/homepage/reels/reel-one'
        assert saved_reels[0]['title'] == 'A Custom Fragrance Story'
        assert saved_reels[1]['url'].endswith('/static/videos/reel1.mp4')

    homepage = client.get('/')
    homepage_body = homepage.get_data(as_text=True)
    assert homepage.status_code == 200
    assert 'https://res.cloudinary.com/example/video/upload/reel-one.mp4' in homepage_body

    replace_response = client.post(
        '/admin/homepage-media',
        data={
            'action': 'upload_reel',
            'reel_index': '0',
            'reel_video': (BytesIO(b'second-video'), 'second-reel.webm'),
        },
        content_type='multipart/form-data',
    )
    assert replace_response.status_code == 302
    assert deleted_assets[0][0] == 'zuhraan/homepage/reels/reel-one'
    assert deleted_assets[0][1]['resource_type'] == 'video'

    delete_response = client.post(
        '/admin/homepage-media',
        data={'action': 'delete_reel', 'reel_index': '0'},
    )
    assert delete_response.status_code == 302
    assert deleted_assets[1][0] == 'zuhraan/homepage/reels/reel-two'

    with app.app_context():
        saved_reels = json.loads(Setting.query.filter_by(key='homepage_reels').one().value)
        assert saved_reels[0] is None

    homepage_after_delete = client.get('/').get_data(as_text=True)
    assert 'https://res.cloudinary.com/example/video/upload/reel-two.mp4' not in homepage_after_delete
    assert homepage_after_delete.count('class="reel-card"') == 4


def test_cart_add_returns_success_message_and_updated_count(client):
    with app.app_context():
        variant_id = create_product(149.0)

    response = client.post(
        '/cart/add',
        data={'variant_id': variant_id, 'quantity': 2},
        headers={'Accept': 'application/json'},
    )

    assert response.status_code == 200
    assert response.get_json() == {'message': 'Test Perfume added to cart!', 'cart_count': 2}


def test_health_check_verifies_database_connection(client):
    response = client.get('/healthz')

    assert response.status_code == 200
    assert response.get_json() == {'status': 'ok'}


def test_shipping_settings_persist_and_calculate_standard_charge(client):
    with app.app_context():
        admin = User(email='admin@example.test', password='test-password', role='admin')
        db.session.add(admin)
        db.session.commit()
        admin_id = admin.id

    add_admin_session(client, admin_id)
    response = client.post(
        '/admin/settings',
        data={
            'shipping_charge': '79',
            'free_shipping_threshold': '999',
        },
    )

    assert response.status_code == 200
    with app.app_context():
        assert Setting.query.filter_by(key='shipping_charge').one().value == '79.00'
        assert Setting.query.filter_by(key='free_shipping_threshold').one().value == '999.00'

        assert calculate_shipping(149.0, {
            'shipping_charge': '79.00',
            'free_shipping_threshold': '999.00',
        }) == 79.0


@pytest.mark.parametrize(
    ('subtotal', 'expected_shipping'),
    [(998.99, 79.0), (999.0, 0.0), (1200.0, 0.0)],
)
def test_shipping_becomes_free_at_or_above_threshold(subtotal, expected_shipping):
    settings = {'shipping_charge': '79.00', 'free_shipping_threshold': '999.00'}

    assert calculate_shipping(subtotal, settings) == expected_shipping




def test_cancelling_order_restores_stock_only_once(client):
    with app.app_context():
        admin = User(email='admin@example.test', password='test-password', role='admin')
        db.session.add(admin)
        db.session.flush()
        admin_id = admin.id
        variant_id = create_product(149.0)
        variant = db.session.get(ProductVariant, variant_id)
        variant.stock_quantity = 8
        order = Order(total_amount=298.0, payment_method='cod', status='pending')
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(
            order_id=order.id,
            variant_id=variant_id,
            quantity=2,
            price_at_time=149.0,
        ))
        db.session.commit()
        order_id = order.id
    add_admin_session(client, admin_id)

    first = client.post(f'/admin/orders/{order_id}/status', data={'status': 'cancelled'})
    second = client.post(f'/admin/orders/{order_id}/status', data={'status': 'cancelled'})

    assert first.status_code == 302
    assert second.status_code == 302
    with app.app_context():
        assert db.session.get(ProductVariant, variant_id).stock_quantity == 10


def test_product_with_order_history_cannot_be_deleted(client):
    with app.app_context():
        admin = User(email='admin@example.test', password='test-password', role='admin')
        db.session.add(admin)
        db.session.flush()
        admin_id = admin.id
        variant_id = create_product(149.0)
        product_id = db.session.get(ProductVariant, variant_id).product_id
        order = Order(total_amount=149.0)
        db.session.add(order)
        db.session.flush()
        db.session.add(OrderItem(
            order_id=order.id,
            variant_id=variant_id,
            quantity=1,
            price_at_time=149.0,
        ))
        db.session.commit()
    add_admin_session(client, admin_id)

    assert client.get(f'/admin/product/{product_id}/delete').status_code == 405
    assert client.post(f'/admin/product/{product_id}/delete').status_code == 302
    with app.app_context():
        assert db.session.get(Product, product_id) is not None
        assert OrderItem.query.count() == 1


def test_state_changes_require_post_and_external_redirects_are_rejected(client):
    assert client.get('/account/logout').status_code == 405
    assert client.get('/cart/remove/1').status_code == 405
    assert safe_internal_redirect('/account') == '/account'
    assert safe_internal_redirect('//example.com') == ''
    assert safe_internal_redirect('https://example.com') == ''
