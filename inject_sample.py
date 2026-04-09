from app import app
from models import db, Product, Category, ProductVariant

IMAGES = [
    'images/banner/zuhran_1.webp',
    'images/banner/zuhran_2.webp',
    'images/banner/zuhran_3.jpg',
]

# (name, short_desc, top, mid, base, longevity, projection,
#  cat_name, img_idx, p50, op50, s50, p100, op100, s100, tag, rank)
PRODUCTS = [
    ('Oud Noir Reserve', 'A dark, smoky masterpiece built for the bold.',
     'Saffron, Cardamom', 'Rose, Oud', 'Sandalwood, Musk, Amber',
     'Long Lasting', 'Strong', 'Extrait De Parfum', 0,
     129.00, 159.00, 20, 189.00, 229.00, 15, 'best_seller', 1),

    ('Velvet Rose', 'A soft floral enveloped in warm amber.',
     'Bergamot, Pink Pepper', 'Rose, Peony', 'Patchouli, Vanilla, Musk',
     'Long Lasting', 'Moderate', 'Eau De Parfum', 1,
     99.00, 119.00, 30, 149.00, 179.00, 25, 'best_seller', 2),

    ('Golden Elixir', 'A radiant citrus-woody accord exuding luxury.',
     'Neroli, Bergamot', 'Jasmine, Iris', 'Vetiver, Cedarwood, Amber',
     'Moderate', 'Moderate', 'Premium Collection', 2,
     149.00, 179.00, 18, 219.00, 259.00, 12, 'best_seller', 3),

    ('Midnight Ember', 'A smouldering oriental for the night wanderer.',
     'Black Pepper, Grapefruit', 'Leather, Oud', 'Tobacco, Benzoin, Vetiver',
     'Long Lasting', 'Strong', 'Extrait De Parfum', 0,
     139.00, 169.00, 22, 199.00, 239.00, 14, 'best_seller', 4),

    ('Aurora Mystique', 'A mystical floral with an ethereal trail.',
     'Lychee, Violet', 'Iris, Magnolia', 'White Musk, Cedarwood',
     'Moderate', 'Intimate', 'Eau De Parfum', 1,
     89.00, None, 35, 129.00, None, 28, 'featured', None),

    ('Crimson Whispers', 'A seductive fruity-floral with a warm heart.',
     'Raspberry, Mandarin', 'Rose, Ylang Ylang', 'Sandalwood, Musk',
     'Long Lasting', 'Moderate', 'Eau De Parfum', 2,
     95.00, None, 40, 139.00, None, 30, None, None),

    ('Amethyst Glow', 'A sparkling gourmand with a purple haze.',
     'Cassis, Violet', 'Heliotrope, Jasmine', 'Tonka Bean, Vanilla',
     'Moderate', 'Intimate', 'Premium Collection', 0,
     119.00, 139.00, 25, 175.00, 199.00, 18, 'new_arrival', None),

    ('Wild Orchid', 'An exotic tropical burst with deep floral roots.',
     'Mango, Orange', 'Orchid, Tuberose', 'Amber, Musk, Vanilla',
     'Long Lasting', 'Moderate', 'Eau De Parfum', 1,
     85.00, None, 50, 125.00, None, 38, None, None),

    ('Scandal', 'A provocative sweetness with an addictive finish.',
     'Blood Orange, Grapefruit', 'Gardenia, Jasmine', 'Caramel, Vetiver, Cedar',
     'Long Lasting', 'Strong', 'Extrait De Parfum', 2,
     155.00, 185.00, 15, 225.00, 265.00, 10, None, None),

    ('Saffron Rose', 'Rich saffron wrapped in velvety rose petals.',
     'Saffron, Cinnamon', 'Rose, Geranium', 'Oud, Amber, Musk',
     'Long Lasting', 'Strong', 'Extrait De Parfum', 0,
     169.00, 199.00, 12, 249.00, 289.00, 8, 'new_arrival', None),

    ('Citrus Soleil', 'A bright, energetic splash of Mediterranean sun.',
     'Lemon, Bergamot, Grapefruit', 'Neroli, Petitgrain', 'White Musk, Vetiver',
     'Moderate', 'Intimate', 'Eau De Parfum', 1,
     69.00, None, 60, 99.00, None, 45, None, None),

    ('Cedar Smoke', 'A minimalist woody smoke trail for connoisseurs.',
     'Bergamot, Elemi', 'Smoked Wood, Birch', 'Cedarwood, Vetiver, Guaiac',
     'Long Lasting', 'Moderate', 'Premium Collection', 2,
     109.00, None, 20, 159.00, None, 15, None, None),
]

CAT_NAMES = ['Eau De Parfum', 'Extrait De Parfum', 'Premium Collection', 'Room Freshener']


def inject_sample():
    with app.app_context():
        # Ensure all categories exist
        cat_map = {}
        for name in CAT_NAMES:
            cat = Category.query.filter_by(name=name).first()
            if not cat:
                cat = Category(name=name)
                db.session.add(cat)
                db.session.flush()
            cat_map[name] = cat

        db.session.flush()
        added = 0

        for row in PRODUCTS:
            (name, short_desc, top, mid, base, longevity, projection,
             cat_name, img_idx, p50, op50, s50, p100, op100, s100, tag, rank) = row

            if Product.query.filter_by(name=name).first():
                print(f'  skipping (exists): {name}')
                continue

            cat = cat_map[cat_name]
            product = Product(
                name=name,
                category_id=cat.id,
                short_description=short_desc,
                full_description=short_desc,
                top_notes=top,
                middle_notes=mid,
                base_notes=base,
                longevity=longevity,
                projection=projection,
                images=IMAGES[img_idx],
                tag=tag,
                best_seller_rank=rank,
            )
            db.session.add(product)
            db.session.flush()

            db.session.add(ProductVariant(
                product_id=product.id, size='50ml',
                price=p50, original_price=op50, stock_quantity=s50))
            db.session.add(ProductVariant(
                product_id=product.id, size='100ml',
                price=p100, original_price=op100, stock_quantity=s100))

            added += 1
            print(f'  added: {name}')

        db.session.commit()
        print(f'\nDone. {added} products inserted.')


if __name__ == '__main__':
    inject_sample()

