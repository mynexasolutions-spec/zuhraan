import os

os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
os.environ['SECRET_KEY'] = 'test-secret'

import pytest

from app import app
from models import Category, Product, ProductVariant, Setting, User, db
from routes.main import calculate_shipping


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


def test_shipping_settings_persist_and_checkout_adds_standard_charge(client):
    with app.app_context():
        admin = User(email='admin@example.test', password='test-password', role='admin')
        db.session.add(admin)
        db.session.flush()
        admin_id = admin.id
        variant_id = create_product(149.0)

    add_admin_session(client, admin_id)
    response = client.post(
        '/admin/settings',
        data={
            'shipping_charge': '79',
            'free_shipping_threshold': '999',
            'payment_cod_enabled': '1',
            'payment_online_enabled': '1',
        },
    )

    assert response.status_code == 200
    with app.app_context():
        assert Setting.query.filter_by(key='shipping_charge').one().value == '79.00'
        assert Setting.query.filter_by(key='free_shipping_threshold').one().value == '999.00'

    with client.session_transaction() as session:
        session['cart'] = {str(variant_id): 1}
    checkout = client.get('/checkout')
    checkout_body = checkout.get_data(as_text=True)

    assert checkout.status_code == 200
    assert '79.00' in checkout_body
    assert '228.00' in checkout_body


def test_checkout_shows_zero_shipping_and_subtotal_at_free_shipping_threshold(client):
    with app.app_context():
        variant_id = create_product(999.0)
        db.session.add_all([
            Setting(key='shipping_charge', value='79.00'),
            Setting(key='free_shipping_threshold', value='999.00'),
            Setting(key='payment_cod_enabled', value='1'),
            Setting(key='payment_online_enabled', value='1'),
        ])
        db.session.commit()

    with client.session_transaction() as session:
        session['cart'] = {str(variant_id): 1}
    checkout = client.get('/checkout')
    checkout_body = checkout.get_data(as_text=True)

    assert checkout.status_code == 200
    assert 'id="shippingAmount"' in checkout_body
    assert '&#8377;0.00' in checkout_body
    assert '999.00' in checkout_body


@pytest.mark.parametrize(
    ('subtotal', 'expected_shipping'),
    [(998.99, 79.0), (999.0, 0.0), (1200.0, 0.0)],
)
def test_shipping_becomes_free_at_or_above_threshold(subtotal, expected_shipping):
    settings = {'shipping_charge': '79.00', 'free_shipping_threshold': '999.00'}

    assert calculate_shipping(subtotal, settings) == expected_shipping
