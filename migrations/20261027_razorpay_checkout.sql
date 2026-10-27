BEGIN;

ALTER TABLE public."order"
    ADD COLUMN IF NOT EXISTS razorpay_order_id VARCHAR(100),
    ADD COLUMN IF NOT EXISTS razorpay_payment_id VARCHAR(100);

CREATE UNIQUE INDEX IF NOT EXISTS uq_order_razorpay_order_id
    ON public."order" (razorpay_order_id)
    WHERE razorpay_order_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_order_razorpay_payment_id
    ON public."order" (razorpay_payment_id)
    WHERE razorpay_payment_id IS NOT NULL;

COMMIT;
