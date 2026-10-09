import hmac
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from flask import Blueprint, current_app, jsonify, request

from gokwik_client import GoKwikRequestError, is_gokwik_payment_method, sync_gokwik_order
from models import Order, db, utcnow_naive


shiprocket_bp = Blueprint('shiprocket', __name__, url_prefix='/api/fulfillment')


def safe_tracking_url(value: Any) -> str | None:
    url = str(value or '').strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None
    return url[:500]


def mapped_order_status(shiprocket_status: str) -> str | None:
    """Map fulfillment progress without treating courier cancellation as order cancellation."""
    normalized = shiprocket_status.strip().upper()
    if normalized == 'DELIVERED':
        return 'delivered'
    shipped_markers = (
        'PICKED UP',
        'IN TRANSIT',
        'OUT FOR DELIVERY',
        'REACHED DESTINATION',
        'RTO',
    )
    if any(marker in normalized for marker in shipped_markers):
        return 'shipped'
    processing_markers = (
        'AWB',
        'MANIFEST',
        'PICKUP',
        'READY TO SHIP',
        'NEW',
        'CANCELED',
        'CANCELLED',
    )
    if any(marker in normalized for marker in processing_markers):
        return 'processing'
    return None


def apply_shiprocket_tracking(order: Order, payload: dict[str, Any]) -> bool:
    """Apply an authenticated tracking event and return whether order status changed."""
    raw_status = str(
        payload.get('current_status') or payload.get('shipment_status') or ''
    ).strip()
    if raw_status:
        order.shiprocket_status = raw_status[:100]

    raw_status_id = payload.get('current_status_id') or payload.get('shipment_status_id')
    try:
        order.shiprocket_status_id = int(raw_status_id) if raw_status_id is not None else None
    except (TypeError, ValueError):
        order.shiprocket_status_id = None

    courier_name = str(payload.get('courier_name') or '').strip()
    awb_number = str(payload.get('awb') or payload.get('awb_code') or '').strip()
    tracking_url = safe_tracking_url(payload.get('track_url'))
    if courier_name:
        order.shipping_provider = courier_name[:100]
    if awb_number:
        order.awb_number = awb_number[:150]
    if tracking_url:
        order.shiprocket_tracking_url = tracking_url
    order.shiprocket_tracking_updated_at = utcnow_naive()

    target_status = mapped_order_status(raw_status)
    old_status = order.status
    status_rank = {'pending': 0, 'processing': 1, 'shipped': 2, 'delivered': 3}
    if (
        target_status
        and old_status != 'cancelled'
        and status_rank.get(target_status, -1) >= status_rank.get(old_status, -1)
    ):
        order.status = target_status
    return order.status != old_status


@shiprocket_bp.post('/webhook')
def tracking_webhook():
    """Receive authenticated Shiprocket tracking events."""
    if not current_app.config.get('SHIPROCKET_ENABLED'):
        return jsonify({'error': 'Shiprocket integration is disabled.'}), 404

    expected_token = str(current_app.config.get('SHIPROCKET_WEBHOOK_TOKEN') or '')
    supplied_token = request.headers.get('x-api-key', '')
    if not expected_token or not hmac.compare_digest(supplied_token, expected_token):
        return jsonify({'error': 'Unauthorized.'}), 401
    if not request.is_json:
        return jsonify({'error': 'JSON body required.'}), 415

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({'error': 'Invalid JSON body.'}), 400

    awb_number = str(payload.get('awb') or '').strip()
    shiprocket_order_id = str(payload.get('sr_order_id') or '').strip()
    order: Order | None = None
    if awb_number:
        order = Order.query.filter_by(awb_number=awb_number).with_for_update().first()
    if order is None and shiprocket_order_id:
        order = (
            Order.query
            .filter_by(shiprocket_order_id=shiprocket_order_id)
            .with_for_update()
            .first()
        )
    if order is None:
        current_app.logger.warning(
            'Shiprocket webhook did not match a local order.',
            extra={'shiprocket_order_id': shiprocket_order_id, 'shiprocket_awb': awb_number},
        )
        # Acknowledge valid events so Shiprocket does not retry an unknown historical order.
        return jsonify({'ok': True}), 200

    status_changed = apply_shiprocket_tracking(order, payload)
    db.session.commit()

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
                'Shiprocket status was saved but GoKwik synchronization failed.',
                extra={'order_id': order.id},
            )

    return jsonify({'ok': True}), 200
