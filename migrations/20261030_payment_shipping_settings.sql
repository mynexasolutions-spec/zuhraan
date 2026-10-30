BEGIN;

-- Split the legacy shipping settings into independent prepaid and COD rules.
-- Existing values are copied once so current checkout pricing is preserved.
INSERT INTO public.setting (key, value)
VALUES (
    'shipping_charge_prepaid',
    COALESCE((SELECT value FROM public.setting WHERE key = 'shipping_charge'), '0')
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.setting (key, value)
VALUES (
    'free_shipping_threshold_prepaid',
    COALESCE((SELECT value FROM public.setting WHERE key = 'free_shipping_threshold'), '0')
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.setting (key, value)
VALUES (
    'shipping_charge_cod',
    COALESCE((SELECT value FROM public.setting WHERE key = 'shipping_charge'), '0')
)
ON CONFLICT (key) DO NOTHING;

INSERT INTO public.setting (key, value)
VALUES (
    'free_shipping_threshold_cod',
    COALESCE((SELECT value FROM public.setting WHERE key = 'free_shipping_threshold'), '0')
)
ON CONFLICT (key) DO NOTHING;

COMMIT;
