from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import json

db = SQLAlchemy()

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    address = db.Column(db.String(300), default='')
    phone = db.Column(db.String(20), default='')
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    builds = db.relationship('Build', backref='user', lazy=True)
    tickets = db.relationship('Ticket', backref='user', lazy=True)


class Component(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    type = db.Column(db.String(50), nullable=False)   # cpu, gpu, ram, ssd, hdd, psu, cabinet, air_cooler, liquid_cooler, fan
    name = db.Column(db.String(120), nullable=False)
    brand = db.Column(db.String(80))
    specs = db.Column(db.Text)                         # JSON string
    price = db.Column(db.Float, nullable=False)
    wattage = db.Column(db.Integer, default=0)         # TDP in watts
    stock = db.Column(db.Boolean, default=True)
    form_factor = db.Column(db.String(50), default='')
    is_rgb = db.Column(db.Boolean, default=False)
    image = db.Column(db.String(200), default='')      # filename in /static/images
    performance_score = db.Column(db.Integer, default=50)  # 1-100, used for "better component" logic & sorting
    ram_slots = db.Column(db.Integer, default=0)        # only relevant for motherboards (2 or 4)

    def get_specs(self):
        try:
            return json.loads(self.specs)
        except:
            return {}

    def to_dict(self):
        return {
            'id': self.id,
            'type': self.type,
            'name': self.name,
            'brand': self.brand,
            'specs': self.get_specs(),
            'price': self.price,
            'wattage': self.wattage,
            'stock': self.stock,
            'form_factor': self.form_factor,
            'is_rgb': self.is_rgb,
            'image': self.image,
            'performance_score': self.performance_score,
            'ram_slots': self.ram_slots,
        }


class Build(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    components = db.Column(db.Text, default='[]')       # JSON list of core PC component ids
    peripherals = db.Column(db.Text, default='[]')       # JSON list of peripheral component ids (monitor, keyboard, etc.)
    ram_quantity = db.Column(db.Integer, default=1)      # how many RAM sticks of the chosen RAM module
    fan_quantity = db.Column(db.Integer, default=0)      # how many extra fans (item 3)
    total_price = db.Column(db.Float, default=0.0)
    extras_price = db.Column(db.Float, default=0.0)      # price added from description/add-ons
    description = db.Column(db.Text, default='')         # custom build notes e.g. RGB wiring, vertical GPU mount
    status = db.Column(db.String(50), default='draft')   # draft, saved, cart, ordered, cancelled
    payment_method = db.Column(db.String(30), default='') # razorpay
    razorpay_order_id = db.Column(db.String(64), default='')   # set when a Razorpay order is created
    razorpay_payment_id = db.Column(db.String(64), default='') # set once payment is verified
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    ordered_at = db.Column(db.DateTime, default=None)     # set when order is confirmed
    delivery_date = db.Column(db.DateTime, default=None)  # set when order is confirmed (created_at + 7 days)
    cancelled_at = db.Column(db.DateTime, default=None)   # set when order is cancelled

    def get_components(self):
        try:
            return json.loads(self.components)
        except:
            return []

    def get_peripherals(self):
        try:
            return json.loads(self.peripherals)
        except:
            return []

    def can_cancel(self):
        """Item 15: orders can only be cancelled within 3 days of being placed."""
        if self.status != 'ordered':
            return False
        if not self.ordered_at:
            return False
        return (datetime.utcnow() - self.ordered_at).days < 3


class Ticket(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    message = db.Column(db.Text, nullable=False)
    attachment = db.Column(db.String(300), default='')   # filename if uploaded
    status = db.Column(db.String(30), default='open')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PasswordReset(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), nullable=False)
    token = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    used = db.Column(db.Boolean, default=False)


class GuestVisit(db.Model):
    """Tracks page visits from anyone browsing the site, logged in or not.
    Used by the admin panel to show overall site activity (item 1 & 5)."""
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), nullable=False)   # anonymous browser-session identifier
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)  # set if logged in at time of visit
    path = db.Column(db.String(300), nullable=False)
    ip_address = db.Column(db.String(64), default='')
    visited_at = db.Column(db.DateTime, default=datetime.utcnow)
