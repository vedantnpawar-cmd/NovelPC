from flask import Flask, render_template, redirect, url_for, request, session, flash, jsonify, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
import json, os, uuid, secrets
from datetime import datetime, timedelta
import razorpay
from dotenv import load_dotenv

from models import db, User, Component, Build, Ticket, PasswordReset, GuestVisit
from compatibility import run_all_checks
from seed_data import seed_components
from recommender import recommend_build
from invoice import generate_invoice_pdf

load_dotenv()  # loads variables from a local .env file, if present (see .env.example)

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'novelpc-secret-2024')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///db.sqlite3'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['UPLOAD_FOLDER'] = os.path.join(app.root_path, 'static', 'uploads')
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5MB

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

# ─── Razorpay ────────────────────────────────────────────────────────────
# Keys are read from environment variables only — never hardcode them here.
# See .env.example for where to put them locally.
RAZORPAY_KEY_ID = os.environ.get('RAZORPAY_KEY_ID', '')
RAZORPAY_KEY_SECRET = os.environ.get('RAZORPAY_KEY_SECRET', '')
razorpay_client = (
    razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET else None
)
if razorpay_client is None:
    print('⚠️  Razorpay keys not set — payments will be disabled. See .env.example.')

with app.app_context():
    db.create_all()

    from sqlalchemy import text

    columns = db.session.execute(
        text("PRAGMA table_info(build)")
    ).fetchall()

    column_names = [column[1] for column in columns]

    if 'razorpay_order_id' not in column_names:
        db.session.execute(
            text("ALTER TABLE build ADD COLUMN razorpay_order_id VARCHAR(255)")
        )

    if 'razorpay_payment_id' not in column_names:
        db.session.execute(
            text("ALTER TABLE build ADD COLUMN razorpay_payment_id VARCHAR(255)")
        )

    db.session.commit()

    seed_components()
# ─── Decorators ────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'warning')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login'))
        return f(*args, **kwargs)
    return decorated

# ─── Guest/visitor tracking (item 1 & 5) ─────────────────────────────────────
# Tracks every page view, whether the visitor is logged in or browsing as a guest,
# so the admin panel can show overall site activity, not just registered-user data.
SKIP_TRACKING_PREFIXES = ('/static/', '/admin', '/api/', '/favicon')

@app.before_request
def track_visit():
    path = request.path
    if path.startswith(SKIP_TRACKING_PREFIXES):
        return
    if 'guest_session_id' not in session:
        session['guest_session_id'] = secrets.token_hex(8)
    try:
        visit = GuestVisit(
            session_id=session['guest_session_id'],
            user_id=session.get('user_id'),
            path=path,
            ip_address=request.remote_addr or '',
        )
        db.session.add(visit)
        db.session.commit()
    except Exception:
        db.session.rollback()  # never let tracking break the actual request

# ─── Component type groups (cooling split into air/liquid; fan is optional) ──
ALL_TYPES   = ['cpu', 'gpu', 'motherboard', 'ram', 'ssd', 'hdd', 'psu', 'cabinet', 'air_cooler', 'liquid_cooler', 'fan']
PAGE_TYPES  = ['cpu', 'gpu', 'motherboard', 'ram', 'ssd', 'hdd', 'psu', 'cabinet', 'air_cooler', 'liquid_cooler', 'fan']
PERIPHERAL_TYPES = ['monitor', 'keyboard', 'mouse', 'headphones', 'earphones', 'gaming_chair', 'mousepad']
ALL_PAGE_TYPES = PAGE_TYPES + PERIPHERAL_TYPES
REQUIRED_FOR_BUILD = ['cpu', 'gpu', 'motherboard', 'ram', 'psu', 'cabinet']  # storage + cooling checked separately

COMPONENT_INFO = {
    'cpu': {
        'title': 'CPU — Central Processing Unit',
        'definition': 'The CPU is the brain of a computer. It executes instructions from programs and coordinates all other components.',
        'how_it_works': 'The CPU fetches instructions from memory, decodes them, executes them, and writes results back. Modern CPUs have multiple cores allowing parallel execution.',
        'types': ['Desktop CPUs (high performance)', 'Mobile CPUs (power efficient)', 'Server CPUs (many cores)', 'Embedded CPUs (low power)'],
    },
    'gpu': {
        'title': 'GPU — Graphics Processing Unit',
        'definition': 'The GPU is a specialized processor designed to rapidly manipulate and alter memory to accelerate image creation in a frame buffer.',
        'how_it_works': 'GPUs contain thousands of smaller cores optimized for parallel processing, making them ideal for rendering graphics and AI workloads.',
        'types': ['Discrete GPUs (separate card)', 'Integrated GPUs (built into CPU)', 'Workstation GPUs (professional)', 'Mobile GPUs (laptop)'],
    },
    'motherboard': {
        'title': 'Motherboard',
        'definition': 'The motherboard is the main circuit board that connects all PC components. It determines what CPU, RAM, and expansion cards can be used.',
        'how_it_works': 'The motherboard provides electrical connections and data pathways (buses) between the CPU, RAM, storage, and peripherals via chipsets.',
        'types': ['ATX (standard, full-size)', 'Micro-ATX (compact)', 'Mini-ITX (very small)', 'E-ATX (extended, workstation)'],
    },
    'ram': {
        'title': 'RAM — Random Access Memory',
        'definition': 'RAM is fast, volatile memory used to store data that the CPU is actively using. More RAM allows more programs to run simultaneously.',
        'how_it_works': 'RAM stores data in capacitors that can be read/written extremely quickly. Data is lost when power is removed, unlike storage drives.',
        'types': ['DDR4 (current standard)', 'DDR5 (latest, faster)', 'ECC RAM (error-correcting, server)', 'SO-DIMM (laptop form factor)'],
    },
    'ssd': {
        'title': 'SSD — Solid State Drive',
        'definition': 'SSDs are storage devices that use flash memory chips instead of spinning disks, offering much faster speeds than HDDs.',
        'how_it_works': 'SSDs store data in NAND flash memory cells. NVMe SSDs connect directly to the CPU via PCIe lanes for maximum speed.',
        'types': ['SATA SSD (older interface)', 'NVMe M.2 (fast, compact)', 'PCIe SSD (enterprise)', 'U.2 (server)'],
    },
    'hdd': {
        'title': 'HDD — Hard Disk Drive',
        'definition': 'HDDs store data on magnetic spinning platters. They offer large capacities at low cost, ideal for bulk storage.',
        'how_it_works': 'Read/write heads move over spinning platters to read magnetic data. RPM (rotations per minute) determines speed.',
        'types': ['7200 RPM (performance)', '5400 RPM (quiet, efficient)', 'NAS drives (24/7 operation)', 'External HDDs (portable)'],
    },
    'psu': {
        'title': 'PSU — Power Supply Unit',
        'definition': 'The PSU converts AC power from the wall into DC power that PC components use. Wattage and efficiency rating are key specs.',
        'how_it_works': 'PSUs use transformers and voltage regulators to provide stable 3.3V, 5V, and 12V power rails to components.',
        'types': ['ATX (standard)', 'SFX (small form factor)', 'Modular (detachable cables)', 'Non-modular (fixed cables)'],
    },
    'cabinet': {
        'title': 'Cabinet (PC Case)',
        'definition': 'The cabinet houses all PC components, providing physical protection, airflow management, and aesthetic presentation.',
        'how_it_works': 'Cases route airflow from intake fans through the system, past hot components, and out exhaust fans to maintain safe temperatures.',
        'types': ['Full Tower (largest, most expansion)', 'Mid Tower (most popular)', 'Mini Tower / mATX', 'Mini-ITX (ultra compact)'],
    },
    'air_cooler': {
        'title': 'Air Cooling',
        'definition': 'Air coolers use a heatsink and fan(s) to pull heat away from the CPU and dissipate it into the surrounding air.',
        'how_it_works': 'Heat pipes draw warmth from the CPU into a finned heatsink; fans then blow air across the fins to carry heat away.',
        'types': ['Tower coolers (large heatsink)', 'Low-profile coolers (small cases)', 'Stock coolers (bundled)'],
    },
    'liquid_cooler': {
        'title': 'Liquid Cooling (AIO)',
        'definition': 'All-in-one (AIO) liquid coolers pump coolant through a block on the CPU to a radiator, where fans dissipate the heat.',
        'how_it_works': 'A pump circulates liquid coolant between a CPU water block and a radiator. Fans on the radiator expel the absorbed heat.',
        'types': ['240mm AIO (compact)', '280mm AIO (balanced)', '360mm AIO (max cooling)', 'Custom loops (enthusiast)'],
    },
    'fan': {
        'title': 'Fans',
        'definition': 'Fans improve airflow through the cabinet, helping intake cool air and exhaust hot air for better overall thermals.',
        'how_it_works': 'Fans are placed as intake (front/bottom) or exhaust (rear/top) to create consistent airflow direction through the case.',
        'types': ['120mm fans (standard)', '140mm fans (higher airflow, quieter)', 'RGB fans (aesthetic)', 'Static-pressure fans (for radiators)'],
    },
    'monitor': {
        'title': 'Monitor',
        'definition': 'The monitor is your window into the PC — its resolution, refresh rate, and panel type directly shape your gaming and work experience.',
        'how_it_works': 'A monitor receives a video signal from the GPU and refreshes its pixels at a fixed rate (Hz) to display motion smoothly.',
        'types': ['IPS (accurate colors)', 'VA (high contrast)', 'TN (fastest response)', 'OLED (best contrast, premium)'],
    },
    'keyboard': {
        'title': 'Keyboard',
        'definition': 'Keyboards are the primary text and command input device. Mechanical keyboards use individual switches per key for tactile feedback.',
        'how_it_works': 'Each keypress actuates a switch (mechanical) or membrane contact, sending a signal to the PC identifying which key was pressed.',
        'types': ['Mechanical (tactile/clicky/linear)', 'Membrane (quiet, affordable)', 'Optical (fast actuation)', 'Wireless/Bluetooth'],
    },
    'mouse': {
        'title': 'Mouse',
        'definition': 'The mouse is the primary pointing device, critical for gaming precision, especially in competitive titles.',
        'how_it_works': 'An optical or laser sensor tracks movement across a surface and translates it into cursor movement at a given DPI sensitivity.',
        'types': ['Wired (lowest latency)', 'Wireless (cable-free)', 'Lightweight gaming mice', 'Ergonomic/office mice'],
    },
    'headphones': {
        'title': 'Headphones / Gaming Headset',
        'definition': 'Over-ear headphones deliver immersive audio and often include a microphone for in-game communication.',
        'how_it_works': 'Drivers convert electrical audio signals into sound waves; surround sound models simulate spatial positioning for footsteps and effects.',
        'types': ['Wired headsets', 'Wireless 2.4GHz headsets', 'Bluetooth headphones', 'Open-back (audiophile) headphones'],
    },
    'earphones': {
        'title': 'Earphones',
        'definition': 'Compact in-ear audio devices, popular for portability, calls, and casual listening alongside your PC setup.',
        'how_it_works': 'Small drivers sit close to the ear canal, delivering sound with minimal bulk compared to over-ear headphones.',
        'types': ['Wired in-ear', 'True Wireless (TWS)', 'Neckband wireless', 'Noise-cancelling earphones'],
    },
    'gaming_chair': {
        'title': 'Gaming Chair',
        'definition': 'A gaming chair supports long sitting sessions with ergonomic adjustments, reducing strain during extended gameplay or work.',
        'how_it_works': 'Adjustable recline, armrests, and lumbar support let you customize seating posture to reduce back and neck fatigue.',
        'types': ['Racing-style chairs', 'Ergonomic mesh chairs', 'Premium adjustable chairs', 'Floor/rocker chairs'],
    },
    'mousepad': {
        'title': 'Mouse Pad',
        'definition': 'A mouse pad provides a consistent surface for accurate mouse tracking and can protect your desk.',
        'how_it_works': 'The pad surface texture affects glide and friction, influencing precision and control for the mouse sensor.',
        'types': ['Cloth pads (control)', 'Hard plastic pads (speed)', 'RGB pads (aesthetic)', 'Extended desk mats (XXL)'],
    },
}

# ─── Helper ────────────────────────────────────────────────────────────────
def save_uploaded_file(file_storage):
    if not file_storage or file_storage.filename == '':
        return ''
    ext = os.path.splitext(file_storage.filename)[1]
    filename = secure_filename(f"{uuid.uuid4().hex}{ext}")
    file_storage.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
    return filename

def get_all_components_dict():
    data = {}
    for ct in ALL_TYPES:
        data[ct] = [c.to_dict() for c in Component.query.filter_by(type=ct).all()]
    return data

# ─── Core Pages ────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/components')
def components():
    return render_template('components.html', types=PAGE_TYPES, peripheral_types=PERIPHERAL_TYPES)

@app.route('/components/<comp_type>')
def component_detail(comp_type):
    info = COMPONENT_INFO.get(comp_type)
    if not info:
        flash('Unknown component type.', 'danger')
        return redirect(url_for('components'))
    products = Component.query.filter_by(type=comp_type).all()
    rgb_products = [p for p in products if p.is_rgb]
    non_rgb_products = [p for p in products if not p.is_rgb]
    return render_template('component_detail.html', info=info, comp_type=comp_type,
                            rgb_products=rgb_products, non_rgb_products=non_rgb_products)

@app.route('/components/<comp_type>/<int:comp_id>')
def component_single(comp_type, comp_id):
    product = Component.query.get_or_404(comp_id)
    info = COMPONENT_INFO.get(comp_type, {})
    return render_template('component_single.html', product=product, info=info, comp_type=comp_type)

@app.route('/components/<comp_type>/<int:comp_id>/buy')
@login_required
def buy_single_component(comp_type, comp_id):
    product = Component.query.get_or_404(comp_id)
    if not product.stock:
        flash('This item is out of stock.', 'danger')
        return redirect(url_for('component_single', comp_type=comp_type, comp_id=comp_id))
    build = Build(user_id=session['user_id'], components=json.dumps([product.id]),
                  total_price=product.price, status='cart')
    db.session.add(build)
    db.session.commit()
    flash(f'{product.name} added to cart!', 'success')
    return redirect(url_for('payment', build_id=build.id))

# ─── Auth ──────────────────────────────────────────────────────────────────
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        address  = request.form.get('address', '').strip()
        phone    = request.form.get('phone', '').strip()

        if not username or not email or not password or not address or not phone:
            flash('All fields are required. Please fill in username, email, password, phone and address.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(username=username).first():
            flash('Username already taken.', 'danger')
            return render_template('register.html')
        if User.query.filter_by(email=email).first():
            flash('An account with this email already exists. Please log in instead.', 'danger')
            return render_template('register.html')

        user = User(username=username, email=email,
                    password_hash=generate_password_hash(password),
                    address=address, phone=phone)
        db.session.add(user)
        db.session.commit()
        flash('Registration successful! Please log in.', 'success')
        return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            session['user_id']  = user.id
            session['username'] = user.username
            flash(f'Welcome back, {user.username}!', 'success')
            return redirect(url_for('builder'))
        flash('Invalid username or password.', 'danger')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully.', 'info')
    return redirect(url_for('index'))

@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        user = User.query.filter_by(email=email).first()
        # Always show the same message (don't leak which emails exist)
        if user:
            token = secrets.token_urlsafe(24)
            reset = PasswordReset(email=email, token=token)
            db.session.add(reset)
            db.session.commit()
            # In production this would be emailed. For the demo we log it.
            print(f"[DEMO] Password reset link: /reset-password/{token}")
        flash('If an account with that email exists, a reset password link has been sent to it.', 'info')
        return redirect(url_for('login'))
    return render_template('forgot_password.html')

@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    reset = PasswordReset.query.filter_by(token=token, used=False).first()
    if not reset:
        flash('This reset link is invalid or has already been used.', 'danger')
        return redirect(url_for('login'))
    if request.method == 'POST':
        new_password = request.form.get('password', '')
        if not new_password:
            flash('Please enter a new password.', 'danger')
            return render_template('reset_password.html', token=token)
        user = User.query.filter_by(email=reset.email).first()
        if user:
            user.password_hash = generate_password_hash(new_password)
            reset.used = True
            db.session.commit()
            flash('Password reset successful! Please log in.', 'success')
            return redirect(url_for('login'))
    return render_template('reset_password.html', token=token)

# ─── Profile ───────────────────────────────────────────────────────────────
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    user = User.query.get(session['user_id'])
    if request.method == 'POST':
        user.address = request.form.get('address', '').strip()
        user.phone = request.form.get('phone', '').strip()
        db.session.commit()
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))
    return render_template('profile.html', user=user)

# ─── Builder ───────────────────────────────────────────────────────────────
@app.route('/builder')
@login_required
def builder():
    all_components = get_all_components_dict()
    return render_template('builder.html', all_components=all_components)

@app.route('/api/compatibility', methods=['POST'])
@login_required
def check_compatibility():
    data = request.get_json()
    component_ids = data.get('components', {})
    ram_quantity = int(data.get('ram_quantity', 1) or 1)
    fan_quantity = int(data.get('fan_quantity', 0) or 0)
    build_dict = {}
    for ctype, cid in component_ids.items():
        if cid:
            comp = Component.query.get(int(cid))
            build_dict[ctype] = comp.to_dict() if comp else None
        else:
            build_dict[ctype] = None
    results = run_all_checks(build_dict, ram_quantity)
    return jsonify({'checks': results})

@app.route('/api/save_build', methods=['POST'])
@login_required
def save_build():
    data = request.get_json()
    component_ids = data.get('components', {})
    total_price   = data.get('total_price', 0)
    ram_quantity  = int(data.get('ram_quantity', 1) or 1)
    fan_quantity  = int(data.get('fan_quantity', 0) or 0)
    build_id      = data.get('build_id')  # if updating an existing autosaved build
    comp_list = [v for v in component_ids.values() if v]

    if build_id:
        build = Build.query.get(int(build_id))
        if build and build.user_id == session['user_id']:
            build.components = json.dumps(comp_list)
            build.total_price = total_price
            build.ram_quantity = ram_quantity
            build.fan_quantity = fan_quantity
            db.session.commit()
            return jsonify({'success': True, 'build_id': build.id})

    build = Build(
        user_id=session['user_id'],
        components=json.dumps(comp_list),
        total_price=total_price,
        ram_quantity=ram_quantity,
        fan_quantity=fan_quantity,
        status='saved'
    )
    db.session.add(build)
    db.session.commit()
    return jsonify({'success': True, 'build_id': build.id})

@app.route('/api/recommend_build', methods=['POST'])
@login_required
def api_recommend_build():
    data = request.get_json()
    use_case = data.get('use_case', 'gaming')
    budget = float(data.get('budget', 0) or 0)
    if budget <= 0:
        return jsonify({'error': 'Please enter a valid budget.'}), 400
    all_components = get_all_components_dict()
    result = recommend_build(all_components, use_case, budget)
    return jsonify(result)
@app.route('/api/search_components')
def api_search_components():
    q         = request.args.get('q', '').strip().lower()
    comp_type = request.args.get('type', '').strip().lower()

    if not q or not comp_type:
        return jsonify({'results': [], 'cross_type_hint': None})

    # Exact name match first, then starts-with, then contains — so "RTX 3080" 
    # shows the exact card before looser matches
    exact   = Component.query.filter(Component.type == comp_type,
                  Component.name.ilike(q)).all()
    starts  = Component.query.filter(Component.type == comp_type,
                  Component.name.ilike(f'{q}%'),
                  ~Component.name.ilike(q)).all()
    contains = Component.query.filter(Component.type == comp_type,
                  db.or_(Component.name.ilike(f'%{q}%'), Component.brand.ilike(f'%{q}%')),
                  ~Component.name.ilike(f'{q}%')).all()
    within = exact + starts + contains

    cross = Component.query.filter(
        Component.type != comp_type,
        db.or_(Component.name.ilike(f'%{q}%'), Component.brand.ilike(f'%{q}%'))
    ).first()

    cross_hint = None
    if cross and not within:
        type_labels = {
            'cpu':'CPU','gpu':'GPU','motherboard':'Motherboard','ram':'RAM',
            'ssd':'SSD','hdd':'HDD','psu':'PSU','cabinet':'Cabinet',
            'air_cooler':'Air Cooler','liquid_cooler':'Liquid Cooler','fan':'Fan',
            'monitor':'Monitor','keyboard':'Keyboard','mouse':'Mouse',
            'headphones':'Headphones','earphones':'Earphones',
            'gaming_chair':'Gaming Chair','mousepad':'Mouse Pad',
        }
        found_label   = type_labels.get(cross.type, cross.type.replace('_',' ').title())
        current_label = type_labels.get(comp_type, comp_type.replace('_',' ').title())
        cross_hint = (f'"{q}" was found in {found_label}s, not {current_label}s. '
                      f'Please browse the {found_label} section to find it.')

    return jsonify({'results': [c.to_dict() for c in within], 'cross_type_hint': cross_hint})
# ─── Accessories Step (item 33) ──────────────────────────────────────────────
@app.route('/builder/accessories')
@login_required
def builder_accessories():
    """Shown after the core PC build is complete. Lets the user add
    peripherals (monitor, keyboard, mouse, etc.) with a running total
    that includes the core build cost, then proceed to checkout."""
    core_total = float(request.args.get('core_total', 0) or 0)
    core_build_id = request.args.get('build_id', '')
    peripherals = {}
    for ct in PERIPHERAL_TYPES:
        peripherals[ct] = [c.to_dict() for c in Component.query.filter_by(type=ct).all()]
    return render_template('accessories.html', peripherals=peripherals,
                            core_total=core_total, core_build_id=core_build_id)

@app.route('/api/save_accessories', methods=['POST'])
@login_required
def save_accessories():
    """Attaches selected peripheral component ids onto the existing build
    (or creates a new one) and returns the build id for checkout."""
    data = request.get_json()
    build_id = data.get('build_id')
    accessory_ids = data.get('accessories', {})  # {type: id}
    accessory_total = float(data.get('accessory_total', 0) or 0)
    acc_list = [v for v in accessory_ids.values() if v]

    if build_id:
        build = Build.query.get(int(build_id))
    else:
        build = None

    if build and build.user_id == session['user_id']:
        existing = build.get_components()
        build.components = json.dumps(existing + acc_list)
        build.total_price = (build.total_price or 0) + accessory_total
        db.session.commit()
        return jsonify({'success': True, 'build_id': build.id})

    # No existing build — create one with just accessories
    build = Build(user_id=session['user_id'], components=json.dumps(acc_list),
                  total_price=accessory_total, status='saved')
    db.session.add(build)
    db.session.commit()
    return jsonify({'success': True, 'build_id': build.id})

# ─── Saved Builds / Cart / Orders ────────────────────────────────────────────
@app.route('/my-builds')
@login_required
def my_builds():
    builds = Build.query.filter_by(user_id=session['user_id'], status='saved').order_by(Build.created_at.desc()).all()
    enriched = []
    for b in builds:
        comps = [Component.query.get(cid) for cid in b.get_components() if Component.query.get(cid)]
        enriched.append({'build': b, 'components': comps})
    return render_template('my_builds.html', items=enriched)

@app.route('/my-builds/<int:build_id>/delete', methods=['POST'])
@login_required
def delete_build(build_id):
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('my_builds'))
    db.session.delete(build)
    db.session.commit()
    flash('Build deleted.', 'info')
    return redirect(url_for('my_builds'))

@app.route('/my-builds/<int:build_id>/add-to-cart', methods=['POST'])
@login_required
def add_build_to_cart(build_id):
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('my_builds'))
    build.status = 'cart'
    db.session.commit()
    return redirect(url_for('payment', build_id=build.id))

@app.route('/my-orders')
@login_required
def my_orders():
    orders = Build.query.filter_by(user_id=session['user_id'], status='ordered').order_by(Build.created_at.desc()).all()
    cancelled = Build.query.filter_by(user_id=session['user_id'], status='cancelled').order_by(Build.created_at.desc()).all()

    def enrich(builds):
        enriched = []
        for b in builds:
            comps = [Component.query.get(cid) for cid in b.get_components() if Component.query.get(cid)]
            enriched.append({'build': b, 'components': comps})
        return enriched

    return render_template('my_orders.html', items=enrich(orders), cancelled_items=enrich(cancelled))

# ─── Payment ───────────────────────────────────────────────────────────────
@app.route('/payment/<int:build_id>', methods=['GET', 'POST'])
@login_required
def payment(build_id):
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('builder'))

    if request.method == 'POST':
        description = request.form.get('description', '').strip()
        extras_price = float(request.form.get('extras_price', 0) or 0)
        build.description = description
        build.extras_price = extras_price
        db.session.commit()

    comp_ids = build.get_components()
    components = [Component.query.get(cid) for cid in comp_ids if Component.query.get(cid)]
    grand_total = build.total_price + (build.extras_price or 0)
    return render_template('payment.html', build=build, components=components, grand_total=grand_total)

@app.route('/payment/<int:build_id>/create-order', methods=['POST'])
@login_required
def create_razorpay_order(build_id):
    """Creates a Razorpay Order for the build's current total. The frontend
    opens the Razorpay Checkout widget (UPI/GPay/cards/netbanking) using the
    returned order_id — no card details ever touch our server."""
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    if not razorpay_client:
        return jsonify({'success': False, 'message': 'Payments are not configured yet. Please contact support.'}), 503

    grand_total = build.total_price + (build.extras_price or 0)
    amount_paise = int(round(grand_total * 100))  # Razorpay amounts are in the smallest currency unit (paise)

    order = razorpay_client.order.create({
        'amount': amount_paise,
        'currency': 'INR',
        'receipt': f'build_{build.id}',
        'notes': {'build_id': str(build.id), 'user_id': str(build.user_id)}
    })

    build.razorpay_order_id = order['id']
    db.session.commit()

    return jsonify({
        'success': True,
        'order_id': order['id'],
        'amount': amount_paise,
        'currency': 'INR',
        'key_id': RAZORPAY_KEY_ID
    })

@app.route('/payment/<int:build_id>/confirm', methods=['POST'])
@login_required
def confirm_payment(build_id):
    """Verifies the Razorpay payment signature server-side before marking the
    order as placed. This is the step that actually proves the payment is
    genuine — never trust a 'success' claim from the browser alone."""
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    if not razorpay_client:
        return jsonify({'success': False, 'message': 'Payments are not configured yet. Please contact support.'}), 503

    data = request.get_json() or {}
    razorpay_order_id = (data.get('razorpay_order_id') or '').strip()
    razorpay_payment_id = (data.get('razorpay_payment_id') or '').strip()
    razorpay_signature = (data.get('razorpay_signature') or '').strip()

    if not razorpay_order_id or not razorpay_payment_id or not razorpay_signature:
        return jsonify({'success': False, 'message': 'Missing payment details.'}), 400

    # Make sure this payment belongs to the order we created for this build.
    if razorpay_order_id != build.razorpay_order_id:
        return jsonify({'success': False, 'message': 'Order mismatch.'}), 400

    try:
        razorpay_client.utility.verify_payment_signature({
            'razorpay_order_id': razorpay_order_id,
            'razorpay_payment_id': razorpay_payment_id,
            'razorpay_signature': razorpay_signature
        })
    except razorpay.errors.SignatureVerificationError:
        return jsonify({'success': False, 'message': 'Payment verification failed.'}), 400

    build.payment_method = 'razorpay'
    build.razorpay_payment_id = razorpay_payment_id
    build.status = 'ordered'
    build.ordered_at = datetime.utcnow()
    delivery = datetime.utcnow() + timedelta(days=7)
    build.delivery_date = delivery
    db.session.commit()
    delivery_str = delivery.strftime('%d %B %Y')
    return jsonify({
        'success': True,
        'message': f'Order Placed Successfully — Your PC has been sent to assembly! 🎉 Your order will be delivered by {delivery_str}.',
        'delivery_date': delivery_str
    })

@app.route('/order/<int:build_id>/invoice')
@login_required
def download_invoice(build_id):
    """item 8: downloadable PDF invoice for a placed order."""
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        flash('Unauthorized.', 'danger')
        return redirect(url_for('my_orders'))
    if build.status not in ('ordered', 'cancelled'):
        flash('Invoice is only available after an order has been placed.', 'warning')
        return redirect(url_for('my_orders'))

    user = User.query.get(session['user_id'])
    comp_ids = build.get_components()
    components = [Component.query.get(cid) for cid in comp_ids if Component.query.get(cid)]

    pdf_buffer = generate_invoice_pdf(build, user, components)
    return send_file(
        pdf_buffer,
        mimetype='application/pdf',
        as_attachment=True,
        download_name=f'novelPC_Invoice_Order{build.id}.pdf'
    )

@app.route('/order/<int:build_id>/cancel', methods=['POST'])
@login_required
def cancel_order(build_id):
    """item 15: orders can only be cancelled within 3 days of being placed."""
    build = Build.query.get_or_404(build_id)
    if build.user_id != session['user_id']:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    if build.status != 'ordered':
        return jsonify({'success': False, 'message': 'This order cannot be cancelled.'}), 400

    if not build.can_cancel():
        return jsonify({'success': False, 'message': "You can't cancel this order now — the 3-day cancellation window has passed."}), 400

    build.status = 'cancelled'
    build.cancelled_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'success': True, 'message': 'Your order has been cancelled successfully.'})

# ─── Help / Support Tickets ──────────────────────────────────────────────────
@app.route('/help', methods=['GET', 'POST'])
@login_required
def help_page():
    if request.method == 'POST':
        message = request.form.get('message', '').strip()
        attachment_file = request.files.get('attachment')
        if not message:
            flash('Please enter a message before submitting.', 'danger')
            return redirect(url_for('help_page'))
        filename = save_uploaded_file(attachment_file)
        ticket = Ticket(user_id=session['user_id'], message=message, attachment=filename)
        db.session.add(ticket)
        db.session.commit()
        flash('Thank you for your cooperation. Your support ticket has been submitted successfully!', 'success')
        return redirect(url_for('help_page'))
    my_tickets = Ticket.query.filter_by(user_id=session['user_id']).order_by(Ticket.created_at.desc()).all()
    return render_template('help.html', tickets=my_tickets)

# ─── Admin ─────────────────────────────────────────────────────────────────
@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        pwd = request.form.get('password', '')
        if pwd == ADMIN_PASSWORD:
            session['is_admin'] = True
            return redirect(url_for('admin_dashboard'))
        flash('Incorrect admin password.', 'danger')
    return render_template('admin_login.html')

@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))

@app.route('/admin')
@admin_required
def admin_dashboard():
    users = User.query.order_by(User.created_at.desc()).all()
    orders = Build.query.filter_by(status='ordered').order_by(Build.created_at.desc()).all()
    cancelled_orders = Build.query.filter_by(status='cancelled').order_by(Build.created_at.desc()).all()
    saved_builds = Build.query.filter_by(status='saved').order_by(Build.created_at.desc()).all()
    tickets = Ticket.query.order_by(Ticket.created_at.desc()).all()
    total_revenue = sum((b.total_price or 0) + (b.extras_price or 0) for b in orders)

    # item 1 & 5: site-wide activity, including anonymous/guest browsing
    recent_visits = GuestVisit.query.order_by(GuestVisit.visited_at.desc()).limit(200).all()
    total_visits = GuestVisit.query.count()
    distinct_sessions = db.session.query(GuestVisit.session_id).distinct().count()
    guest_visits_count = GuestVisit.query.filter(GuestVisit.user_id.is_(None)).count()

    user_lookup = {u.id: u.username for u in users}
    for v in recent_visits:
        v.username = user_lookup.get(v.user_id)

    return render_template('admin_dashboard.html', users=users, orders=orders,
                            cancelled_orders=cancelled_orders, saved_builds=saved_builds,
                            tickets=tickets, total_revenue=total_revenue,
                            recent_visits=recent_visits, total_visits=total_visits,
                            distinct_sessions=distinct_sessions, guest_visits_count=guest_visits_count)
@app.route('/test-image')
def test_image():
    return '''
    <html>
    <head><title>Image Test</title></head>
    <body style="background: #333; color: white; padding: 20px;">
        <h1>Flask Image Test</h1>
        <p>If you see a GPU image below, Flask can serve images correctly!</p>
        <img src="/static/images/nvidia_gtx_1650.png" style="width: 400px; border: 3px solid yellow;">
        <p>If broken icon above = Flask cannot find the file</p>
    </body>
    </html>
    '''
if __name__ == '__main__':
    app.run(debug=True)
