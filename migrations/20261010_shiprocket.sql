BEGIN;

ALTER TABLE public."order"
    ADD COLUMN IF NOT EXISTS shiprocket_order_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS shiprocket_shipment_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS shiprocket_status VARCHAR(100),
    ADD COLUMN IF NOT EXISTS shiprocket_status_id INTEGER,
    ADD COLUMN IF NOT EXISTS shiprocket_pickup_scheduled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS shiprocket_tracking_url VARCHAR(500),
    ADD COLUMN IF NOT EXISTS shiprocket_tracking_updated_at TIMESTAMP WITHOUT TIME ZONE;

CREATE UNIQUE INDEX IF NOT EXISTS ux_order_shiprocket_order_id
    ON public."order" (shiprocket_order_id)
    WHERE shiprocket_order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS ux_order_shiprocket_shipment_id
    ON public."order" (shiprocket_shipment_id)
    WHERE shiprocket_shipment_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_order_awb_number
    ON public."order" (awb_number)
    WHERE awb_number IS NOT NULL;

COMMIT;
