from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from ithink_client import (
    IThinkRequestError,
    build_ithink_order_payload,
    extract_created_shipment,
    extract_order_details,
)
from models import Order, utcnow_naive


def safe_tracking_url(value: Any) -> str | None:
    url = str(value or '').strip()
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return None
    return url[:500]


def apply_ithink_shipment(order: Order, shipment: dict[str, Any]) -> None:
    """Apply a normalized create or details record to a local order."""
    order_reference = str(shipment.get('order_reference') or '').strip()
    awb_number = str(shipment.get('awb_number') or '').strip()
    courier_name = str(shipment.get('courier_name') or '').strip()
    shipping_status = str(shipment.get('status') or '').strip()
    tracking_url = safe_tracking_url(shipment.get('tracking_url'))

    if order_reference:
        order.shipping_order_reference = order_reference[:150]
    if awb_number:
        order.awb_number = awb_number[:150]
    if courier_name:
        order.shipping_provider = courier_name[:100]
    if shipping_status:
        order.shipping_status = shipping_status[:100]
    if tracking_url:
        order.shipping_tracking_url = tracking_url
    order.shipping_tracking_updated_at = utcnow_naive()
    if awb_number and order.status in {'pending', 'processing'}:
        order.status = 'processing'


def _shipment_status_failed(value: Any) -> bool:
    status = str(value or '').strip().lower()
    return any(marker in status for marker in ('fail', 'error', 'reject', 'cancel'))


def _shipment_details_window(order: Order) -> tuple[str, str]:
    created_on = (order.created_at or utcnow_naive()).date()
    today = utcnow_naive().date()
    start = min(created_on, today) - timedelta(days=1)
    end = max(created_on, today) + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _merge_shipment_records(
    created: dict[str, Any] | None,
    recovered: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if created is None:
        return recovered
    if recovered is None:
        return created
    merged = dict(created)
    for key in ('order_reference', 'awb_number', 'courier_name', 'tracking_url'):
        if not merged.get(key) and recovered.get(key):
            merged[key] = recovered[key]
    merged['status'] = recovered.get('status') or merged.get('status')
    merged['remark'] = merged.get('remark') or recovered.get('remark')
    return merged


def create_or_recover_ithink_shipment(
    order: Order,
    dimensions: dict[str, float],
    client: Any,
    *,
    expected_order_reference: str | None = None,
) -> dict[str, Any]:
    """Create an iThink shipment and recover partial or duplicate responses."""
    expected_reference = str(
        expected_order_reference
        or order.shipping_order_reference
        or f'ZUHRAAN-{order.id}'
    ).strip()
    if order.awb_number:
        return {
            'order_reference': order.shipping_order_reference or expected_reference,
            'awb_number': order.awb_number,
            'courier_name': order.shipping_provider,
            'tracking_url': order.shipping_tracking_url,
            'status': order.shipping_status or 'Manifested',
            'remark': None,
        }

    created: dict[str, Any] | None = None
    create_error: IThinkRequestError | None = None
    retry_failed_shipment = _shipment_status_failed(order.shipping_status)
    if not order.shipping_order_reference or retry_failed_shipment:
        try:
            created_response = client.create_order(
                build_ithink_order_payload(order, dimensions)
            )
            created = extract_created_shipment(
                created_response,
                expected_reference,
            )
        except IThinkRequestError as error:
            create_error = error

    reference = (
        (created or {}).get('order_reference')
        or order.shipping_order_reference
        or expected_reference
    )
    recovered: dict[str, Any] | None = None
    if not created or not created.get('awb_number'):
        start_date, end_date = _shipment_details_window(order)
        try:
            details_response = client.get_order_details(
                order_reference=reference,
                start_date=start_date,
                end_date=end_date,
            )
            recovered = extract_order_details(
                details_response,
                order_reference=reference,
            )
        except (IThinkRequestError, ValueError, AttributeError):
            recovered = None

    shipment = _merge_shipment_records(created, recovered)
    if shipment and (shipment.get('order_reference') or shipment.get('awb_number')):
        if not shipment.get('order_reference'):
            shipment['order_reference'] = reference
        return shipment
    if create_error is not None:
        raise create_error
    if shipment:
        message = 'iThink returned no usable order reference or AWB number.'
        if shipment.get('remark'):
            message += f" iThink said: {shipment['remark']}"
        raise IThinkRequestError(message)
    raise IThinkRequestError('iThink returned no usable order reference or AWB number.')


def mapped_order_status(carrier_status: str) -> str | None:
    """Map iThink progress without regressing or cancelling local orders."""
    normalized = carrier_status.strip().upper()
    if not normalized:
        return None
    if 'RTO' in normalized:
        return 'shipped'
    if normalized == 'DELIVERED' or normalized.endswith(' DELIVERED'):
        return 'delivered'
    shipped_markers = (
        'PICKED UP',
        'IN TRANSIT',
        'REACHED AT DESTINATION',
        'OUT FOR DELIVERY',
        'DISPATCHED',
    )
    if any(marker in normalized for marker in shipped_markers):
        return 'shipped'
    processing_markers = (
        'MANIFEST',
        'NOT PICKED',
        'BOOKED',
        'NEW',
        'PROCESSING',
    )
    if any(marker in normalized for marker in processing_markers):
        return 'processing'
    return None


def apply_ithink_tracking(order: Order, payload: dict[str, Any]) -> bool:
    """Apply one normalized iThink tracking record and return status-change state."""
    raw_status = str(payload.get('status') or '').strip()
    if raw_status:
        order.shipping_status = raw_status[:100]
    raw_status_code = str(payload.get('status_code') or '').strip()
    if raw_status_code:
        order.shipping_status_code = raw_status_code[:30]

    courier_name = str(payload.get('courier_name') or '').strip()
    awb_number = str(payload.get('awb_number') or '').strip()
    tracking_url = safe_tracking_url(payload.get('tracking_url'))
    if courier_name:
        order.shipping_provider = courier_name[:100]
    if awb_number:
        order.awb_number = awb_number[:150]
    if tracking_url:
        order.shipping_tracking_url = tracking_url
    order.shipping_tracking_updated_at = utcnow_naive()

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
