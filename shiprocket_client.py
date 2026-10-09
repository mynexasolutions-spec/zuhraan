import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from threading import Lock
from typing import Any
from urllib.parse import quote

import requests
from flask import current_app

from models import Order


SHIPROCKET_API_BASE_URL = 'https://apiv2.shiprocket.in/v1/external'
COD_PAYMENT_METHODS = frozenset({'cod', 'native_cod'})
_token_lock = Lock()
_cached_token: str | None = None
_token_expires_at: datetime | None = None


class ShiprocketRequestError(RuntimeError):
    """Raised when Shiprocket rejects a request or cannot be reached safely."""


def _positive_decimal(value: Any, label: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f'{label} must be a valid number.') from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f'{label} must be greater than zero.')
    return amount


def parcel_dimensions(
    weight: Any,
    length: Any,
    breadth: Any,
    height: Any,
) -> dict[str, float]:
    """Validate Shiprocket parcel measurements and return API-ready floats."""
    return {
        'weight': float(_positive_decimal(weight, 'Weight')),
        'length': float(_positive_decimal(length, 'Length')),
        'breadth': float(_positive_decimal(breadth, 'Breadth')),
        'height': float(_positive_decimal(height, 'Height')),
    }


def _response_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ('message', 'error', 'status'):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value[:500]
        errors = payload.get('errors')
        if errors:
            return str(errors)[:500]
    return 'Shiprocket returned an unexpected response.'


class ShiprocketClient:
    """Authenticated boundary for the Shiprocket external API."""

    def __init__(self) -> None:
        self.email = str(current_app.config.get('SHIPROCKET_EMAIL') or '')
        self.password = str(current_app.config.get('SHIPROCKET_PASSWORD') or '')
        if not self.email or not self.password:
            raise ShiprocketRequestError('Shiprocket API credentials are not configured.')

    def authenticate(self, force: bool = False) -> str:
        global _cached_token, _token_expires_at

        now = datetime.now(timezone.utc)
        if not force and _cached_token and _token_expires_at and now < _token_expires_at:
            return _cached_token

        with _token_lock:
            now = datetime.now(timezone.utc)
            if not force and _cached_token and _token_expires_at and now < _token_expires_at:
                return _cached_token

            payload = self._send(
                'POST',
                '/auth/login',
                {'email': self.email, 'password': self.password},
                authenticated=False,
                retry_safe=True,
            )
            token = payload.get('token') if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                raise ShiprocketRequestError('Shiprocket authentication returned no token.')

            _cached_token = token
            # Shiprocket documents a 10-day token lifetime. Refresh one day early.
            _token_expires_at = now + timedelta(days=9)
            return token

    def _send(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None,
        authenticated: bool,
        retry_safe: bool,
    ) -> dict[str, Any]:
        attempts = 3 if retry_safe else 1
        force_authentication = False
        final_error: ShiprocketRequestError | None = None

        for attempt in range(1, attempts + 1):
            headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
            if authenticated:
                headers['Authorization'] = f'Bearer {self.authenticate(force_authentication)}'
            try:
                response = requests.request(
                    method,
                    SHIPROCKET_API_BASE_URL + path,
                    json=json_body,
                    headers=headers,
                    timeout=15,
                )
            except requests.RequestException as error:
                final_error = ShiprocketRequestError(f'Shiprocket request failed: {error}')
                if attempt < attempts:
                    time.sleep(0.5 * attempt)
                    continue
                raise final_error from error

            try:
                payload: Any = response.json()
            except ValueError:
                payload = {}

            if response.status_code == 401 and authenticated and not force_authentication:
                force_authentication = True
                if not retry_safe:
                    headers['Authorization'] = f'Bearer {self.authenticate(True)}'
                    try:
                        response = requests.request(
                            method,
                            SHIPROCKET_API_BASE_URL + path,
                            json=json_body,
                            headers=headers,
                            timeout=15,
                        )
                        payload = response.json()
                    except (requests.RequestException, ValueError) as error:
                        raise ShiprocketRequestError(
                            'Shiprocket request failed after refreshing authentication.'
                        ) from error
                else:
                    continue

            if 200 <= response.status_code < 300:
                if not isinstance(payload, dict):
                    raise ShiprocketRequestError('Shiprocket returned invalid JSON data.')
                embedded_status = payload.get('status_code')
                if isinstance(embedded_status, int) and embedded_status >= 400:
                    raise ShiprocketRequestError(_response_message(payload))
                return payload

            final_error = ShiprocketRequestError(
                f'Shiprocket returned HTTP {response.status_code}: {_response_message(payload)}'
            )
            if retry_safe and response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                time.sleep(0.5 * attempt)
                continue
            raise final_error

        if final_error is None:
            raise ShiprocketRequestError('Shiprocket request failed without a response.')
        raise final_error

    def create_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._send('POST', '/orders/create/adhoc', payload, True, False)

    def assign_awb(self, shipment_id: str, courier_id: int | None) -> dict[str, Any]:
        payload: dict[str, Any] = {'shipment_id': shipment_id}
        if courier_id is not None:
            payload['courier_id'] = courier_id
        return self._send('POST', '/courier/assign/awb', payload, True, False)

    def schedule_pickup(self, shipment_id: str) -> dict[str, Any]:
        payload = self._send(
            'POST',
            '/courier/generate/pickup',
            {'shipment_id': [shipment_id]},
            True,
            False,
        )
        pickup_status = payload.get('pickup_status')
        if pickup_status is not None and str(pickup_status).lower() not in {'1', 'true'}:
            raise ShiprocketRequestError(_response_message(payload))
        return payload

    def track_awb(self, awb_number: str) -> dict[str, Any]:
        encoded_awb = quote(awb_number, safe='')
        return self._send('GET', f'/courier/track/awb/{encoded_awb}', None, True, True)

    def cancel_order(self, shiprocket_order_id: str) -> dict[str, Any]:
        return self._send(
            'POST',
            '/orders/cancel',
            {'ids': [shiprocket_order_id]},
            True,
            False,
        )


def build_shiprocket_order_payload(
    order: Order,
    pickup_location: str,
    dimensions: dict[str, float],
) -> dict[str, Any]:
    """Build a Shiprocket adhoc-order request from the immutable local order."""
    if not order.items:
        raise ValueError('The order has no items to ship.')
    if not pickup_location.strip():
        raise ValueError('A Shiprocket pickup location is required.')
    required_address = (
        order.customer_name,
        order.customer_email,
        order.customer_phone,
        order.address_line1,
        order.city,
        order.state,
        order.pincode,
        order.country,
    )
    if not all(required_address):
        raise ValueError('The order does not contain a complete delivery address.')

    customer_parts = str(order.customer_name).strip().split(maxsplit=1)
    first_name = customer_parts[0]
    last_name = customer_parts[1] if len(customer_parts) > 1 else ''
    item_subtotal = sum(
        Decimal(str(item.price_at_time)) * item.quantity for item in order.items
    )
    shipping_charges = Decimal(str(order.shipping_charges or 0))
    charged_subtotal = max(Decimal('0'), Decimal(str(order.total_amount)) - shipping_charges)
    total_discount = max(Decimal('0'), item_subtotal - charged_subtotal)
    prefix = str(current_app.config.get('SHIPROCKET_ORDER_PREFIX') or 'ZUHRAAN-')

    order_items = [
        {
            'name': item.variant.product.name,
            'sku': f'VARIANT-{item.variant_id}',
            'units': item.quantity,
            'selling_price': float(Decimal(str(item.price_at_time))),
            'discount': 0,
            'tax': 0,
            'hsn': '',
        }
        for item in order.items
    ]

    return {
        'order_id': f'{prefix}{order.id}',
        'order_date': order.created_at.strftime('%Y-%m-%d %H:%M'),
        'pickup_location': pickup_location.strip(),
        'comment': f'Website order #{order.id}',
        'billing_customer_name': first_name,
        'billing_last_name': last_name,
        'billing_address': order.address_line1,
        'billing_address_2': order.address_line2 or '',
        'billing_city': order.city,
        'billing_pincode': order.pincode,
        'billing_state': order.state,
        'billing_country': order.country,
        'billing_email': order.customer_email,
        'billing_phone': order.customer_phone,
        'shipping_is_billing': True,
        'order_items': order_items,
        'payment_method': 'COD' if order.payment_method in COD_PAYMENT_METHODS else 'Prepaid',
        'shipping_charges': float(shipping_charges),
        'giftwrap_charges': 0,
        'transaction_charges': 0,
        'total_discount': float(total_discount),
        'sub_total': float(charged_subtotal),
        **dimensions,
    }


def extract_awb_data(payload: dict[str, Any]) -> dict[str, str | None]:
    """Normalize fields returned by either create-order or assign-AWB APIs."""
    response_data = payload.get('response', {}).get('data', {})
    if not isinstance(response_data, dict):
        response_data = {}
    return {
        'awb_number': str(
            response_data.get('awb_code') or payload.get('awb_code') or ''
        ).strip() or None,
        'courier_name': str(
            response_data.get('courier_name') or payload.get('courier_name') or ''
        ).strip() or None,
    }


def extract_tracking_data(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the current status from Shiprocket's AWB tracking response."""
    tracking_data = payload.get('tracking_data')
    if not isinstance(tracking_data, dict):
        raise ShiprocketRequestError('Shiprocket returned no tracking data for this AWB.')
    shipment_track = tracking_data.get('shipment_track')
    current = shipment_track[0] if isinstance(shipment_track, list) and shipment_track else {}
    if not isinstance(current, dict):
        current = {}
    return {
        'status': str(current.get('current_status') or tracking_data.get('shipment_status') or '').strip(),
        'status_id': current.get('current_status_id'),
        'courier_name': str(current.get('courier_name') or '').strip() or None,
        'awb_number': str(current.get('awb_code') or '').strip() or None,
        'tracking_url': str(tracking_data.get('track_url') or '').strip() or None,
    }
