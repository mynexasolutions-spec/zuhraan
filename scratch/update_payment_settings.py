from app import app
from models import db, Setting

with app.app_context():
    # Force Online Payment only
    cod = Setting.query.filter_by(key='payment_cod_enabled').first()
    if cod:
        cod.value = '0'
    else:
        db.session.add(Setting(key='payment_cod_enabled', value='0'))
        
    online = Setting.query.filter_by(key='payment_online_enabled').first()
    if online:
        online.value = '1'
    else:
        db.session.add(Setting(key='payment_online_enabled', value='1'))
        
    db.session.commit()
    print("Settings updated: COD=Disabled, Online=Enabled")
