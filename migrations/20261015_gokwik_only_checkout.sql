BEGIN;

ALTER TABLE public.go_kwik_checkout_session
    ADD COLUMN IF NOT EXISTS failure_reason VARCHAR(500);

UPDATE public."order"
SET gokwik_transaction_id = NULL
WHERE gokwik_transaction_id = '';

CREATE UNIQUE INDEX IF NOT EXISTS uq_order_gokwik_transaction_id
    ON public."order" (gokwik_transaction_id)
    WHERE gokwik_transaction_id IS NOT NULL;

UPDATE public."order"
SET payment_method = 'cod'
WHERE payment_method = 'native_cod';

UPDATE public."order"
SET payment_method = 'legacy_prepaid'
WHERE payment_method = 'razorpay';

ALTER TABLE public."order"
    DROP COLUMN IF EXISTS razorpay_order_id,
    DROP COLUMN IF EXISTS razorpay_payment_id;

DELETE FROM public.setting
WHERE key IN (
    'razorpay_key',
    'razorpay_secret',
    'payment_cod_enabled',
    'payment_online_enabled'
);

COMMIT;
