import csv, hmac, json, math, os, queue, re, sqlite3, threading, time
from datetime import date, datetime, timedelta
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from contextlib import closing
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from ui import (BG, SIDEBAR, SIDEBAR_TILE, SIDEBAR_ACTIVE, SIDEBAR_TEXT, SIDEBAR_BRIGHT, PANEL, CARD,
                BORDER, FG, MUTED, ACCENT, ACCENT_HOVER, ACCENT_SOFT, PURPLE_SURFACE, GREEN, GREEN_SOFT,
                INK, ALT, WARN, FONT,
                number, hours, duration, progress_percent, count_label, display_date, clip, surface, configure_style,
                progress_bar, draw_ring, scroll_area, weekly_chart, progress_palette, progress_badge, progress_tree_tags)
from analytics import PERIODS, activity_chart, bounds, date_range_label, previous_bounds, shift_period, summarize
from storage import acquire_instance, backup_database
from notifications import play_chime, post_toast, set_app_id
from focus_cycles import Cycle
from goals import ensure_schema as ensure_goals_schema
from goals_page import GoalsPage
from scope import asset_record, endpoint_location, in_scope, parse_request_url, parse_scope

BASE=Path(os.environ.get('LOCALAPPDATA',str(Path.home()))) / 'BountyTracker'
BASE.mkdir(parents=True,exist_ok=True)
DB=BASE/'tracker.db'
TOKEN_FILE=BASE/'burp_token.txt'
if not TOKEN_FILE.exists():
    import secrets
    TOKEN_FILE.write_text(secrets.token_urlsafe(32),encoding='utf-8')
TOKEN=TOKEN_FILE.read_text(encoding='utf-8').strip()

def connect(timeout=15):
    c=sqlite3.connect(DB,timeout=timeout)
    c.row_factory=sqlite3.Row
    c.execute('PRAGMA foreign_keys=ON')
    return c

def execute(sql,args=()):
    with closing(connect()) as c, c:
        cur=c.execute(sql,args)
        return cur.lastrowid

def rows(sql,args=()):
    with closing(connect()) as c, c:return [dict(x) for x in c.execute(sql,args).fetchall()]

def setup():
    with closing(connect()) as c, c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS programs(id INTEGER PRIMARY KEY,name TEXT NOT NULL,platform TEXT DEFAULT '',scope TEXT DEFAULT '',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS functions(id INTEGER PRIMARY KEY,program_id INTEGER NOT NULL,name TEXT NOT NULL,endpoint TEXT DEFAULT '',status TEXT DEFAULT 'Discovered',notes TEXT DEFAULT '',FOREIGN KEY(program_id) REFERENCES programs(id) ON DELETE CASCADE,UNIQUE(program_id,name,endpoint));
        CREATE TABLE IF NOT EXISTS tests(id INTEGER PRIMARY KEY,function_id INTEGER NOT NULL,category TEXT NOT NULL,result TEXT DEFAULT 'Not tested',notes TEXT DEFAULT '',FOREIGN KEY(function_id) REFERENCES functions(id) ON DELETE CASCADE,UNIQUE(function_id,category));
        CREATE TABLE IF NOT EXISTS sessions(id INTEGER PRIMARY KEY,program_id INTEGER NOT NULL,function_id INTEGER,started_at TEXT NOT NULL,ended_at TEXT NOT NULL,seconds INTEGER NOT NULL,notes TEXT DEFAULT '',FOREIGN KEY(program_id) REFERENCES programs(id) ON DELETE CASCADE,FOREIGN KEY(function_id) REFERENCES functions(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS assets(id INTEGER PRIMARY KEY,program_id INTEGER NOT NULL,host TEXT NOT NULL,source TEXT DEFAULT '',status TEXT DEFAULT '',FOREIGN KEY(program_id) REFERENCES programs(id) ON DELETE CASCADE,UNIQUE(program_id,host));
        CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY,program_id INTEGER NOT NULL,method TEXT NOT NULL,url TEXT NOT NULL,status INTEGER,source TEXT DEFAULT 'Burp',seen_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(program_id) REFERENCES programs(id) ON DELETE CASCADE,UNIQUE(program_id,method,url));
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,program_id INTEGER NOT NULL,title TEXT NOT NULL,severity TEXT DEFAULT 'Medium',status TEXT DEFAULT 'Draft',bounty REAL DEFAULT 0,notes TEXT DEFAULT '',FOREIGN KEY(program_id) REFERENCES programs(id) ON DELETE CASCADE);
        ''')
        program_columns = {column['name'] for column in c.execute('PRAGMA table_info(programs)')}
        if 'goal_hours' not in program_columns:
            c.execute('ALTER TABLE programs ADD COLUMN goal_hours REAL NOT NULL DEFAULT 50')
        if 'workspace' not in program_columns:
            c.execute("ALTER TABLE programs ADD COLUMN workspace TEXT NOT NULL DEFAULT 'bounty' CHECK(workspace IN ('bounty', 'general'))")
        if 'scope_rules' not in program_columns:
            c.execute("ALTER TABLE programs ADD COLUMN scope_rules TEXT NOT NULL DEFAULT ''")
        item_columns = {column['name'] for column in c.execute('PRAGMA table_info(functions)')}
        if 'work_done' not in item_columns:
            c.execute("ALTER TABLE functions ADD COLUMN work_done TEXT NOT NULL DEFAULT ''")
        if 'reviewed_at' not in item_columns:
            c.execute('ALTER TABLE functions ADD COLUMN reviewed_at TEXT')
        if 'progress_state' not in item_columns:
            c.execute("ALTER TABLE functions ADD COLUMN progress_state TEXT NOT NULL DEFAULT ''")
        if 'work_kind' not in item_columns:
            c.execute("ALTER TABLE functions ADD COLUMN work_kind TEXT NOT NULL DEFAULT 'endpoint'")
        c.execute('''
            CREATE TABLE IF NOT EXISTS path_work(
                program_id INTEGER NOT NULL REFERENCES programs(id) ON DELETE CASCADE,
                host TEXT NOT NULL, path TEXT NOT NULL,
                function_id INTEGER NOT NULL UNIQUE REFERENCES functions(id) ON DELETE CASCADE,
                PRIMARY KEY(program_id,host,path)
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS request_captures(
                id INTEGER PRIMARY KEY,
                request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
                status INTEGER, captured_at TEXT NOT NULL, source_key TEXT,
                function_id INTEGER UNIQUE REFERENCES functions(id) ON DELETE SET NULL,
                UNIQUE(request_id,source_key)
            )
        ''')
        c.execute("""
            CREATE TABLE IF NOT EXISTS analytics_goals(
                workspace TEXT NOT NULL CHECK(workspace IN ('bounty','general')),
                program_id INTEGER REFERENCES programs(id) ON DELETE CASCADE,
                period TEXT NOT NULL CHECK(period IN ('Daily','Weekly','Monthly')),
                hours REAL NOT NULL CHECK(hours>=0.01 AND hours<=100000)
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS analytics_workspace_goal
            ON analytics_goals(workspace,period) WHERE program_id IS NULL
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS analytics_program_goal
            ON analytics_goals(workspace,program_id,period) WHERE program_id IS NOT NULL
        """)
        ensure_goals_schema(c)
        timer_schema = """
            CREATE TABLE IF NOT EXISTS timer_draft(
                id INTEGER PRIMARY KEY CHECK(id=1),
                program_id INTEGER REFERENCES programs(id) ON DELETE CASCADE,
                function_id INTEGER REFERENCES functions(id) ON DELETE SET NULL,
                started_at TEXT NOT NULL, stopped_at TEXT NOT NULL,
                elapsed REAL NOT NULL CHECK(elapsed>=0),
                mode TEXT NOT NULL DEFAULT 'stopwatch', target_seconds INTEGER
            )
        """
        c.execute(timer_schema)
        draft_columns = {column['name'] for column in c.execute('PRAGMA table_info(timer_draft)')}
        if 'mode' not in draft_columns:
            c.execute('ALTER TABLE timer_draft RENAME TO old_timer_draft')
            c.execute(timer_schema)
            c.execute("""
                INSERT INTO timer_draft(id,program_id,function_id,started_at,stopped_at,elapsed)
                SELECT id,program_id,function_id,started_at,stopped_at,elapsed FROM old_timer_draft
            """)
            c.execute('DROP TABLE old_timer_draft')
        draft_columns = {column['name'] for column in c.execute('PRAGMA table_info(timer_draft)')}
        if 'cycle_plan' not in draft_columns:
            c.execute('ALTER TABLE timer_draft ADD COLUMN cycle_plan TEXT')
        if 'cycle_phase' not in draft_columns:
            c.execute('ALTER TABLE timer_draft ADD COLUMN cycle_phase INTEGER NOT NULL DEFAULT 0')
        if 'cycle_ready' not in draft_columns:
            c.execute('ALTER TABLE timer_draft ADD COLUMN cycle_ready INTEGER NOT NULL DEFAULT 0')
        c.execute("""
            CREATE TABLE IF NOT EXISTS focus_settings(
                id INTEGER PRIMARY KEY CHECK(id=1),
                focus_minutes INTEGER NOT NULL CHECK(focus_minutes BETWEEN 1 AND 180),
                short_minutes INTEGER NOT NULL CHECK(short_minutes BETWEEN 1 AND 60),
                long_minutes INTEGER NOT NULL CHECK(long_minutes BETWEEN 1 AND 60),
                cycle_size INTEGER NOT NULL CHECK(cycle_size BETWEEN 1 AND 12),
                rounds INTEGER NOT NULL DEFAULT 0 CHECK(rounds>=0)
            )
        """)
        focus_columns = {column['name'] for column in c.execute('PRAGMA table_info(focus_settings)')}
        if 'desktop_alerts' not in focus_columns:
            c.execute('ALTER TABLE focus_settings ADD COLUMN desktop_alerts INTEGER NOT NULL DEFAULT 1')
        if 'completion_sound' not in focus_columns:
            c.execute('ALTER TABLE focus_settings ADD COLUMN completion_sound INTEGER NOT NULL DEFAULT 1')
        c.execute('INSERT OR IGNORE INTO focus_settings(id,focus_minutes,short_minutes,long_minutes,cycle_size) VALUES(1,25,5,15,4)')
        if 'selected_cycle' not in focus_columns:
            c.execute('ALTER TABLE focus_settings ADD COLUMN selected_cycle INTEGER NOT NULL DEFAULT 1')
        c.execute("""
            CREATE TABLE IF NOT EXISTS focus_cycles(
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                focus_minutes INTEGER NOT NULL CHECK(focus_minutes BETWEEN 1 AND 180),
                short_minutes INTEGER NOT NULL CHECK(short_minutes BETWEEN 0 AND 60),
                long_minutes INTEGER NOT NULL CHECK(long_minutes BETWEEN 0 AND 60),
                cycle_size INTEGER NOT NULL CHECK(cycle_size BETWEEN 1 AND 12),
                auto_advance INTEGER NOT NULL CHECK(auto_advance IN (0,1))
            )
        """)
        c.execute("""
            INSERT OR IGNORE INTO focus_cycles(id,name,focus_minutes,short_minutes,long_minutes,cycle_size,auto_advance)
            SELECT 1,'My Pomodoro',focus_minutes,short_minutes,long_minutes,cycle_size,0 FROM focus_settings WHERE id=1
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS program_id_sequence(
                id INTEGER PRIMARY KEY CHECK(id=1), last_id INTEGER NOT NULL
            )
        """)
        c.execute("""
            INSERT OR IGNORE INTO program_id_sequence(id,last_id)
            SELECT 1,COALESCE(MAX(id),0) FROM programs
        """)
        c.execute("""
            CREATE TRIGGER IF NOT EXISTS reserve_program_id AFTER INSERT ON programs
            BEGIN
                UPDATE program_id_sequence SET last_id=MAX(last_id,NEW.id) WHERE id=1;
            END
        """)
        c.execute('CREATE INDEX IF NOT EXISTS sessions_by_program_date ON sessions(program_id,started_at)')
        c.execute('CREATE INDEX IF NOT EXISTS sessions_by_item ON sessions(function_id,program_id)')
        c.execute('CREATE INDEX IF NOT EXISTS functions_by_program_kind ON functions(program_id,work_kind)')
        c.execute('''
            INSERT OR IGNORE INTO request_captures(request_id,status,captured_at,source_key)
            SELECT r.id,r.status,r.seen_at,'legacy' FROM requests r
            WHERE NOT EXISTS (SELECT 1 FROM request_captures c WHERE c.request_id=r.id)
        ''')



def insert_program(values):
    with closing(connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        program_id, = conn.execute('SELECT last_id+1 FROM program_id_sequence WHERE id=1').fetchone()
        conn.execute("""
            INSERT INTO programs(id,name,platform,goal_hours,scope,scope_rules,workspace)
            VALUES(?,?,?,?,?,?,?)
        """, (program_id, *values))
        return program_id


def write_timer_draft(conn, values):
    conn.execute("""
        INSERT OR REPLACE INTO timer_draft(
            id,program_id,function_id,started_at,stopped_at,elapsed,mode,target_seconds,
            cycle_plan,cycle_phase,cycle_ready
        ) VALUES(1,?,?,?,?,?,?,?,?,?,?)
    """, values)


CATEGORIES=['IDOR / BOLA','Authentication','Authorization','Mass Assignment','Business Logic','Input Validation','XSS','SQL Injection','CSRF','SSRF','Race Condition','File Upload','Information Disclosure','Other']
STATUSES=['Discovered','Untested','In Progress','Partial','Tested','Needs Retest']

def save_burp_record(data):
    pid = int(data['program_id'])
    method = str(data['method']).upper()
    status = int(data.get('status') or 0)
    if not re.fullmatch(r"[A-Z][A-Z0-9!#$%&'*+.^_`|~-]{0,15}", method):
        raise ValueError('Invalid HTTP method.')
    if status and not 100 <= status <= 599:
        raise ValueError('Invalid HTTP status.')
    host, path, url = parse_request_url(data['url'])
    source_key = data.get('capture_time')
    if source_key is not None:
        if (not isinstance(source_key, str) or
                not re.fullmatch(r'[0-9T:.Z+\-]{12,40}', source_key) or source_key == 'legacy'):
            raise ValueError('Invalid capture time.')
        datetime.fromisoformat(source_key)
    with closing(connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        program = conn.execute("SELECT scope_rules FROM programs WHERE id=? AND workspace='bounty'", (pid,)).fetchone()
        if program is None:
            raise LookupError('Unknown Bug Bounty program.')
        if not in_scope(parse_scope(program['scope_rules']), host, path):
            raise PermissionError('URL is outside the program scope.')
        endpoint = f'{method} {url}'
        recorded = conn.execute("SELECT 1 FROM functions WHERE program_id=? AND endpoint=? AND work_kind='endpoint'",
                                (pid, endpoint)).fetchone()
        if not recorded:
            attach_legacy_endpoint(conn, pid, method, path, url)
        request_id, = conn.execute("""
            INSERT INTO requests(program_id,method,url,status) VALUES(?,?,?,?)
            ON CONFLICT(program_id,method,url) DO UPDATE SET status=excluded.status,seen_at=CURRENT_TIMESTAMP
            RETURNING id
        """, (pid, method, url, status)).fetchone()
        conn.execute('''
            INSERT OR IGNORE INTO request_captures(request_id,status,captured_at,source_key)
            VALUES(?,?,COALESCE(?,CURRENT_TIMESTAMP),?)
        ''', (request_id, status, source_key, source_key))
        conn.execute('INSERT OR IGNORE INTO assets(program_id,host,source) VALUES(?,?,?)', (pid, host, 'Burp'))
        if not recorded:
            conn.execute('INSERT OR IGNORE INTO functions(program_id,name,endpoint) VALUES(?,?,?)',
                         (pid, f'{method} {path}', endpoint))


def attach_legacy_endpoint(conn, program_id, method, path, url):
    legacy = f'{method} {path}'
    endpoint = f'{method} {url}'
    old = conn.execute('SELECT id FROM functions WHERE program_id=? AND name=? AND endpoint=?',
                       (program_id, legacy, legacy)).fetchone()
    if old is None:
        return
    matched = set()
    for record in conn.execute('SELECT url FROM requests WHERE program_id=? AND method=?', (program_id, method)):
        try:
            _, saved_path, saved_url = parse_request_url(record['url'])
        except ValueError:
            continue
        if saved_path == path:
            matched.add(saved_url)
    if matched == {url}:
        conn.execute('UPDATE OR IGNORE functions SET endpoint=? WHERE id=?', (endpoint, old['id']))


class ServerHandler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(10)
        super().setup()

    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path != '/api/burp':
            self.send_error(404)
            return
        token = self.headers.get('X-Bounty-Token', '').encode('utf-8')
        if not TOKEN or not hmac.compare_digest(token, TOKEN.encode('utf-8')):
            self.send_error(401)
            return
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 1 <= size <= 262144:
                raise ValueError('Invalid payload size.')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Expected a JSON object.')
            save_burp_record(data)
        except PermissionError:
            self.send_error(403, 'Outside program scope')
        except KeyError:
            self.send_error(400, 'Missing record fields')
        except LookupError:
            self.send_error(404, 'Unknown Bug Bounty program')
        except (ValueError, TypeError, OverflowError):
            self.send_error(400, 'Invalid record or scope rules')
        except TimeoutError:
            self.send_error(408, 'Incomplete request')
        except sqlite3.Error:
            self.send_error(503, 'Database unavailable')
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')


def start_import_listener(app):
    try:
        server = ThreadingHTTPServer(('127.0.0.1', 8765), ServerHandler)
    except OSError as error:
        app.import_listener_message = f'Unavailable · {error}'
        app.notify('Local Burp import is unavailable. See Settings for details.')
        return None
    app.import_listener_message = 'Listening on 127.0.0.1:8765'
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .2}, daemon=True)
    thread.start()
    return server


PAGES = (
    ('Directory', 'Programs', 'directory', '▤'),
    ('Goals', 'Goals', 'goals', '◎'),
    ('Analytics', 'Analytics', 'analytics', '▥'),
    ('Focus', 'Focus', 'focus', '◷'),
    ('Overview', 'Overview', 'overview', '▦'),
    ('Functions & Tests', 'Endpoints & Tests', 'functions', '⊞'),
    ('Sessions', 'Sessions', 'sessions', '◷'),
    ('Assets & Requests', 'Assets & Requests', 'assets', '◇'),
    ('Reports', 'Reports', 'reports', '▤'),
    ('Subdomains', 'Subdomains', 'subdomains', '◎'),
    ('Settings', 'Settings', 'settings', '⚙'),
)
GLOBAL_PAGES = {'Analytics', 'Focus', 'Goals', 'Settings'}
DETAIL_PAGES = {'Work item': 'work_item', 'Goal detail': 'goal_detail'}
TIMER_REFRESH_PAGES = {'Focus', 'Work item', 'Sessions', 'Overview', 'Analytics', 'Directory', 'Goals', 'Goal detail'}
BOUNTY_ONLY = {'Assets & Requests', 'Reports', 'Subdomains'}
TEST_RESULT = ('Not tested', 'In Progress', 'Passed', 'Finding', 'Needs Retest')
PROGRESS_STATES = ('Not Started', 'In Progress', 'Reviewed', 'Completed', 'Needs Revisit', 'On Hold')
SEVERITY = ('Info', 'Low', 'Medium', 'High', 'Critical')
REPORT_STATUS = ('Draft', 'Submitted', 'Triaged', 'Accepted', 'Resolved', 'Duplicate', 'Rejected')

SESSION_QUERY = '''
    SELECT s.*, f.name AS function_name
    FROM sessions s LEFT JOIN functions f ON f.id=s.function_id
    WHERE s.program_id=?
    ORDER BY s.started_at DESC, s.id DESC
'''


def endpoint_progress(record):
    if not record:
        return 'Not Started'
    return record['progress_state'] or ('Completed' if record['reviewed_at'] else 'Not Started')


def time_goal_hours(project):
    goal = project['goal_hours']
    return goal if goal and goal > 0 else 50


def parse_duration(value):
    if ':' in value:
        parts = value.split(':')
        if len(parts) not in (2, 3):
            raise ValueError
        h, m, s = [int(part) for part in parts] + [0] * (3 - len(parts))
        if h < 0 or not 0 <= m < 60 or not 0 <= s < 60:
            raise ValueError
        seconds = h * 3600 + m * 60 + s
    else:
        seconds = round(float(value.replace(',', '.')) * 3600)
    if not 0 < seconds <= 36000000:
        raise ValueError
    return seconds


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        setup()
        self.title('BountyTracker | Workspaces')
        if os.name == 'nt':
            self.iconbitmap(default=str(Path(__file__).with_name('bountytracker.ico')))
        self.geometry('1240x830')
        self.minsize(900, 620)
        self.configure(bg=BG)
        self.pid = self.fid = None
        self.workspace = 'bounty'
        self.session_filter = None
        self.endpoint_id = None
        self.endpoint_history = []
        self.path_tabs = {}
        self.work_tree = None
        self.topic_card_buttons = {}
        self.analytics_period = 'Weekly'
        self.analytics_date = date.today()
        self.analytics_scope = {}
        self.started = self.session_start = self.session_program = self.session_function = None
        self.elapsed = 0
        self.timer_stopped_at = None
        self.timer_checkpoint = 0
        self.timer_mode = 'stopwatch'
        self.timer_limit = None
        self.active_cycle = None
        self.cycle_phase = 0
        self.cycle_ready = False
        self.focus_selection = {}
        self.focus_clock = None
        self.focus_caption = None
        self.focus_progress = None
        self.focus_action_button = None
        self.focus_new_buttons = []
        self.alert_results = queue.SimpleQueue()
        self.timer_job = None
        self.import_listener_message = 'Not started'
        self.notice = tk.StringVar()
        self.notice_job = None
        self.goals = GoalsPage(self, connect, parse_duration)
        configure_style(self)
        self.make_shell()
        restored = self.restore_timer()
        self.show('Directory')
        self.protocol('WM_DELETE_WINDOW', self.close)
        if restored:
            message = ('Resume, save, or discard it.' if self.session_program is not None and self.timer_mode != 'break'
                       else 'Resume or discard it.')
            self.notify(f'Previous timer restored. {message}')
        self.tick()

    def make_shell(self):
        sidebar = tk.Frame(self, bg=SIDEBAR, width=214)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDEBAR)
        brand.pack(fill='x', padx=17, pady=(23, 28))
        icon = tk.Canvas(brand, width=34, height=34, bg=SIDEBAR, highlightthickness=0)
        icon.pack(side='left', padx=(0, 9))
        icon.create_polygon(17, 1, 32, 9, 32, 24, 17, 33, 2, 24, 2, 9, fill='#bda8ff')
        icon.create_oval(10, 10, 24, 24, outline=SIDEBAR, width=2)
        icon.create_polygon(17, 12, 22, 17, 17, 22, 12, 17, fill=SIDEBAR)
        self.label(brand, 'BountyTracker', 13, SIDEBAR_BRIGHT, bold=True).pack(side='left')
        self.label(sidebar, 'WORKSPACES', 8, SIDEBAR_TEXT, bold=True).pack(anchor='w', padx=17, pady=(0, 6))
        switch = tk.Frame(sidebar, bg=SIDEBAR_TILE, padx=3, pady=3)
        switch.pack(fill='x', padx=13, pady=(0, 18))
        self.workspace_buttons = {}
        for key, title in (('bounty', 'Bug Bounty'), ('general', 'General')):
            button = self.button(switch, title, lambda name=key: self.switch_workspace(name), kind='workspace')
            button.configure(padx=5, font=(FONT, 9, 'bold'))
            button.pack(side='left', fill='x', expand=True)
            self.workspace_buttons[key] = button
        self.nav = {}
        menu = tk.Frame(sidebar, bg=SIDEBAR)
        menu.pack(fill='x')
        for page, title, _, glyph in PAGES:
            self.nav[page] = self.button(menu, f'{glyph}   {title}', lambda name=page: self.show(name), kind='nav')
        bottom = tk.Frame(sidebar, bg=SIDEBAR)
        bottom.pack(side='bottom', fill='x', padx=17, pady=17)
        tk.Frame(bottom, bg=SIDEBAR_ACTIVE, height=1).pack(fill='x', pady=(0, 11))
        self.label(bottom, '●  Local data', 9, '#a6dec4').pack(side='left')
        self.label(bottom, 'v1.3.1', 8, SIDEBAR_TEXT).pack(side='right')

        content = tk.Frame(self, bg=BG)
        content.pack(side='left', fill='both', expand=True)
        header = tk.Frame(content, bg=PANEL)
        header.pack(fill='x')
        tk.Frame(content, height=1, bg=BORDER).pack(fill='x')
        header_inner = tk.Frame(header, bg=PANEL)
        header_inner.pack(fill='x', padx=23, pady=11)
        trail = tk.Frame(header_inner, bg=PANEL)
        trail.pack(side='left', fill='x')
        self.trail_root = self.button(trail, 'Bug Bounty', lambda: self.show('Directory'), kind='quiet')
        self.trail_root.pack(side='left')
        self.label(trail, '/', 11, MUTED).pack(side='left', padx=(1, 8))
        self.trail_current = self.label(trail, 'Programs', 12, bold=True)
        self.trail_current.pack(side='left')

        timer = tk.Frame(header_inner, bg=PURPLE_SURFACE, padx=9, pady=5)
        timer.pack(side='right', before=trail)
        clock = tk.Frame(timer, bg=PURPLE_SURFACE)
        clock.pack(side='left', padx=(0, 10))
        self.timer_context = self.label(clock, 'TIMER', 8, MUTED, bold=True, width=18)
        self.timer_context.pack(anchor='w')
        self.timer_label = self.label(clock, '00:00:00', 15, fg=FG, bold=True, font='Consolas')
        self.timer_label.pack(anchor='w')
        self.timer_toggle = self.button(timer, 'Start', self.toggle_timer, kind='primary', width=8)
        self.timer_toggle.pack(side='left', padx=(0, 6))
        self.timer_save = self.button(timer, 'Save', self.save_session, width=7)
        self.timer_save.pack(side='left')
        self.timer_discard = self.button(timer, 'Discard', self.discard_timer, width=7, kind='quiet')
        self.body = tk.Frame(content, bg=BG)
        self.body.pack(fill='both', expand=True, padx=24, pady=(17, 4))
        tk.Label(content, textvariable=self.notice, bg=BG, fg=ACCENT, anchor='w',
                 font=(FONT, 9)).pack(fill='x', padx=25, pady=(0, 6))

    def label(self, parent, text, size=11, fg=FG, *, bold=False, font=FONT, **kwargs):
        return tk.Label(parent, text=text, font=(font, size, 'bold' if bold else 'normal'),
                        bg=parent.cget('bg'), fg=fg, anchor='w', **kwargs)

    def button(self, parent, text, command, width=None, *, kind='secondary'):
        colors = {
            'primary': (ACCENT, INK, ACCENT_HOVER, INK),
            'secondary': (CARD, FG, ACCENT_SOFT, ACCENT),
            'danger': ('#fae9ef', WARN, '#f5d8e2', WARN),
            'nav': (SIDEBAR, SIDEBAR_TEXT, SIDEBAR_TILE, SIDEBAR_BRIGHT),
            'workspace': (SIDEBAR_TILE, SIDEBAR_TEXT, '#3c3056', SIDEBAR_BRIGHT),
            'quiet': (parent.cget('bg'), ACCENT, ACCENT_SOFT, ACCENT),
        }
        bg, fg, active_bg, active_fg = colors[kind]
        button = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                           activebackground=active_bg, activeforeground=active_fg,
                           disabledforeground='#756883', relief='flat', bd=0,
                           font=(FONT, 10, 'bold'), padx=10, pady=6, width=width,
                           anchor='w' if kind == 'nav' else 'center',
                           cursor='hand2', highlightthickness=1, highlightbackground=bg,
                           highlightcolor=ACCENT)
        button.bind('<Enter>', lambda _: button.configure(bg=active_bg) if button['state'] == 'normal' and button['bg'] == bg else None)
        button.bind('<Leave>', lambda _: button.configure(bg=bg) if button['bg'] == active_bg else None)
        return button

    def entry(self, parent, width=28, **kwargs):
        return tk.Entry(parent, width=width, bg=CARD, fg=FG, insertbackground=FG,
                        selectbackground=ACCENT_SOFT, relief='flat', font=(FONT, 11),
                        highlightbackground=BORDER, highlightcolor=ACCENT, highlightthickness=1, **kwargs)

    def notify(self, message):
        self.notice.set(message)
        if self.notice_job is not None:
            self.after_cancel(self.notice_job)
        self.notice_job = self.after(4500, self.clear_notice)

    def clear_notice(self):
        self.notice.set('')
        self.notice_job = None

    def heading(self, text, description='', action=None, *, deletion=None):
        row = tk.Frame(self.body, bg=BG)
        row.pack(fill='x', pady=(0, 15))
        if action:
            self.button(row, *action, kind='primary').pack(side='right', padx=(8, 0))
        if deletion:
            self.button(row, *deletion, kind='danger').pack(side='right', padx=(12, 0))
        copy = tk.Frame(row, bg=BG)
        copy.pack(side='left', fill='x', expand=True)
        title = self.label(copy, text, 26, bold=True, justify='left')
        title.pack(fill='x')
        title.bind('<Configure>', lambda event: title.configure(wraplength=max(40, event.width)), add=True)
        if description:
            subtitle = self.label(copy, description, 10, MUTED, justify='left')
            subtitle.pack(fill='x', pady=(3, 0))
            subtitle.bind('<Configure>', lambda event: subtitle.configure(wraplength=max(40, event.width)), add=True)

    @property
    def general(self):
        return self.workspace == 'general'

    @property
    def project_label(self):
        return 'subject' if self.general else 'program'

    @property
    def item_label(self):
        return 'topic' if self.general else 'endpoint'

    def update_navigation(self):
        for key, button in self.workspace_buttons.items():
            active = key == self.workspace
            button.configure(bg=SIDEBAR_ACTIVE if active else SIDEBAR_TILE,
                             fg=SIDEBAR_BRIGHT if active else SIDEBAR_TEXT,
                             highlightbackground=SIDEBAR_TILE)
        for page, title, _, glyph in PAGES:
            button = self.nav[page]
            button.pack_forget()
            if self.pid is None and page != 'Directory' and page not in GLOBAL_PAGES:
                continue
            if self.general and page in BOUNTY_ONLY:
                continue
            if page == 'Directory':
                title = self.project_label.title() + 's'
            elif page == 'Functions & Tests' and self.general:
                title = 'Topics'
            selected = (page == self.page or (page == 'Functions & Tests' and self.page == 'Work item')
                        or (page == 'Goals' and self.page == 'Goal detail'))
            button.configure(text=f'{glyph}   {title}', bg=SIDEBAR_ACTIVE if selected else SIDEBAR,
                             fg=SIDEBAR_BRIGHT if selected else SIDEBAR_TEXT,
                             highlightbackground=SIDEBAR)
            button.pack(fill='x', padx=11, pady=1)

    def switch_workspace(self, workspace):
        self.workspace = workspace
        self.show('Directory')

    def refresh_programs(self):
        programs = rows('SELECT * FROM programs ORDER BY name COLLATE NOCASE, id')
        self.all_programs = {program['id']: program for program in programs}
        self.program_by_id = {program['id']: program for program in programs if program['workspace'] == self.workspace}

    def activate_program(self, program_id):
        if program_id not in self.program_by_id:
            return
        if self.pid != program_id:
            self.fid = None
            self.session_filter = None
        self.pid = program_id
        self.focus_selection[self.workspace] = (program_id, None)
        self.show('Overview')

    def show(self, page):
        if self.general and page in BOUNTY_ONLY:
            page = 'Overview'
        if self.pid is None and page not in GLOBAL_PAGES and page != 'Goal detail':
            page = 'Directory'
        if page == 'Directory':
            self.pid = self.fid = self.session_filter = None
            self.refresh_programs()
        self.work_tree = None
        self.topic_card_buttons = {}
        self.focus_clock = self.focus_caption = self.focus_progress = self.focus_action_button = self.focus_next_label = None
        self.focus_new_buttons = []
        for widget in self.body.winfo_children():
            widget.destroy()
        if self.notice_job is not None:
            self.after_cancel(self.notice_job)
            self.clear_notice()
        self.page = page
        self.update_navigation()
        self.trail_root.configure(text='General' if self.general else 'Bug Bounty')
        project = self.program_by_id.get(self.pid)
        trail = clip(project['name'], 28) if project else self.project_label.title() + 's'
        self.trail_current.configure(text='Goals' if page == 'Goal detail' else page if page in GLOBAL_PAGES else trail)
        method = DETAIL_PAGES.get(page)
        if method is None:
            method = next(method for key, _, method, _ in PAGES if key == page)
        getattr(self, f'page_{method}')()
        self.update_timer()

    def page_goals(self):
        self.goals.render()

    def page_goal_detail(self):
        self.goals.detail()

    def empty(self, parent, title, detail, action=None):
        card = surface(parent)
        card.pack(fill='x', pady=(2, 8))
        self.label(card, title, 14, bold=True).pack(anchor='w', padx=19, pady=(16, 3))
        self.label(card, detail, 10, MUTED, wraplength=540, justify='left').pack(anchor='w', padx=19, pady=(0, 15))
        if action:
            self.button(card, *action, kind='primary').pack(anchor='w', padx=19, pady=(0, 16))

    def need_program(self):
        if self.pid is None:
            self.empty(self.body, f'Add a {self.project_label} first',
                       f'Records are linked to the selected {self.project_label}.',
                       (f'+ Add {self.project_label}', self.add_program))
            return False
        return True

    def tree(self, parent, columns, *, export=False, multiple=False, height=10):
        frame = surface(parent)
        frame.pack(fill='both', expand=True, pady=(6, 0))
        tree = ttk.Treeview(frame, columns=tuple(title for title, _ in columns),
                            show='headings', height=height, selectmode='extended' if multiple else 'browse')
        for title, width in columns:
            tree.heading(title, text=title, anchor='w')
            tree.column(title, width=width, minwidth=min(width, 55), anchor='w', stretch=width > 100)
        tree.tag_configure('alt', background=ALT)
        tree.tag_configure('done', foreground=GREEN)
        vertical = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        vertical.pack(side='right', fill='y')
        horizontal.pack(side='bottom', fill='x')
        tree.pack(side='left', fill='both', expand=True)
        if export:
            def copy(_):
                items = tree.selection()
                if items:
                    self.clipboard_clear()
                    records = [tree.item(item) for item in items]
                    self.clipboard_append('\n'.join('\t'.join(map(str, (row['text'], *row['values']))).lstrip('\t')
                                                    for row in records))
                    self.notify('Selected rows copied.')
                return 'break'
            tree.bind('<Control-c>', copy)
        return tree

    def fill_tree(self, tree, records, values):
        tree.delete(*tree.get_children())
        for i, record in enumerate(records):
            tags = ('alt',) if i % 2 else ()
            completed = record.get('status') == 'Completed'
            if 'progress_state' in record:
                completed = completed or endpoint_progress(record) == 'Completed'
            if completed:
                tags += ('done',)
            tree.insert('', 'end', iid=str(record['id']), values=values(record), tags=tags)

    def form(self, title, fields, submit):
        win = tk.Toplevel(self, bg=BG)
        win.title(title)
        win.transient(self)
        win.resizable(True, False)
        frame = tk.Frame(win, bg=BG, padx=20, pady=16)
        frame.pack(fill='both', expand=True)
        self.label(frame, title, 17, bold=True).pack(anchor='w', pady=(0, 6))
        getters = {}
        focus = None
        for key, label, value, mode in fields:
            self.label(frame, label, 10, MUTED, bold=True).pack(anchor='w', pady=(8, 4))
            if isinstance(mode, (dict, tuple, list)):
                choices = dict(mode) if isinstance(mode, dict) else {entry: entry for entry in mode}
                if value not in choices:
                    choices[value] = f'{value} (existing)'
                keys = tuple(choices)
                field = ttk.Combobox(frame, state='readonly', values=tuple(choices.values()),
                                     font=(FONT, 11))
                field.current(keys.index(value))
                getters[key] = lambda field=field, keys=keys: keys[field.current()]
            elif mode == 'text':
                field = tk.Text(frame, height=4, bg=CARD, fg=FG, insertbackground=FG,
                                font=(FONT, 11), wrap='word', padx=7, pady=6,
                                relief='flat', highlightthickness=1,
                                highlightbackground=BORDER, highlightcolor=ACCENT)
                field.insert('1.0', str(value or ''))
                getters[key] = lambda field=field: field.get('1.0', 'end-1c')
            else:
                field = self.entry(frame)
                field.insert(0, str(value if value is not None else ''))
                getters[key] = field.get
                field.bind('<Return>', lambda _: save())
            field.pack(fill='x', ipady=3 if mode is None else 0)
            if focus is None:
                focus = field
        error = self.label(frame, '', 9, WARN, wraplength=440, justify='left', height=2)
        error.pack(fill='x', pady=(9, 1))
        actions = tk.Frame(frame, bg=BG)
        actions.pack(fill='x', pady=(7, 0))
        def save():
            try:
                values = {key: read() for key, read in getters.items()}
                submit({key: value.strip() if isinstance(value, str) else value
                        for key, value in values.items()})
            except ValueError as exc:
                error.configure(text=str(exc))
                return
            except sqlite3.Error:
                error.configure(text='Could not save the record. Try again.')
                return
            win.destroy()
            self.show(self.page)
            self.notify('Changes saved.')
        self.button(actions, 'Save', save, kind='primary').pack(side='right')
        self.button(actions, 'Cancel', win.destroy, kind='quiet').pack(side='right', padx=8)
        win.bind('<Escape>', lambda _: win.destroy())
        win.update_idletasks()
        width, height = 490, win.winfo_reqheight()
        win.minsize(width, height)
        top = max(self.winfo_rooty() + 12, self.winfo_rooty() + (self.winfo_height() - height) // 2)
        win.geometry(f'{width}x{height}+{max(0, self.winfo_rootx() + (self.winfo_width() - width) // 2)}+{max(0, top)}')
        win.grab_set()
        focus.focus_set()
        return win

    def add_program(self):
        return self.program_form()

    def edit_goal(self):
        return self.program_form(self.program_by_id[self.pid])

    def program_form(self, program=None):
        values = program or {'name': '', 'platform': '', 'scope': '', 'scope_rules': '', 'goal_hours': 50}
        project_label = self.project_label
        workspace = self.workspace
        fields = [('name', f'{project_label.title()} name', values['name'], None)]
        if not self.general:
            fields.append(('platform', 'Platform', values['platform'], None))
        fields.extend((
            ('goal', 'Time goal (hours)', str(values['goal_hours'] or 50), None),
            ('scope', 'Subject notes' if self.general else 'Scope notes', values['scope'], 'text'),
        ))

        if not self.general:
            fields.append(('scope_rules', 'Import scope · one host, *.host, host/path or !host per line', values['scope_rules'], 'text'))

        def save(data):
            if not data['name']:
                raise ValueError(f'Enter a {project_label} name.')
            try:
                goal = float(data['goal'].replace(',', '.'))
            except ValueError:
                goal = 0
            if not .1 <= goal <= 100000:
                raise ValueError('The goal must be between 0.1 and 100,000 hours.')
            scope_rules = data.get('scope_rules', '')
            parse_scope(scope_rules)
            values = (data['name'], data.get('platform', ''), goal, data['scope'], scope_rules)
            if program:
                execute('UPDATE programs SET name=?,platform=?,goal_hours=?,scope=?,scope_rules=? WHERE id=?', (*values, program['id']))
            else:
                self.pid = insert_program((*values, workspace))
                self.fid = self.session_filter = None
                self.page = 'Overview'
            self.refresh_programs()

        return self.form(f'Edit {project_label}' if program else f'New {project_label}', fields, save)

    def overview_start(self):
        card = surface(self.body)
        card.pack(fill='x', pady=(1, 0))
        card.grid_columnconfigure(0, weight=3)
        card.grid_columnconfigure(1, weight=2)
        content = tk.Frame(card, bg=PANEL)
        content.grid(row=0, column=0, sticky='w', padx=(23, 12), pady=28)
        self.label(content, f'Add your first {self.project_label}', 20, bold=True).pack(anchor='w')
        self.label(content, 'Set a time goal. Saved sessions count toward your goal.',
                   11, MUTED, wraplength=300, justify='left').pack(anchor='w', pady=(7, 19))
        self.button(content, f'+ Add {self.project_label}', self.add_program, kind='primary').pack(anchor='w')
        graphic = tk.Canvas(card, width=210, height=225, bg=PANEL, highlightthickness=0)
        graphic.grid(row=0, column=1, sticky='ew', padx=(0, 12), pady=8)

        def draw(event):
            graphic.delete('all')
            cx, cy = event.width / 2, event.height / 2
            for dx in range(-95, 96, 19):
                for dy in range(-95, 96, 19):
                    graphic.create_oval(cx + dx - 1, cy + dy - 1, cx + dx + 1, cy + dy + 1,
                                        fill=BORDER, outline='')
            for radius, color in ((70, BORDER), (51, '#b7a8ef'), (31, '#8c73df')):
                graphic.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, outline=color, width=2)
            graphic.create_arc(cx - 70, cy - 70, cx + 70, cy + 70, start=34, extent=90,
                               style='arc', width=3, outline=ACCENT)
            graphic.create_polygon(cx, cy - 12, cx + 12, cy, cx, cy + 12, cx - 12, cy, fill=ACCENT)
        graphic.bind('<Configure>', draw)

    def page_directory(self):
        noun = self.project_label
        done_status = 'Completed' if self.general else 'Tested'
        records = rows("""
            SELECT p.id, p.name, p.platform, p.goal_hours,
                   COALESCE((SELECT SUM(seconds) FROM sessions WHERE program_id=p.id), 0) AS seconds,
                   (SELECT COUNT(*) FROM functions WHERE program_id=p.id AND work_kind='endpoint') AS items,
                   (SELECT COUNT(*) FROM functions WHERE program_id=p.id AND work_kind='endpoint' AND status=?) AS done
            FROM programs p WHERE p.workspace=?
            ORDER BY p.name COLLATE NOCASE, p.id
        """, (done_status, self.workspace))
        subtitle = 'Study time and topic progress by subject.' if self.general else 'Logged time and endpoint progress by program.'
        self.heading(noun.title() + 's', subtitle,
                     (f'+ New {noun}', self.add_program) if records else None)
        if not records:
            self.overview_start()
            return

        summary = tk.Frame(self.body, bg=BG)
        summary.pack(fill='x', pady=(0, 17))
        for index, (title, value, panel_color, text_color) in enumerate((
            ('Total logged', duration(sum(item['seconds'] for item in records)), PURPLE_SURFACE, ACCENT),
            (noun.title() + 's', str(len(records)), PANEL, FG),
            ('Goals reached', str(sum(item['seconds'] >= time_goal_hours(item) * 3600 for item in records)),
             GREEN_SOFT, GREEN),
        )):
            summary.grid_columnconfigure(index, weight=1, uniform='summary')
            tile = surface(summary, panel_color)
            tile.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 5, 0 if index == 2 else 5))
            icon = tk.Canvas(tile, width=33, height=33, bg=panel_color, highlightthickness=0)
            icon.pack(side='left', padx=(15, 10))
            icon.create_oval(1, 1, 32, 32, fill=PANEL if index != 1 else CARD, outline='')
            if index == 0:
                icon.create_oval(9, 9, 24, 24, outline=text_color, width=2)
                icon.create_line(16, 11, 16, 16, 21, 18, fill=text_color, width=2)
            elif index == 1:
                icon.create_rectangle(9, 12, 24, 23, outline=text_color, width=2)
                icon.create_line(12, 12, 12, 9, 18, 9, 20, 12, fill=text_color, width=2)
            else:
                icon.create_line(9, 16, 14, 21, 24, 11, fill=text_color, width=2)
            copy = tk.Frame(tile, bg=panel_color)
            copy.pack(side='left', pady=(12, 11))
            self.label(copy, title, 10, text_color, bold=True).pack(anchor='w')
            self.label(copy, value, 21, text_color, bold=True).pack(anchor='w')

        listing = scroll_area(self.body)
        for item in records:
            self.directory_row(listing, item, done_status)

    def directory_row(self, parent, item, done_status):
        row = surface(parent)
        row.pack(fill='x', pady=(0, 8))
        row.grid_columnconfigure(0, weight=4, uniform='entries')
        row.grid_columnconfigure(1, weight=2, uniform='entries')
        row.grid_columnconfigure(2, weight=3, uniform='entries')
        info = tk.Frame(row, bg=PANEL)
        info.grid(row=0, column=0, sticky='ew', padx=(15, 10), pady=12)
        name = self.label(info, item['name'], 13, bold=True)
        name.pack(fill='x')
        name.bind('<Configure>', lambda event: name.configure(wraplength=max(60, event.width)), add=True)
        stats = (f"{item['done']} of {item['items']} {self.item_label}s {done_status.lower()}"
                 if item['items'] else f"No {self.item_label}s yet")
        if item['platform']:
            stats = f"{clip(item['platform'], 17)}  ·  {stats}"
        self.label(info, stats, 9, MUTED).pack(anchor='w', pady=(2, 0))

        time = tk.Frame(row, bg=PANEL)
        time.grid(row=0, column=1, sticky='ew', padx=(4, 10), pady=12)
        self.label(time, duration(item['seconds']), 12, bold=True).pack(anchor='w')
        self.label(time, 'Logged time', 9, MUTED).pack(anchor='w')

        goal = time_goal_hours(item)
        ratio = item['seconds'] / (goal * 3600)
        progress = tk.Frame(row, bg=PANEL)
        progress.grid(row=0, column=2, sticky='ew', padx=(4, 8), pady=12)
        self.label(progress, f'{progress_percent(ratio)}% of {number(goal)} h goal', 10, GREEN if ratio >= 1 else ACCENT, bold=True).pack(anchor='w')
        progress_bar(progress, ratio).pack(fill='x', pady=(6, 0))

        def open_project(_=None):
            self.activate_program(item['id'])

        self.button(row, 'Open  →', open_project, kind='quiet').grid(row=0, column=3, padx=(5, 12))

        def make_clickable(widget):
            widget.configure(cursor='hand2')
            widget.bind('<Button-1>', open_project)
            widget.bind('<Enter>', lambda _: row.configure(highlightbackground=ACCENT), add=True)
            widget.bind('<Leave>', lambda _: row.configure(highlightbackground=BORDER), add=True)
            for child in widget.winfo_children():
                make_clickable(child)

        row.configure(cursor='hand2')
        row.bind('<Button-1>', open_project)
        for section in (info, time, progress):
            make_clickable(section)

    def confirm_delete(self, title, detail, statement, args):
        if not messagebox.askyesno(title, detail, default=messagebox.NO, parent=self):
            return False
        try:
            execute(statement, args)
        except sqlite3.Error as error:
            messagebox.showerror('Delete failed', str(error), parent=self)
            return False
        return True

    def delete_program(self):
        program = self.program_by_id.get(self.pid)
        if not program:
            return
        if self.session_program == self.pid:
            self.notify('Save the current timer session before deleting this record.')
            return
        name = program['name']
        noun = self.project_label
        related = ('its topics, notes, study sessions and linked goals' if self.general else
                   'its endpoints, path notes, requests, assets, reports, sessions and linked goals')
        detail = f'Delete “{name}” and {related}? This permanently deletes the records.'
        if self.confirm_delete(f'Delete {noun}', detail, 'DELETE FROM programs WHERE id=?', (self.pid,)):
            self.show('Directory')
            self.notify(f'{noun.title()} deleted.')

    def page_overview(self):
        program = self.program_by_id.get(self.pid)
        subtitle = 'Study time, topics, and recent sessions' if self.general else 'Logged time, endpoints, and recent sessions'
        self.heading(clip(program['name'], 26) if program else f'{self.project_label.title()} overview',
                     subtitle, (f'Edit {self.project_label}', self.edit_goal) if program else None,
                     deletion=(f'Delete {self.project_label}', self.delete_program) if program else None)
        if not self.need_program():
            return
        today = date.today()
        goal = time_goal_hours(program)
        worked, = rows('SELECT COALESCE(SUM(seconds),0) AS seconds FROM sessions WHERE program_id=?', (self.pid,))
        worked = worked['seconds']
        fraction = worked / (goal * 3600)
        daily = rows("""
            SELECT substr(started_at, 1, 10) AS day, SUM(seconds) AS seconds FROM sessions
            WHERE program_id=? AND started_at>=? AND started_at<?
            GROUP BY day
        """, (self.pid, (today - timedelta(days=6)).isoformat(), (today + timedelta(days=1)).isoformat()))
        recent = rows(SESSION_QUERY + ' LIMIT 4', (self.pid,))
        board = scroll_area(self.body)
        top = tk.Frame(board, bg=BG)
        top.pack(fill='x', pady=(1, 13))
        top.grid_columnconfigure(0, weight=6, uniform='top')
        top.grid_columnconfigure(1, weight=5, uniform='top')

        main = surface(top, PURPLE_SURFACE)
        main.grid(row=0, column=0, padx=(0, 7), sticky='nsew')
        mast = tk.Frame(main, bg=PURPLE_SURFACE)
        mast.pack(fill='x', padx=17, pady=(15, 0))
        self.label(mast, 'Time goal', 12, bold=True).pack(side='left')
        if program['platform']:
            self.label(mast, clip(program['platform'], 17), 9, ACCENT).pack(side='right')
        body = tk.Frame(main, bg=PURPLE_SURFACE)
        body.pack(fill='x', expand=True, padx=15, pady=(4, 7))
        draw_ring(body, fraction).pack(side='left', padx=(0, 10))
        text = tk.Frame(body, bg=PURPLE_SURFACE)
        text.pack(side='left', fill='x', expand=True)
        self.label(text, clip(program['name'], 23), 13, bold=True).pack(anchor='w')
        self.label(text, f'{hours(worked)} / {number(goal)} h', 18, ACCENT, bold=True).pack(anchor='w', pady=(4, 1))
        remaining = max(goal * 3600 - worked, 0)
        status = f'{duration(remaining)} left' if remaining else 'Goal reached'
        self.label(text, status, 9, GREEN if not remaining else MUTED).pack(anchor='w')
        chart = surface(top)
        chart.grid(row=0, column=1, padx=(7, 0), sticky='nsew')
        weekly_chart(self, chart, {entry['day']: entry['seconds'] for entry in daily}, today)

        if self.general:
            self.goals.linked_goals(board, self.pid)
        self.overview_items(board)
        if not self.general:
            links = tk.Frame(board, bg=BG)
            links.pack(fill='x', pady=(0, 10))
            counts, = rows("""
                SELECT (SELECT COUNT(*) FROM assets WHERE program_id=?) AS assets,
                       (SELECT COUNT(*) FROM requests WHERE program_id=?) AS requests,
                       (SELECT COUNT(*) FROM reports WHERE program_id=?) AS reports
            """, (self.pid, self.pid, self.pid))
            self.button(links, f"{count_label(counts['assets'], 'asset')}  ·  {count_label(counts['requests'], 'request')}  →",
                        lambda: self.show('Assets & Requests'), kind='quiet').pack(side='left')
            self.button(links, f"{count_label(counts['reports'], 'report')}  →",
                        lambda: self.show('Reports'), kind='quiet').pack(side='left', padx=9)
        activity = surface(board)
        activity.pack(fill='x', pady=(0, 8))
        activity_header = tk.Frame(activity, bg=PANEL)
        activity_header.pack(fill='x', padx=16, pady=(9, 3))
        self.label(activity_header, 'Recent sessions', 12, bold=True).pack(side='left')
        self.button(activity_header, 'View all  →', lambda: self.show('Sessions'), kind='quiet').pack(side='right')
        if not recent:
            self.label(activity, 'No sessions recorded yet.', 10, MUTED).pack(anchor='w', padx=16, pady=(3, 14))
            self.button(activity, '+ Log time', self.manual_session).pack(anchor='w', padx=16, pady=(0, 15))
        for session in recent:
            tk.Frame(activity, bg=BORDER, height=1).pack(fill='x', padx=16)
            row = tk.Frame(activity, bg=PANEL)
            row.pack(fill='x', padx=16, pady=8)
            title = ((session['notes'] or '').strip() or session['function_name'] or 'Unassigned session').splitlines()[0]
            self.label(row, duration(session['seconds']), 10, ACCENT, bold=True).pack(side='right')
            self.label(row, clip(title, 31), 10, bold=True).pack(anchor='w')
            self.label(row, display_date(session['started_at']), 8, MUTED).pack(anchor='w', pady=(1, 0))

    def tools(self, parent=None):
        parent = parent or self.body
        bar = tk.Frame(parent, bg=parent.cget('bg'))
        bar.pack(fill='x', pady=(0, 3))
        return bar

    def selected(self, tree, label):
        items = tree.selection()
        if items and items[0].isdigit():
            return int(items[0])
        self.notify(f'Select a {label} first.')
        return None

    def tracked_items(self):
        order = 'ASC' if self.general else 'DESC'
        return rows(f"""
            SELECT f.*, COALESCE(SUM(s.seconds), 0) AS seconds
            FROM functions f LEFT JOIN sessions s ON s.function_id=f.id AND s.program_id=f.program_id
            WHERE f.program_id=? AND f.work_kind='endpoint' GROUP BY f.id ORDER BY f.id {order}
        """, (self.pid,))

    def item_tree(self, parent, height=10):
        columns = (('Test status', 103), ('Progress', 115), ('Time', 88), ('Notes', 135))
        tree = self.tree(parent, columns, export=True, height=height)
        self.work_tree = tree
        tree.configure(show=('tree', 'headings'))
        tree.heading('#0', text='Host / path / method', anchor='w')
        tree.column('#0', width=300, minwidth=200)
        tree.tag_configure('group', foreground=ACCENT)
        progress_tree_tags(tree)
        tree.bind('<Double-1>', lambda event: self.open_tree_item(tree, tree.identify_row(event.y)))
        tree.bind('<Return>', lambda _: self.open_tree_item(tree, tree.focus()))
        return tree

    def open_tree_item(self, tree, key):
        if not key:
            return
        if key.isdigit():
            tree.selection_set(key)
            self.open_work_item(int(key))
            return 'break'
        elif key in tree.path_groups:
            self.open_path(*tree.path_groups[key])
            return 'break'

    def item_values(self, item):
        notes = ' '.join((item['notes'] or '').splitlines())
        logged = duration(item['seconds'])
        return item['status'], endpoint_progress(item), logged, notes

    def fill_item_tree(self, tree, records, *, expand=False, retained_paths=()):
        open_groups = set()
        pending = list(tree.get_children())
        while pending:
            key = pending.pop()
            if not key.isdigit():
                if tree.item(key, 'open'):
                    open_groups.add(tree.item(key, 'tags')[1])
                pending.extend(tree.get_children(key))
        tree.delete(*tree.get_children())
        folders = {}
        tree.path_groups = {}
        state = {(item['host'], item['path']): item for item in rows("""
            SELECT w.host,w.path,f.notes,f.reviewed_at,f.progress_state,
                   COALESCE((SELECT SUM(seconds) FROM sessions WHERE program_id=w.program_id AND function_id=f.id),0) AS seconds
            FROM path_work w JOIN functions f ON f.id=w.function_id WHERE w.program_id=?
        """, (self.pid,))}

        def group_values(group):
            item = state.get(group)
            if item is None:
                return ()
            return '', endpoint_progress(item), duration(item['seconds']), ' '.join((item['notes'] or '').splitlines())

        def group_tags(group, key):
            return ('group', key, 'progress:' + endpoint_progress(state[group])) if group in state else ('group', key)

        locations = self.endpoint_locations(records)
        # Keep retained captures and path work reachable without a catalog method row.
        locations.extend(((host, path, ''), None) for host, path in retained_paths)
        for (host, path, method), item in sorted(locations, key=lambda entry: entry[0]):
            if host not in folders:
                host_key = f'host:{host}'
                folders[host] = tree.insert('', 'end', text=host or 'Unassigned host',
                                            open=True, tags=group_tags((host, '/'), host_key), values=group_values((host, '/')))
                tree.path_groups[folders[host]] = (host, '/')
            parent = folders[host]
            if path:
                prefixes = []
                current = ''
                for segment in path.strip('/').split('/'):
                    if segment:
                        current += '/' + segment
                        prefixes.append(current)
                for prefix in prefixes:
                    group = (host, prefix)
                    if group not in folders:
                        path_key = f'path:{host}:{prefix}'
                        folders[group] = tree.insert(parent, 'end', text=prefix,
                                                     open=expand or path_key in open_groups,
                                                     tags=group_tags(group, path_key), values=group_values(group))
                        tree.path_groups[folders[group]] = group
                    parent = folders[group]
            if item is None:
                continue
            title = method or item['name']
            if method and (item['name'] != f'{method} {path}' or (path != '/' and path.endswith('/'))):
                title = f"{method} · {item['name']}"
            tree.insert(parent, 'end', iid=str(item['id']), text=title, values=self.item_values(item),
                        tags=('progress:' + endpoint_progress(item),))

    def retained_endpoint_paths(self):
        searchable = {}
        for request in rows('''
            SELECT r.method,r.url FROM requests r WHERE r.program_id=?
            AND EXISTS (SELECT 1 FROM request_captures c WHERE c.request_id=r.id)
        ''', (self.pid,)):
            try:
                host, path, url = parse_request_url(request['url'])
            except ValueError:
                continue
            searchable.setdefault((host, path), []).append(f"{request['method']} {url}")
        for item in rows('''
            SELECT w.host,w.path,f.notes,f.reviewed_at,f.progress_state
            FROM path_work w JOIN functions f ON f.id=w.function_id WHERE w.program_id=?
        ''', (self.pid,)):
            host, path = item['host'], item['path']
            searchable.setdefault((host, path), []).extend(
                (host, path, item['notes'] or '', endpoint_progress(item)))
        return {location: ' '.join(values).casefold() for location, values in searchable.items()}

    def endpoint_locations(self, records):
        locations = [(endpoint_location(item['endpoint'] or ''), item) for item in records]
        if any(not host and path and method for (host, path, method), _ in locations):
            known_hosts = {}
            for request in rows('SELECT method,url FROM requests WHERE program_id=?', (self.pid,)):
                try:
                    host, path, _ = parse_request_url(request['url'])
                except ValueError:
                    continue
                known_hosts.setdefault((path, request['method']), set()).add(host)
            resolved = []
            for (host, path, method), item in locations:
                candidates = known_hosts.get((path, method), ())
                imported_name = f'{method} {path}'
                if (not host and len(candidates) == 1 and item['name'] == imported_name
                        and item['endpoint'] == imported_name):
                    host = next(iter(candidates))
                resolved.append(((host, path, method), item))
            locations = resolved
        return locations

    def topic_card(self, parent, item):
        state = item['status'] or 'Not Started'
        _, _, edge, _ = progress_palette(state)
        border = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        tk.Frame(border, bg=edge, width=5).pack(side='left', fill='y')
        content = tk.Frame(border, bg=PANEL)
        content.pack(fill='both', expand=True, padx=(13, 14), pady=10)
        header = tk.Frame(content, bg=PANEL)
        header.pack(fill='x')
        badge = progress_badge(header, state)
        badge.pack(side='right', padx=(9, 0))

        def open_topic(_=None):
            self.open_work_item(item['id'])

        action = self.button(header, item['name'], open_topic, kind='quiet')
        action.configure(font=(FONT, 12, 'bold'), padx=3, pady=2, anchor='w', takefocus=True)
        action.pack(side='left', fill='x', expand=True)
        action.bind('<Configure>', lambda event: action.configure(wraplength=max(55, event.width - 12)), add=True)
        self.topic_card_buttons[item['id']] = action

        metadata = tk.Frame(content, bg=PANEL)
        metadata.pack(fill='x', pady=(4, 0))
        self.label(metadata, duration(item['seconds']), 11, FG, bold=True).pack(side='left', padx=(0, 6))
        self.label(metadata, 'study time', 9, MUTED).pack(side='left')
        self.button(metadata, 'Open  →', open_topic, kind='quiet').pack(side='right')
        note = ' '.join((item['notes'] or item['work_done'] or '').split())
        if note:
            preview = self.label(content, clip(note, 160), 10, MUTED, justify='left')
            preview.pack(fill='x', pady=(2, 0))
            preview.bind('<Configure>', lambda event: preview.configure(wraplength=max(50, event.width)), add=True)
        children = [border]
        while children:
            widget = children.pop()
            children.extend(widget.winfo_children())
            if not isinstance(widget, tk.Button):
                widget.configure(cursor='hand2')
                widget.bind('<Button-1>', open_topic)
        return border

    def page_topics(self):
        self.heading('Topics', 'Open a card to see notes and study sessions.', ('+ Add topic', self.add_function))
        if not self.need_program():
            return
        records = self.tracked_items()
        if not records:
            self.empty(self.body, 'No topics yet', 'Add the first topic for this subject.')
            return
        controls = self.tools()
        search = tk.StringVar()
        self.label(controls, 'Search', 9, MUTED).pack(side='left', padx=(0, 7))
        self.entry(controls, 23, textvariable=search).pack(side='left', ipady=4)
        self.label(controls, 'Progress', 9, MUTED).pack(side='left', padx=(12, 7))
        states = ttk.Combobox(controls, values=('All states', *PROGRESS_STATES), width=15, state='readonly')
        states.current(0)
        states.pack(side='left')
        counter = self.label(controls, '', 10, MUTED)
        counter.pack(side='right')
        listing = scroll_area(self.body)
        cards = [(item, self.topic_card(listing, item)) for item in records]
        empty = self.label(listing, 'No matching topics.', 10, MUTED)

        def render(*_):
            query = search.get().strip().casefold()
            count = 0
            empty.pack_forget()
            for item, card in cards:
                card.pack_forget()
                state = item['status'] or 'Not Started'
                searchable = ' '.join((item['name'], state, item['notes'] or '', item['work_done'] or '')).casefold()
                if (states.current() == 0 or states.get() == state) and query in searchable:
                    card.pack(fill='x', pady=(0, 10))
                    count += 1
            if not count:
                empty.pack(anchor='w', pady=8)
            counter.configure(text=count_label(count, 'topic'))

        states.bind('<<ComboboxSelected>>', render)
        search.trace_add('write', render)
        render()

    def overview_items(self, board):
        records = self.tracked_items()
        label = self.item_label
        finished = 'Completed' if self.general else 'Tested'
        done = sum(item['status'] == finished for item in records)
        if self.general:
            bar = self.tools(board)
            self.label(bar, 'Topics', 13, bold=True).pack(side='left')
            self.label(bar, f'{done} of {len(records)} completed', 10, MUTED).pack(side='left', padx=9)
            self.button(bar, '+ Add topic', self.add_function, kind='quiet').pack(side='right')
            if records:
                self.button(bar, 'All topics  →', lambda: self.show('Functions & Tests'), kind='quiet').pack(side='right')
                for item in records[:4]:
                    self.topic_card(board, item).pack(fill='x', pady=(0, 9))
            else:
                self.label(board, 'No topics yet.', 10, MUTED).pack(anchor='w', pady=(0, 8))
            return

        card = surface(board)
        card.pack(fill='x', pady=(0, 13))
        section = tk.Frame(card, bg=PANEL)
        section.pack(fill='x', padx=16, pady=(11, 8))
        bar = self.tools(section)
        self.label(bar, 'Endpoints', 12, bold=True).pack(side='left')
        if records:
            self.label(bar, f'{done} of {len(records)} tested', 9, MUTED).pack(side='left', padx=9)
        self.button(bar, '+ Add endpoint', self.add_function, kind='quiet').pack(side='right')
        self.button(bar, 'Refresh', lambda: self.show(self.page), kind='quiet').pack(side='right')
        retained_paths = self.retained_endpoint_paths()
        if records or retained_paths:
            self.button(bar, 'Manage  →', lambda: self.show('Functions & Tests'), kind='quiet').pack(side='right')
            table = self.item_tree(section, height=min(max(len(records), 3), 5))
            self.fill_item_tree(table, records, retained_paths=retained_paths)
            self.button(bar, 'Details', lambda: self.open_selected_item(table), kind='quiet').pack(side='right')
        else:
            self.label(section, 'No endpoints yet.', 10, MUTED).pack(anchor='w', pady=(0, 5))

    def page_functions(self):
        if self.general:
            return self.page_topics()
        subtitle = 'Double-click a host, path or method to open its work page. Use the arrows to expand the tree.'
        self.heading('Endpoints & Tests', subtitle, ('+ Add endpoint', self.add_function))
        if not self.need_program():
            return
        bar = self.tools()
        self.button(bar, 'Open details',
                    lambda: self.open_selected_item(self.function_tree), kind='primary').pack(side='left', padx=(0, 6))
        self.button(bar, 'Log time', self.log_item_time).pack(side='left', padx=(0, 6))
        self.button(bar, 'Use timer', self.track_function).pack(side='left', padx=(0, 6))
        self.button(bar, 'Checklist', self.test_checklist).pack(side='left', padx=(0, 6))
        self.button(bar, 'Refresh', lambda: self.show(self.page), kind='quiet').pack(side='left')
        search = tk.StringVar()
        self.entry(bar, 14, textvariable=search).pack(side='right', ipady=5)
        self.label(bar, 'Search', 10, MUTED).pack(side='right', padx=(4, 6))
        self.function_tree = self.item_tree(self.body)
        items = self.tracked_items()
        retained_paths = self.retained_endpoint_paths()

        def render(*_):
            query = search.get().strip().casefold()
            shown = [item for item in items if query in ' '.join(
                map(str, (item['name'], item['endpoint'], *self.item_values(item)))).casefold()]
            visible_paths = [key for key, searchable in retained_paths.items() if query in searchable]
            self.fill_item_tree(self.function_tree, shown, expand=bool(query), retained_paths=visible_paths)
            if not shown and not visible_paths:
                message = 'No matching endpoints.' if query else 'No endpoints recorded yet.'
                self.notify(message)

        search.trace_add('write', render)
        render()

    def open_selected_item(self, tree):
        selection = tree.selection()
        if selection and selection[0] in tree.path_groups:
            return self.open_path(*tree.path_groups[selection[0]])
        endpoint_id = self.selected(tree, self.item_label)
        if endpoint_id is not None:
            self.open_work_item(endpoint_id)

    def open_work_item(self, endpoint_id):
        if self.page != 'Work item':
            self.endpoint_history.clear()
        self.endpoint_history.append((self.page, self.endpoint_id))
        self.endpoint_id = endpoint_id
        self.show('Work item')

    def open_path(self, host, path):
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            found = db.execute('SELECT function_id FROM path_work WHERE program_id=? AND host=? AND path=?',
                               (self.pid, host, path)).fetchone()
            if found:
                fid = found['function_id']
            else:
                fid = db.execute("""
                    INSERT INTO functions(program_id,name,endpoint,work_kind)
                    VALUES(?,?,?,'path')
                """, (self.pid, path, f'Path · {host or "Unassigned host"}{path}')).lastrowid
                db.execute('INSERT INTO path_work(program_id,host,path,function_id) VALUES(?,?,?,?)',
                           (self.pid, host, path, fid))
        self.open_work_item(fid)

    def open_capture(self, capture_id):
        with closing(connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            capture = db.execute("""
                SELECT c.function_id,r.method,r.url FROM request_captures c JOIN requests r ON r.id=c.request_id
                WHERE c.id=? AND r.program_id=?
            """, (capture_id, self.pid)).fetchone()
            if capture is None:
                return
            fid = capture['function_id']
            if fid is None:
                _, _, url = parse_request_url(capture['url'])
                fid = db.execute("""
                    INSERT INTO functions(program_id,name,endpoint,work_kind)
                    VALUES(?,?,?,'capture')
                """, (self.pid, f'Request #{capture_id}', f"{capture['method']} {url}")).lastrowid
                db.execute('UPDATE request_captures SET function_id=? WHERE id=?', (fid, capture_id))
        self.open_work_item(fid)

    def back_to_endpoint_tree(self):
        target, parent = self.endpoint_history.pop() if self.endpoint_history else ('Functions & Tests', None)
        endpoint_id = str(self.endpoint_id)
        folder = None
        if not self.general and target in ('Overview', 'Functions & Tests'):
            path = rows('SELECT host,path FROM path_work WHERE function_id=? AND program_id=?',
                        (self.endpoint_id, self.pid))
            if path:
                folder = (path[0]['host'], path[0]['path'])
        self.endpoint_id = parent
        self.show(target)
        if self.general and endpoint_id.isdigit():
            button = self.topic_card_buttons.get(int(endpoint_id))
            if button is not None:
                self.after_idle(lambda: self.reveal_topic_card(button))
            return
        tree = self.work_tree
        if tree is not None:
            if folder is not None:
                endpoint_id = next((key for key, value in tree.path_groups.items() if value == folder), endpoint_id)
            if tree.exists(endpoint_id):
                tree.see(endpoint_id)
                tree.selection_set(endpoint_id)
                tree.focus(endpoint_id)
                tree.focus_set()

    def reveal_topic_card(self, button):
        if not button.winfo_exists():
            return
        button.focus_set()
        viewport = button.master
        while viewport is not self and not isinstance(viewport, tk.Canvas):
            viewport = viewport.master
        if isinstance(viewport, tk.Canvas):
            extent = viewport.bbox('all')
            if extent:
                position = button.winfo_rooty() - viewport.winfo_rooty()
                if position < 0 or position + button.winfo_height() > viewport.winfo_height():
                    viewport.yview_moveto(max(0, (viewport.canvasy(position) - 12) / extent[3]))

    def page_work_item(self):
        matches = rows('SELECT * FROM functions WHERE id=? AND program_id=?', (self.endpoint_id, self.pid))
        if not matches:
            self.show('Functions & Tests')
            self.notify(f'This {self.item_label} is no longer available.')
            return
        endpoint, = matches
        fid = endpoint['id']
        sessions = rows("""
            SELECT * FROM sessions WHERE program_id=? AND function_id=? ORDER BY started_at DESC, id DESC
        """, (self.pid, fid))
        finished, = rows("SELECT COUNT(*) AS count FROM tests WHERE function_id=? AND result!='Not tested'", (fid,))
        kind = 'topic' if self.general else endpoint['work_kind']
        branch = self.path_branch(fid) if kind == 'path' else None
        capture = None
        if kind == 'capture':
            capture, = rows('SELECT status,captured_at FROM request_captures WHERE function_id=?', (fid,))
        origin, _ = self.endpoint_history[-1] if self.endpoint_history else ('Functions & Tests', None)
        list_label = 'Topics' if self.general else 'Endpoints & Tests'
        back_label = 'Parent page' if origin == 'Work item' else ('Overview' if origin == 'Overview' else list_label)
        self.button(self.body, f'←  {back_label}', self.back_to_endpoint_tree, kind='quiet').pack(anchor='w', pady=(0, 5))
        details = {'path': 'Path workspace', 'capture': 'Captured request', 'endpoint': 'Endpoint details', 'topic': 'Topic workspace'}
        detail = details[kind]
        title = endpoint['name'] if branch is None or branch[1] != '/' else branch[0] or 'Unassigned host'
        self.heading(clip(title, 27), detail,
                     ('+ Log time', lambda: self.session_form(linked_item=fid, lock_item=True)))
        board = scroll_area(self.body)
        cards = tk.Frame(board, bg=BG)
        cards.pack(fill='x', pady=(0, 12))
        cards.grid_columnconfigure(0, weight=6, uniform='work')
        cards.grid_columnconfigure(1, weight=4, uniform='work')
        summary = surface(cards, PURPLE_SURFACE)
        summary.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        address = tk.Frame(summary, bg=PURPLE_SURFACE)
        address.pack(fill='x', padx=15, pady=(12, 9))
        label = 'Subject' if kind == 'topic' else ('Path' if branch is not None else 'Request address')
        self.label(address, label, 9, ACCENT, bold=True).pack(anchor='w')
        if kind == 'topic':
            location = self.program_by_id[self.pid]['name']
        else:
            location = f"{branch[0] or 'Unassigned host'}{branch[1]}" if branch else endpoint['endpoint'] or endpoint['name']
        reference = self.label(address, location, 11, bold=True, justify='left')
        reference.pack(fill='x', pady=(3, 0))
        reference.bind('<Configure>', lambda event: reference.configure(wraplength=event.width), add=True)
        facts = tk.Frame(summary, bg=PURPLE_SURFACE)
        facts.pack(fill='x', padx=15, pady=(0, 12))
        progress = (endpoint['status'] or 'Not Started') if kind == 'topic' else endpoint_progress(endpoint)
        own_seconds = sum(session['seconds'] for session in sessions)
        facts_content = [('Study time' if kind == 'topic' else 'Logged here', duration(own_seconds))]
        if kind == 'topic':
            facts_content.append(('Sessions', str(len(sessions))))
        elif branch is not None:
            facts_content.append(('Path total', duration(own_seconds + branch[4])))
        elif capture is not None:
            facts_content.append(('HTTP status', str(capture['status'] or 'No response')))
        else:
            facts_content.append(('Test status', endpoint['status'] or 'Discovered'))
        facts_content.append(('Progress', progress))
        for index, (label, value) in enumerate(facts_content):
            col = tk.Frame(facts, bg=PURPLE_SURFACE)
            col.pack(side='left', fill='x', expand=True)
            self.label(col, label, 9, MUTED).pack(anchor='w')
            if label == 'Progress':
                progress_badge(col, value, prominent=True).pack(anchor='w', pady=(3, 0))
            else:
                self.label(col, value, 14, ACCENT if index == 0 else FG, bold=True).pack(anchor='w', pady=(2, 0))

        notes = surface(cards)
        notes.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        note_head = tk.Frame(notes, bg=PANEL)
        note_head.pack(fill='x', padx=14, pady=(6, 2))
        self.label(note_head, 'Notes', 13, bold=True).pack(side='left')
        self.button(note_head, 'Edit notes', lambda: self.edit_item_text(endpoint, 'notes'), kind='quiet').pack(side='right')
        note_text = ' '.join((endpoint['notes'] or '').split())
        preview = clip(note_text, 175) if note_text else 'No notes yet.'
        content = self.label(notes, preview, 10, FG if note_text else MUTED, justify='left')
        content.pack(fill='x', padx=14, pady=(4, 12))
        content.bind('<Configure>', lambda event: content.configure(wraplength=event.width), add=True)

        if kind == 'topic':
            tk.Frame(notes, height=1, bg=BORDER).pack(fill='x', padx=14, pady=(0, 3))
            work_head = tk.Frame(notes, bg=PANEL)
            work_head.pack(fill='x', padx=14)
            self.label(work_head, 'Work completed', 13, bold=True).pack(side='left')
            self.button(work_head, 'Edit work', lambda: self.edit_item_text(endpoint, 'work_done'), kind='quiet').pack(side='right')
            summary = clip(' '.join((endpoint['work_done'] or '').split()), 175)
            work = self.label(notes, summary or 'No work recorded yet.', 10, FG if summary else MUTED, justify='left')
            work.pack(fill='x', padx=14, pady=(4, 12))
            work.bind('<Configure>', lambda event: work.configure(wraplength=event.width), add=True)

        tools = self.tools(board)
        tools.pack_configure(pady=(0, 10))
        self.label(tools, 'Set progress', 9, MUTED).pack(side='left', padx=(0, 6))
        values = PROGRESS_STATES if progress in PROGRESS_STATES else (*PROGRESS_STATES, progress)
        selector = ttk.Combobox(tools, values=values, state='readonly', width=15)
        selector.current(values.index(progress))
        selector.pack(side='left', padx=(0, 8))
        selector.bind('<<ComboboxSelected>>', lambda _: self.set_work_progress(fid, selector.get()))
        self.button(tools, 'Start timer', lambda: self.start_endpoint_timer(fid)).pack(side='left', padx=(0, 6))
        self.button(tools, 'Focus', lambda: self.open_focus_for_item(fid), kind='quiet').pack(side='left')
        if kind == 'endpoint':
            self.button(tools, 'Checklist', lambda: self.test_checklist(fid)).pack(side='left', padx=(0, 6))
            self.button(tools, 'Edit endpoint', lambda: self.function_form(endpoint), kind='quiet').pack(side='left')
            self.label(tools, f"{finished['count']} of {len(CATEGORIES)} checklist entries updated",
                       9, MUTED).pack(side='right')
        if kind == 'topic':
            self.button(tools, 'Edit topic', lambda: self.function_form(endpoint), kind='quiet').pack(side='left')
        if kind in ('topic', 'endpoint'):
            self.button(tools, f'Delete {kind}', lambda: self.delete_tracked_item(endpoint), kind='danger').pack(
                side='left', padx=(5, 0))
        if capture is not None:
            self.label(tools, 'Captured ' + capture['captured_at'], 9, MUTED).pack(side='right')

        if kind == 'topic':
            self.goals.linked_goals(board, self.pid, fid)
        if branch is not None:
            self.path_sections(board, fid, branch, sessions)
        else:
            self.endpoint_sessions(board, sessions)

    def path_branch(self, fid):
        base, = rows('SELECT host,path FROM path_work WHERE function_id=?', (fid,))
        host, prefix = base['host'], base['path']

        def within(candidate_host, path):
            return candidate_host == host and (prefix == '/' or path == prefix or path.startswith(prefix + '/'))

        paths = rows("""
            SELECT w.path,f.id,f.notes,f.reviewed_at,f.progress_state,
                   COALESCE((SELECT SUM(seconds) FROM sessions WHERE program_id=w.program_id AND function_id=f.id),0) AS seconds
            FROM path_work w JOIN functions f ON f.id=w.function_id WHERE w.program_id=? AND w.host=?
        """, (self.pid, host))
        path_items = {item['path']: item for item in paths if item['path'] != prefix and within(host, item['path'])}
        leaves = [(path, item) for (item_host, path, _), item in self.endpoint_locations(self.tracked_items())
                  if path and within(item_host, path)]
        captures = rows("""
            SELECT c.id,c.status,c.captured_at,r.method,r.url,f.notes,f.reviewed_at,f.progress_state,
                   COALESCE((SELECT SUM(seconds) FROM sessions WHERE program_id=r.program_id AND function_id=c.function_id),0) AS seconds
            FROM request_captures c JOIN requests r ON r.id=c.request_id
            LEFT JOIN functions f ON f.id=c.function_id WHERE r.program_id=? ORDER BY c.id DESC
        """, (self.pid,))
        matching = []
        for record in captures:
            try:
                item_host, path, _ = parse_request_url(record['url'])
            except ValueError:
                continue
            if within(item_host, path):
                record['path'] = path
                matching.append(record)

        children = set()
        for path in [*path_items, *(path for path, _ in leaves), *(r['path'] for r in matching)]:
            relative = path[len(prefix.rstrip('/')):].strip('/')
            if relative:
                children.add(f"{prefix.rstrip('/')}/{relative.split('/', 1)[0]}")
        subpaths = [(path, path_items.get(path)) for path in sorted(children)]
        branch_seconds = (sum(item['seconds'] for item in path_items.values()) +
                          sum(item['seconds'] for _, item in leaves) + sum(item['seconds'] for item in matching))
        direct_endpoints = [item for path, item in leaves if path.rstrip('/') == prefix.rstrip('/')]
        return host, prefix, subpaths, matching, branch_seconds, direct_endpoints

    def path_sections(self, board, fid, branch, sessions):
        host, _, subpaths, captures, _, endpoints = branch
        tabs = ttk.Notebook(board)
        tabs.pack(fill='x', pady=(0, 10))
        paths, requests, history = [tk.Frame(tabs, bg=BG) for _ in range(3)]
        tabs.add(paths, text='Subpaths & methods')
        tabs.add(requests, text=f'Requests ({len(captures)})')
        tabs.add(history, text=f'Sessions ({len(sessions)})')
        self.endpoint_sessions(history, sessions)

        columns = (('Path or method', 240), ('Progress', 104), ('Own time', 95), ('Notes', 170))
        if subpaths or endpoints:
            self.label(paths, 'Double-click a row to open its own work page.', 9, MUTED).pack(anchor='w', pady=(5, 0))
            table = self.tree(paths, columns, height=min(len(subpaths) + len(endpoints), 7))
            progress_tree_tags(table)
            actions = {}
            for index, (path, item) in enumerate(subpaths):
                key = f'p{index}'
                actions[key] = lambda path=path: self.open_path(host, path)
                table.insert('', 'end', iid=key, values=(path, endpoint_progress(item),
                             duration(item['seconds']) if item else '0 s', item['notes'] if item else ''),
                             tags=('progress:' + endpoint_progress(item),))
            for item in endpoints:
                key = f"e{item['id']}"
                actions[key] = lambda key=item['id']: self.open_work_item(key)
                table.insert('', 'end', iid=key, values=(item['name'], endpoint_progress(item),
                             duration(item['seconds']), item['notes']),
                             tags=('progress:' + endpoint_progress(item),))
            self.bind_open_row(table, lambda key: actions[key]())
        else:
            self.label(paths, 'No subpaths or methods recorded.', 10, MUTED).pack(anchor='w', pady=7)

        if captures:
            controls = self.tools(requests)
            search = tk.StringVar()
            self.label(controls, 'Find request', 9, MUTED).pack(side='left', padx=(2, 7))
            self.entry(controls, 24, textvariable=search).pack(side='left', ipady=3)
            self.label(controls, 'Double-click to open a request.', 9, MUTED).pack(side='right')
            table = self.tree(requests, (('Request', 85), ('Method and path', 190), ('HTTP', 56), ('Captured (UTC)', 145),
                              ('Progress', 106), ('Time', 76), ('Notes', 150)), height=min(len(captures), 8))

            progress_tree_tags(table)
            table.column('Method and path', minwidth=165)
            table.column('Captured (UTC)', minwidth=128)
            table.column('Notes', minwidth=112)

            def values(record):
                stamp = record['captured_at'].replace('T', ' ')[:19]
                return (f"#{record['id']}", f"{record['method']} {record['path']}", record['status'] or '—', stamp,
                        endpoint_progress(record), duration(record['seconds']),
                        ' '.join((record['notes'] or '').splitlines()))

            def render(*_):
                query = search.get().strip().casefold()
                shown = [record for record in captures if query in ' '.join(map(str, values(record))).casefold()]
                self.fill_tree(table, shown, values)
                for record in shown:
                    table.item(str(record['id']), tags=('progress:' + endpoint_progress(record),))

            search.trace_add('write', render)
            render()
            self.bind_open_row(table, lambda key: self.open_capture(int(key)))
        else:
            self.label(requests, 'No captured requests under this path.', 10, MUTED).pack(anchor='w', pady=7)
        tabs.select(self.path_tabs.get(fid, 0))
        tabs.bind('<<NotebookTabChanged>>', lambda _: self.path_tabs.__setitem__(fid, tabs.index(tabs.select())))

    def bind_open_row(self, tree, action):
        def open_row(key):
            if key:
                action(key)
                return 'break'
        tree.bind('<Double-1>', lambda event: open_row(tree.identify_row(event.y)))
        tree.bind('<Return>', lambda _: open_row(tree.focus()))

    def endpoint_sessions(self, board, sessions):
        self.label(board, 'Study sessions' if self.general else 'Logged sessions', 12, bold=True).pack(anchor='w', pady=(0, 3))
        if not sessions:
            self.label(board, f'No time has been logged for this {self.item_label}.', 10, MUTED).pack(anchor='w')
            return
        tools = self.tools(board)
        table = self.tree(board, (('Start', 170), ('Time', 95), ('Session notes', 250)), height=min(len(sessions), 6))
        self.fill_tree(table, sessions, lambda row: (row['started_at'].replace('T', ' ', 1),
                        duration(row['seconds']), ' '.join((row['notes'] or '').splitlines())))

        def edit_selected_session():
            session_id = self.selected(table, 'session')
            if session_id is not None:
                session = next(row for row in sessions if row['id'] == session_id)
                return self.session_form(session, lock_item=True)

        self.button(tools, 'Edit session', edit_selected_session, kind='quiet').pack(side='left')
        self.button(tools, 'Delete session', lambda: self.delete_session(table), kind='danger').pack(side='left', padx=6)
        table.bind('<Double-1>', lambda event: edit_selected_session() if table.identify_row(event.y) else None)
        table.bind('<Return>', lambda _: edit_selected_session())

    def delete_tracked_item(self, item):
        item_id = item['id']
        if self.session_function == item_id:
            self.notify('Save the current timer session before deleting this item.')
            return
        noun = self.item_label
        detail = (f"Delete “{item['name']}”, its notes and linked goals? "
                  f"Logged sessions will stay in this {self.project_label} as Unassigned.")
        if not self.general:
            detail += '\nTest results will be removed. Captured requests stay available.'
        if self.confirm_delete(f'Delete {noun}', detail,
                               "DELETE FROM functions WHERE id=? AND program_id=? AND work_kind='endpoint'",
                               (item_id, self.pid)):
            if self.fid == item_id:
                self.fid = None
            if self.session_filter == item_id:
                self.session_filter = None
            self.back_to_endpoint_tree()
            self.notify(f'{noun.title()} deleted.')

    def edit_item_text(self, item, field):
        label, statement = {
            'notes': ('Notes', 'UPDATE functions SET notes=? WHERE id=? AND program_id=?'),
            'work_done': ('Work completed', 'UPDATE functions SET work_done=? WHERE id=? AND program_id=?'),
        }[field]

        def save(data):
            execute(statement, (data[field], item['id'], self.pid))

        return self.form(label, ((field, label, item[field], 'text'),), save)

    def set_work_progress(self, item_id, state):
        if state not in PROGRESS_STATES:
            return
        statement = ('UPDATE functions SET status=? WHERE id=? AND program_id=?' if self.general else
                     'UPDATE functions SET progress_state=? WHERE id=? AND program_id=?')
        try:
            execute(statement, (state, item_id, self.pid))
        except sqlite3.Error as error:
            self.show(self.page)
            messagebox.showerror('Could not save progress', str(error), parent=self)
            return
        self.show(self.page)
        self.notify(f'Progress set to {state}.')

    def start_endpoint_timer(self, fid):
        if self.session_program is not None:
            if self.session_program != self.pid or self.session_function != fid:
                self.notify('Save the current timer session before starting another item.')
                return
            if self.started is not None:
                self.notify(f'The timer is already running for this {self.item_label}.')
                return
        self.fid = fid
        self.toggle_timer()
        self.notify(f'Timer running for this {self.item_label}.')

    def add_function(self):
        return self.function_form()

    def edit_function(self):
        return self.edit_item(self.function_tree)

    def edit_item(self, tree):
        fid = self.selected(tree, self.item_label)
        if fid is not None:
            return self.function_form(rows('SELECT * FROM functions WHERE id=? AND program_id=?', (fid, self.pid))[0])

    def log_item_time(self, tree=None):
        fid = self.selected(tree if tree is not None else self.function_tree, self.item_label)
        if fid is not None:
            return self.session_form(linked_item=fid)

    def function_form(self, function=None):
        item_label = self.item_label
        fallback_status = 'Not Started' if self.general else 'Discovered'
        initial = function or {'name': '', 'endpoint': '', 'status': fallback_status, 'notes': '', 'work_done': ''}
        fields = [('name', f'{item_label.title()} name', initial['name'], None)]
        if not self.general:
            fields.append(('endpoint', 'Endpoint path', initial['endpoint'], None))
        fields.extend((
            ('status', 'Status', initial['status'] or fallback_status, PROGRESS_STATES if self.general else STATUSES),
            ('notes', 'Notes', initial['notes'], 'text'),
        ))
        if self.general:
            fields.append(('work_done', 'Work completed / exercises solved', initial['work_done'], 'text'))
        pid = self.pid

        def save(data):
            if not data['name']:
                raise ValueError(f'Enter a {item_label} name.')
            values = (data['name'], data.get('endpoint', ''), data['status'], data['notes'], data.get('work_done', ''))
            try:
                if function:
                    execute('UPDATE functions SET name=?,endpoint=?,status=?,notes=?,work_done=? WHERE id=?',
                            (*values, function['id']))
                else:
                    execute('INSERT INTO functions(program_id,name,endpoint,status,notes,work_done) VALUES(?,?,?,?,?,?)',
                            (pid, *values))
            except sqlite3.IntegrityError as exc:
                duplicate = 'A topic with this name already exists.' if self.general else 'An endpoint with this name and path already exists.'
                raise ValueError(duplicate) from exc

        return self.form(f'Edit {item_label}' if function else f'New {item_label}', fields, save)

    def test_checklist(self, fid=None):
        if fid is None:
            fid = self.selected(self.function_tree, 'endpoint')
        if fid is None:
            return
        win = tk.Toplevel(self, bg=BG)
        win.title('Test checklist')
        win.geometry('490x540')
        win.minsize(415, 350)
        win.transient(self)
        self.label(win, 'Test checklist', 17, bold=True).pack(anchor='w', padx=16, pady=(13, 8))
        footer = tk.Frame(win, bg=BG)
        footer.pack(side='bottom', fill='x', padx=16, pady=12)
        body = tk.Frame(win, bg=BG)
        body.pack(fill='both', expand=True, padx=16)
        form = scroll_area(body)
        existing = {record['category']: record['result'] for record in rows('SELECT * FROM tests WHERE function_id=?', (fid,))}
        choices = TEST_RESULT
        variables = {}
        for i, category in enumerate(CATEGORIES):
            line = tk.Frame(form, bg=PANEL if i % 2 == 0 else CARD)
            line.pack(fill='x')
            self.label(line, category, 10).pack(side='left', padx=8, pady=7)
            value = existing.get(category, 'Not tested')
            var = tk.StringVar(value=value)
            ttk.Combobox(line, textvariable=var, values=tuple(choices), state='readonly', width=17).pack(
                side='right', padx=8, pady=4)
            variables[category] = var
        def save():
            results = [(fid, category, val.get()) for category, val in variables.items()]
            try:
                with closing(connect()) as conn, conn:
                    conn.executemany("""
                        INSERT INTO tests(function_id,category,result) VALUES(?,?,?)
                        ON CONFLICT(function_id,category) DO UPDATE SET result=excluded.result
                    """, results)
            except sqlite3.Error as exc:
                messagebox.showerror('Save error', str(exc), parent=win)
                return
            win.destroy()
            if self.page == 'Work item':
                self.show(self.page)
            self.notify('Checklist saved.')
        self.button(footer, 'Save', save, kind='primary').pack(side='right')
        self.button(footer, 'Cancel', win.destroy, kind='quiet').pack(side='right', padx=7)
        win.bind('<Escape>', lambda _: win.destroy())
        win.grab_set()

    def track_function(self):
        fid = self.selected(self.function_tree, self.item_label)
        if fid is not None:
            self.fid = fid
            self.notify(f'{self.item_label.title()} linked to the next timer session.')
            self.update_timer()

    def page_assets(self):
        self.heading('Assets & Requests', 'Hosts and saved HTTP requests for the selected program.',
                     ('Import file', self.import_assets) if self.pid is not None else None)
        if not self.need_program():
            return
        notebook = ttk.Notebook(self.body)
        notebook.pack(fill='both', expand=True)
        asset_tab, request_tab = (tk.Frame(notebook, bg=BG) for _ in range(2))
        notebook.add(asset_tab, text='Assets')
        notebook.add(request_tab, text='HTTP requests')
        assets = self.tree(asset_tab, (('Host', 315), ('Source', 125), ('Status', 90)), export=True, multiple=True)
        self.fill_tree(assets, rows('SELECT * FROM assets WHERE program_id=? ORDER BY id DESC', (self.pid,)),
                       lambda r: (r['host'], r['source'], r['status']))
        requests = self.tree(request_tab, (('Method', 80), ('URL', 310), ('Status', 69), ('Last seen', 150)),
                             export=True, multiple=True)
        self.fill_tree(requests, rows('SELECT * FROM requests WHERE program_id=? ORDER BY id DESC LIMIT 1000', (self.pid,)),
                       lambda r: (r['method'], r['url'], r['status'], r['seen_at']))
        if not assets.get_children():
            self.label(asset_tab, 'No assets recorded yet.', 9, MUTED).pack(anchor='w', pady=(5, 0))
        if not requests.get_children():
            self.label(request_tab, 'No HTTP requests recorded yet.', 9, MUTED).pack(anchor='w', pady=(5, 0))
    def import_assets(self, path=None):
        if not self.pid or self.general:
            return
        rules = parse_scope(self.program_by_id[self.pid]['scope_rules'])
        if not any(not rule.denied and rule.path is None for rule in rules):
            self.notify('Add a host or wildcard import scope rule before importing subdomains.')
            return
        path = path or filedialog.askopenfilename(parent=self, filetypes=[
            ('Hostname list or JSON Lines', '*.txt *.jsonl *.json'), ('All files', '*.*')])
        if not path:
            return
        accepted = {}
        skipped = invalid = 0
        try:
            with Path(path).open(encoding='utf-8-sig', errors='replace') as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        host, status = asset_record(line)
                    except ValueError:
                        invalid += 1
                        continue
                    if not in_scope(rules, host):
                        skipped += 1
                        continue
                    if status or host not in accepted:
                        accepted[host] = status
            with closing(connect()) as conn, conn:
                conn.executemany("""
                    INSERT INTO assets(program_id,host,source,status) VALUES(?,?,'File import',?)
                    ON CONFLICT(program_id,host) DO UPDATE
                    SET status=CASE WHEN excluded.status != '' THEN excluded.status ELSE assets.status END
                """, [(self.pid, host, status) for host, status in accepted.items()])
        except (OSError, sqlite3.Error) as error:
            messagebox.showerror('Import error', str(error), parent=self)
            return
        self.show(self.page)
        self.notify(f'{len(accepted)} in scope  ·  {skipped} outside scope  ·  {invalid} invalid')

    def page_sessions(self):
        self.heading('Sessions', 'Timer and manually logged sessions.',
                     ('+ Log time', self.manual_session) if self.pid is not None else None)
        if not self.need_program():
            return
        records = rows(SESSION_QUERY, (self.pid,))
        bar = self.tools()
        self.button(bar, 'Edit', self.edit_session).pack(side='left', padx=(0, 7))
        self.button(bar, 'Delete session', lambda: self.delete_session(self.session_tree), kind='danger').pack(
            side='left', padx=(0, 7))
        self.button(bar, 'Export CSV', self.export_sessions).pack(side='left', padx=(0, 8))
        choices = [(None, f'All {self.item_label}s'), (0, 'Unassigned')]
        items = rows('SELECT id,name FROM functions WHERE program_id=? ORDER BY name COLLATE NOCASE, id', (self.pid,))
        choices.extend((item['id'], f"{clip(item['name'], 21)} · {item['id']}") for item in items)
        selector = ttk.Combobox(bar, state='readonly', values=[title for _, title in choices], width=24)
        selector.pack(side='left')
        selector.current(next((index for index, (key, _) in enumerate(choices) if key == self.session_filter), 0))
        totals = self.label(bar, '', 9, MUTED)
        totals.pack(side='right')
        self.session_tree = self.tree(self.body, (('Start', 190), ('Time', 91), (self.item_label.title(), 130), ('Notes', 250)))

        def render(_=None):
            self.session_filter = choices[selector.current()][0]
            shown = self.filter_sessions(records)
            totals.configure(text=f"{count_label(len(shown), 'session')}  ·  {hours(sum(row['seconds'] for row in shown))}")
            self.fill_tree(self.session_tree, shown, lambda row: (
                row['started_at'].replace('T', ' ', 1), duration(row['seconds']),
                row['function_name'] or 'Unassigned', ' '.join((row['notes'] or '').splitlines())))
            if not shown:
                self.notify('No sessions match this selection.')

        selector.bind('<<ComboboxSelected>>', render)
        render()
        self.session_tree.bind('<Double-1>', lambda event: self.edit_session() if self.session_tree.identify_row(event.y) else None)
        self.session_tree.bind('<Return>', lambda _: self.edit_session())

    def filter_sessions(self, records):
        if self.session_filter is None:
            return records
        return [row for row in records if (row['function_id'] or 0) == self.session_filter]

    def manual_session(self):
        return self.session_form(linked_item=self.session_filter)

    def edit_session(self):
        session_id = self.selected(self.session_tree, 'session')
        if session_id is not None:
            return self.session_form(rows('SELECT * FROM sessions WHERE id=?', (session_id,))[0])

    def delete_session(self, tree):
        session_id = self.selected(tree, 'session')
        if session_id is None:
            return
        matches = rows('SELECT started_at,seconds FROM sessions WHERE id=? AND program_id=?',
                       (session_id, self.pid))
        if not matches:
            self.show(self.page)
            return
        session, = matches
        detail = f"Delete the {duration(session['seconds'])} session from {display_date(session['started_at'])}? This removes it from the logged totals."
        if self.confirm_delete('Delete session', detail, 'DELETE FROM sessions WHERE id=? AND program_id=?',
                               (session_id, self.pid)):
            self.show(self.page)
            self.notify('Session deleted.')

    def session_form(self, session=None, *, linked_item=None, lock_item=False, project_id=None, goal_window=None):
        pid = project_id if project_id is not None else self.pid
        start = session['started_at'] if session else datetime.now().isoformat(timespec='seconds')
        seconds = session['seconds'] if session else 1800
        whole, rest = divmod(seconds, 3600)
        minutes, seconds_part = divmod(rest, 60)
        original_start = start.replace('T', ' ', 1)
        function_id = session['function_id'] if session else linked_item
        if function_id is None and session is None and project_id is None:
            function_id = self.fid
        function_id = function_id or 0
        original_duration = f'{whole:02}:{minutes:02}:{seconds_part:02}'
        fields = [
            ('start', 'Start (e.g. 2026-11-15 14:30)', original_start, None),
            ('duration', 'Duration (e.g. 1.5 hours or 01:30)', original_duration, None),
        ]
        if not lock_item:
            functions = {0: 'Unassigned'}
            functions.update({r['id']: f"{r['name']}  ·  #{r['id']}" for r in rows(
                'SELECT id,name FROM functions WHERE program_id=? ORDER BY name COLLATE NOCASE', (pid,))})
            fields.append(('function', f'Linked {self.item_label}', function_id, functions))
        fields.append(('notes', 'Session notes', session['notes'] if session else '', 'text'))
        def save(data):
            try:
                timestamp = datetime.fromisoformat(data['start'])
                if ' ' not in data['start'] and 'T' not in data['start']:
                    raise ValueError
            except ValueError as exc:
                raise ValueError('Enter a start date and time, e.g. 2026-11-15 14:30.') from exc
            if goal_window is not None:
                first, last = goal_window
                latest = min(last, date.today()) if last else date.today()
                if not first <= timestamp.date() <= latest:
                    raise ValueError(f'Choose a session date between {first} and {latest}.')
            try:
                total = parse_duration(data['duration'])
                finish = timestamp + timedelta(seconds=total)
            except (ValueError, OverflowError) as exc:
                raise ValueError('Enter hours as a number (1.5) or as hours:minutes (01:30).') from exc
            started_at = timestamp.isoformat(timespec='seconds')
            ended_at = finish.isoformat(timespec='seconds')
            if session and data['start'] == original_start and total == seconds:
                started_at, ended_at = session['started_at'], session['ended_at']
            fid = (function_id if lock_item else data['function']) or None
            values = (fid, started_at, ended_at, total, data['notes'])
            if session:
                execute('UPDATE sessions SET function_id=?,started_at=?,ended_at=?,seconds=?,notes=? WHERE id=?',
                        (*values, session['id']))
            else:
                execute('INSERT INTO sessions(program_id,function_id,started_at,ended_at,seconds,notes) VALUES(?,?,?,?,?,?)',
                        (pid, *values))
        return self.form('Edit session' if session else 'Log time', fields, save)

    def export_sessions(self):
        records = self.filter_sessions(rows(SESSION_QUERY, (self.pid,)))
        if not records:
            self.notify('There are no sessions to export.')
            return
        path = filedialog.asksaveasfilename(parent=self, initialfile=f'{self.workspace}-sessions.csv',
                                            defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if not path:
            return
        def csv_text(value):
            text = str(value or '')
            return "'" + text if text.lstrip().startswith(('=', '+', '-', '@')) or text.startswith(('\t', '\r', '\n')) else text
        try:
            if self.is_active_data(path):
                raise OSError('Choose an export destination outside the active data files.')
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(('Start', 'End', 'Duration (seconds)', self.item_label.title(), 'Notes'))
                for row in records:
                    writer.writerow((csv_text(row['started_at']), csv_text(row['ended_at']), row['seconds'],
                                     csv_text(row['function_name']), csv_text(row['notes'])))
        except OSError as error:
            messagebox.showerror('Could not save CSV', str(error), parent=self)
            return
        self.notify('Sessions exported to CSV.')

    def open_focus_for_item(self, item_id):
        self.focus_selection[self.workspace] = (self.pid, item_id)
        self.show('Focus')

    def page_focus(self):
        settings, = rows('SELECT selected_cycle FROM focus_settings WHERE id=1')
        presets = rows('SELECT * FROM focus_cycles ORDER BY id')
        current = next((row for row in presets if row['id'] == settings['selected_cycle']), presets[0])
        cycle = self.active_cycle or Cycle.from_row(current)
        project_id, item_id = self.focus_selection.get(self.workspace, (self.pid, self.fid))
        if project_id not in self.program_by_id:
            project_id = item_id = None
        projects = [(None, 'Standalone timer')]
        projects.extend((pid, f"{clip(project['name'], 22)} · {pid}") for pid, project in self.program_by_id.items())
        items = [(None, 'Unassigned')]
        if project_id is not None:
            items.extend((item['id'], f"{clip(item['name'], 24)} · {item['id']}") for item in rows(
                'SELECT id,name FROM functions WHERE program_id=? ORDER BY name COLLATE NOCASE,id', (project_id,)))
        if item_id not in {key for key, _ in items}:
            item_id = None

        self.heading('Focus', 'Plan a cycle, set your pace, and track your time.', ('Alert settings', self.edit_focus_settings))
        choices = self.tools()
        if self.active_cycle:
            project = self.all_programs.get(self.session_program)
            target = project['name'] if project else 'Standalone timer'
            if self.session_function is not None:
                linked = rows('SELECT name FROM functions WHERE id=?', (self.session_function,))
                if linked:
                    target += '  /  ' + linked[0]['name']
            self.label(choices, 'Current cycle', 9, MUTED, bold=True).pack(side='left', padx=(0, 9))
            self.label(choices, clip(target, 60), 11, FG, bold=True).pack(side='left')
        else:
            self.label(choices, self.project_label.title(), 9, MUTED, bold=True).pack(side='left', padx=(0, 6))
            project_picker = ttk.Combobox(choices, state='readonly', width=23, values=[label for _, label in projects])
            project_picker.current(next(index for index, (key, _) in enumerate(projects) if key == project_id))
            project_picker.pack(side='left', padx=(0, 12))
            project_picker.bind('<<ComboboxSelected>>', lambda _: self.select_focus_target(projects[project_picker.current()][0], None))
            self.label(choices, self.item_label.title(), 9, MUTED, bold=True).pack(side='left', padx=(0, 6))
            item_picker = ttk.Combobox(choices, state='readonly', width=23, values=[label for _, label in items])
            item_picker.current(next(index for index, (key, _) in enumerate(items) if key == item_id))
            item_picker.pack(side='left')
            item_picker.bind('<<ComboboxSelected>>', lambda _: self.select_focus_target(project_id, items[item_picker.current()][0]))

        board = scroll_area(self.body)
        panels = tk.Frame(board, bg=BG)
        panels.pack(fill='x', pady=(9, 12))
        panels.grid_columnconfigure(0, weight=6, uniform='focus')
        panels.grid_columnconfigure(1, weight=5, uniform='focus')

        shade = GREEN_SOFT if self.active_cycle and self.timer_mode == 'break' else PURPLE_SURFACE
        stage_color = GREEN if shade == GREEN_SOFT else ACCENT
        hero = surface(panels, shade)
        hero.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        self.focus_default_seconds = cycle.focus * 60
        self.focus_caption = self.label(hero, '', 11, stage_color, bold=True)
        self.focus_caption.pack(anchor='center', pady=(17, 4))
        self.focus_clock = self.label(hero, '', 38, FG, bold=True, font='Consolas')
        self.focus_clock.pack(anchor='center')
        self.focus_progress = ttk.Progressbar(hero, mode='determinate', maximum=100, style='Focus.Horizontal.TProgressbar')
        self.focus_progress.pack(fill='x', padx=20, pady=(11, 9))
        self.focus_next_label = self.label(hero, '', 10, MUTED)
        self.focus_next_label.pack(anchor='center', padx=9, pady=(1, 9))
        buttons = tk.Frame(hero, bg=shade)
        buttons.pack(pady=(0, 14))
        self.focus_action_button = self.button(
            buttons, 'Start cycle', lambda: self.toggle_timer() if self.session_start else
            self.start_cycle(project_id, item_id, cycle), kind='primary')
        self.focus_action_button.pack(side='left')
        if self.active_cycle and self.timer_mode == 'break':
            self.button(buttons, 'Skip break', self.skip_cycle_break, kind='quiet').pack(side='left', padx=(6, 0))

        plan = surface(panels)
        plan.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        heading = self.tools(plan)
        heading.pack_configure(padx=12, pady=(9, 3))
        self.label(heading, 'Cycle', 13, FG, bold=True).pack(side='left')
        if self.active_cycle:
            self.label(plan, clip(cycle.name, 31), 11, ACCENT, bold=True).pack(anchor='w', padx=13)
        else:
            self.button(heading, '+ New', self.edit_cycle, kind='quiet').pack(side='right')
            preset = ttk.Combobox(plan, state='readonly', values=[row['name'] for row in presets])
            preset.current(next(index for index, row in enumerate(presets) if row['id'] == current['id']))
            preset.pack(fill='x', padx=13)
            preset.bind('<<ComboboxSelected>>', lambda _: self.select_cycle(presets[preset.current()]['id']))
        values = tk.Frame(plan, bg=PANEL)
        values.pack(fill='x', padx=13, pady=(8, 6))
        for index, (title, value) in enumerate((
            ('Focus', f'{cycle.focus} min'), ('Short break', f'{cycle.short} min' if cycle.short else 'Off'),
            ('Long break', f'{cycle.long} min' if cycle.long else 'Off'), ('Rounds', str(cycle.rounds)),
        )):
            values.grid_columnconfigure(index % 2, weight=1, uniform='plan')
            tile = tk.Frame(values, bg=ALT)
            tile.grid(row=index // 2, column=index % 2, sticky='nsew', padx=2, pady=2)
            self.label(tile, title, 9, MUTED).pack(anchor='w', padx=7, pady=(4, 0))
            self.label(tile, value, 12, FG, bold=True).pack(anchor='w', padx=7, pady=(0, 4))
        round_strip = tk.Frame(plan, bg=PANEL)
        round_strip.pack(fill='x', padx=14, pady=(1, 4))
        stages = cycle.phases()
        completed_rounds = sum(phase.mode == 'pomodoro' for phase in stages[:self.cycle_phase]) if self.active_cycle else 0
        active_round = stages[self.cycle_phase].round_number if self.active_cycle else 1
        for index in range(cycle.rounds):
            number = index + 1
            color, foreground = ((GREEN_SOFT, GREEN) if number <= completed_rounds else
                                 (ACCENT, INK) if number == active_round else (ALT, MUTED))
            round_strip.grid_columnconfigure(index, weight=1, uniform='rounds')
            tk.Label(round_strip, text='✓' if number <= completed_rounds else str(number), bg=color, fg=foreground,
                     font=(FONT, 9, 'bold'), padx=2, pady=4).grid(row=0, column=index, sticky='ew', padx=(0, 2))
        footer = self.tools(plan)
        footer.pack_configure(padx=13, pady=(2, 8))
        self.label(footer, 'Automatic' if cycle.auto else 'Manual transitions', 9, MUTED).pack(side='left')
        if not self.active_cycle:
            self.button(footer, 'Edit', lambda: self.edit_cycle(current), kind='quiet').pack(side='right')
            if current['id'] != 1:
                self.button(footer, 'Delete', lambda: self.delete_cycle(current), kind='quiet').pack(side='right')

        quick = surface(board)
        quick.pack(fill='x', pady=(0, 6))
        bar = self.tools(quick)
        bar.pack_configure(padx=13, pady=(8, 7))
        self.label(bar, 'Quick timers', 12, FG, bold=True).pack(side='left')
        self.button(bar, 'Test alert', self.test_timer_alert, kind='quiet').pack(side='right')
        actions = self.tools(quick)
        actions.pack_configure(padx=13, pady=(0, 10))
        custom = self.button(actions, 'Custom countdown', lambda: self.custom_countdown(project_id, item_id))
        custom.pack(side='left', padx=(0, 5))
        self.focus_new_buttons.append(custom)
        for name, minutes in (('Short break', cycle.short), ('Long break', cycle.long)):
            if minutes:
                button = self.button(actions, f'{name} · {minutes} min',
                                     lambda length=minutes: self.start_limited_timer(project_id, None, 'break', length * 60))
                button.pack(side='left', padx=(0, 5))
                self.focus_new_buttons.append(button)
        if self.active_cycle:
            hint = ('This standalone cycle does not create work sessions.' if self.session_program is None else
                    'Focus rounds are saved to the original project. Breaks are excluded.')
        else:
            hint = ('Standalone timers run without logging work.' if project_id is None else
                    'New focus rounds and countdowns log work to this selection. Breaks are excluded.')
        self.label(board, hint, 9, MUTED, wraplength=510, justify='left').pack(anchor='w', pady=(2, 4))

    def select_cycle(self, preset_id):
        execute('UPDATE focus_settings SET selected_cycle=? WHERE id=1', (preset_id,))
        self.show('Focus')

    def edit_cycle(self, preset=None):
        defaults = preset or {'name': '', 'focus_minutes': 25, 'short_minutes': 5,
                              'long_minutes': 15, 'cycle_size': 4, 'auto_advance': 0}
        fields = [('name', 'Cycle name', defaults['name'], None)]
        limits = (
            ('focus_minutes', 'Focus minutes', 1, 180),
            ('short_minutes', 'Short break minutes (0 to skip)', 0, 60),
            ('long_minutes', 'Long break minutes (0 to skip)', 0, 60),
            ('cycle_size', 'Focus rounds', 1, 12),
        )
        fields.extend((key, label, defaults[key], None) for key, label, _, _ in limits)
        fields.append(('auto_advance', 'Phase transitions', defaults['auto_advance'],
                       {0: 'Start each phase manually', 1: 'Start next phase automatically'}))

        def save(data):
            if not data['name'] or len(data['name']) > 60:
                raise ValueError('Enter a cycle name of up to 60 characters.')
            values = [data['name']]
            for key, label, minimum, maximum in limits:
                try:
                    value = int(data[key])
                except ValueError:
                    value = -1
                if not minimum <= value <= maximum:
                    raise ValueError(f'{label} must be a whole number from {minimum} to {maximum}.')
                values.append(value)
            values.append(data['auto_advance'])
            with closing(connect()) as conn, conn:
                try:
                    if preset:
                        preset_id = preset['id']
                        conn.execute("""
                            UPDATE focus_cycles SET name=?,focus_minutes=?,short_minutes=?,long_minutes=?,cycle_size=?,auto_advance=?
                            WHERE id=?
                        """, (*values, preset_id))
                    else:
                        preset_id = conn.execute("""
                            INSERT INTO focus_cycles(name,focus_minutes,short_minutes,long_minutes,cycle_size,auto_advance)
                            VALUES(?,?,?,?,?,?)
                        """, values).lastrowid
                except sqlite3.IntegrityError as error:
                    raise ValueError('Another cycle already uses that name.') from error
                conn.execute('UPDATE focus_settings SET selected_cycle=? WHERE id=1', (preset_id,))

        return self.form('Edit cycle' if preset else 'New cycle', fields, save)

    def delete_cycle(self, preset):
        if preset['id'] == 1:
            return
        if self.confirm_delete('Delete cycle', f"Delete the {preset['name']} cycle preset?",
                               'DELETE FROM focus_cycles WHERE id=?', (preset['id'],)):
            self.select_cycle(1)

    def start_cycle(self, project_id, item_id, cycle):
        if self.session_start is not None:
            return
        phase = cycle.phases()[0]
        self.active_cycle = cycle
        self.start_limited_timer(project_id, item_id, phase.mode, phase.seconds)
        self.show('Focus')

    def test_timer_alert(self):
        self.timer_alert('Timer test', 'The BountyTracker timer alert is ready.', test=True)

    def timer_alert(self, title, message, *, test=False):
        settings, = rows('SELECT desktop_alerts,completion_sound FROM focus_settings WHERE id=1')
        desktop = bool(settings['desktop_alerts']) and os.name == 'nt'
        sound = bool(settings['completion_sound'])
        if test and not (desktop or sound):
            self.notify('Turn on desktop notifications or sound in Timer settings.')
            return
        if desktop:
            def deliver():
                try:
                    post_toast(title, message)
                except OSError as error:
                    self.alert_results.put(('desktop', str(error)))
                else:
                    if test:
                        self.alert_results.put(('sent', 'Windows accepted the test notification.'))
            threading.Thread(target=deliver, daemon=True, name='BountyTracker notification').start()
        if sound and os.name == 'nt':
            def play():
                try:
                    play_chime()
                except (RuntimeError, OSError):
                    self.alert_results.put(('sound', ''))
            threading.Thread(target=play, daemon=True, name='BountyTracker timer sound').start()
        elif sound:
            self.bell()
        if test:
            self.notify('Sending the test alert…' if desktop else 'Playing the test sound…')

    def collect_timer_alerts(self):
        while not self.alert_results.empty():
            kind, message = self.alert_results.get_nowait()
            if kind == 'sound':
                self.bell()
            elif kind == 'desktop':
                self.notify(f'Windows notification unavailable. {clip(message, 110)}')
            else:
                self.notify(message)

    def select_focus_target(self, project_id, item_id):
        self.focus_selection[self.workspace] = (project_id, item_id)
        self.show('Focus')

    def edit_focus_settings(self):
        settings, = rows('SELECT desktop_alerts,completion_sound FROM focus_settings WHERE id=1')

        def save(data):
            execute('UPDATE focus_settings SET desktop_alerts=?,completion_sound=? WHERE id=1',
                    (data['desktop_alerts'], data['completion_sound']))

        fields = (
            ('desktop_alerts', 'Windows notifications', settings['desktop_alerts'], {1: 'On', 0: 'Off'}),
            ('completion_sound', 'Completion chime', settings['completion_sound'], {1: 'On', 0: 'Off'}),
        )
        return self.form('Alert settings', fields, save)

    def custom_countdown(self, project_id, item_id):
        if self.session_start is not None:
            self.notify('Save or discard the current timer before starting a new one.')
            return

        def save(data):
            try:
                seconds = parse_duration(data['duration'])
            except (ValueError, OverflowError) as error:
                raise ValueError('Enter hours as 1.5 or hours:minutes as 01:30.') from error
            self.start_limited_timer(project_id, item_id, 'countdown', seconds)

        return self.form('Custom countdown', (('duration', 'Duration (hours or HH:MM:SS)', '00:30:00', None),), save)

    def start_limited_timer(self, project_id, item_id, mode, target):
        if self.session_start is not None:
            self.notify('Save or discard the current timer before starting a new one.')
            return
        self.session_start = datetime.now().isoformat(timespec='seconds')
        self.session_program = project_id
        self.session_function = item_id
        self.timer_mode = mode
        self.timer_limit = target
        self.started = time.monotonic()
        self.save_timer_draft(0)
        self.update_timer()

    def restore_timer(self):
        records = rows('SELECT * FROM timer_draft WHERE id=1')
        if not records:
            return False
        draft, = records
        self.session_program = draft['program_id']
        self.session_function = draft['function_id']
        self.session_start = draft['started_at']
        self.timer_stopped_at = draft['stopped_at']
        self.elapsed = self.timer_checkpoint = draft['elapsed']
        self.timer_mode = draft['mode']
        self.timer_limit = draft['target_seconds']
        try:
            self.active_cycle = Cycle.restore(draft['cycle_plan'])
        except (TypeError, ValueError):
            self.active_cycle = None
        self.cycle_phase = draft['cycle_phase']
        self.cycle_ready = bool(draft['cycle_ready']) and self.active_cycle is not None
        return True

    def elapsed_time(self):
        if self.started is None:
            return self.elapsed
        return self.elapsed + max(0, time.monotonic() - self.started)

    def save_timer_draft(self, elapsed, *, silent=False):
        if self.session_start is None:
            return True
        stopped = self.timer_stopped_at or datetime.now().isoformat(timespec='seconds')
        self.timer_checkpoint = elapsed
        try:
            with closing(connect(timeout=.15 if silent else 1)) as conn, conn:
                write_timer_draft(conn, (
                    self.session_program, self.session_function, self.session_start, stopped, elapsed,
                    self.timer_mode, self.timer_limit, self.active_cycle.serialize() if self.active_cycle else None,
                    self.cycle_phase, int(self.cycle_ready),
                ))
        except sqlite3.Error:
            if not silent:
                self.notify('Could not save the timer draft. Keep the app open to preserve the current timer.')
            return False
        return True

    @staticmethod
    def clock_text(seconds):
        return f'{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}'

    def update_timer(self):
        elapsed = self.elapsed_time()
        remaining = max(0, math.ceil(self.timer_limit - elapsed)) if self.timer_limit is not None else int(elapsed)
        self.timer_label.configure(text=self.clock_text(remaining), fg=ACCENT if self.started is not None else FG)
        status = 'TIMER'
        mode_label = {'pomodoro': 'Focus', 'break': 'Break', 'countdown': 'Countdown'}.get(self.timer_mode)
        if self.session_program is not None:
            name = self.all_programs.get(self.session_program, {}).get('name', '')
            status = f'{mode_label} · {clip(name, 10)}' if mode_label else ('● ' if self.started is not None else 'Ⅱ ') + clip(name, 15)
        elif mode_label:
            status = mode_label + ' · Standalone'
        elif self.fid is not None:
            status = f'{self.item_label.upper()} #{self.fid}'
        self.timer_context.configure(text=status)
        pending = self.session_start is not None
        state = 'normal' if self.pid is not None or pending else 'disabled'
        due = self.timer_limit is not None and elapsed >= self.timer_limit
        action = 'Pause' if self.started is not None else (
            'Finish' if due else 'Resume' if pending and not self.cycle_ready else 'Start')
        if self.active_cycle:
            stages = self.active_cycle.phases()
            phase = stages[self.cycle_phase]
            mode_label = phase.title
            status = f'{phase.title} · {phase.round_number}/{self.active_cycle.rounds}'
            self.timer_context.configure(text=status)
        if self.timer_toggle['state'] != state:
            bg = ACCENT if state == 'normal' else CARD
            self.timer_toggle.configure(state=state, bg=bg, highlightbackground=bg)
        self.timer_toggle.configure(text=action)
        self.timer_save.configure(state='normal' if elapsed >= 1 and self.timer_mode != 'break' and self.session_program is not None else 'disabled')
        if pending and not self.timer_discard.winfo_manager():
            self.timer_discard.pack(side='left', padx=(3, 0))
        elif not pending and self.timer_discard.winfo_manager():
            self.timer_discard.pack_forget()
        if self.focus_clock is not None:
            amount = remaining if pending else self.focus_default_seconds
            self.focus_clock.configure(text=self.clock_text(amount))
            descriptor = 'Running' if self.started is not None else 'Ready' if self.cycle_ready else 'Paused'
            caption = f'{mode_label or "Stopwatch"} · {descriptor}' if pending else 'Ready to focus'
            next_text = ''
            if self.active_cycle:
                caption += f'  ·  Round {phase.round_number} of {self.active_cycle.rounds}'
                if self.cycle_phase + 1 < len(stages):
                    following = stages[self.cycle_phase + 1]
                    next_text = f'Next: {following.title} · {following.seconds // 60} min'
                else:
                    next_text = 'Final phase of this cycle'
            elif not pending:
                next_text = 'Choose a project to log your work.' if self.focus_selection.get(self.workspace, (self.pid, None))[0] is None else ''
            self.focus_caption.configure(text=caption)
            self.focus_next_label.configure(text=next_text)
            completed = min(100, 100 * elapsed / self.timer_limit) if self.timer_limit else 0
            self.focus_progress.configure(value=completed)
            button_text = f'Start {phase.title.lower()}' if self.cycle_ready else f'{action} timer'
            self.focus_action_button.configure(text=button_text if pending else 'Start cycle')
            for button in self.focus_new_buttons:
                button.configure(state='disabled' if pending else 'normal')
        return elapsed

    def tick(self):
        self.collect_timer_alerts()
        if self.page in ('Goals', 'Goal detail') and self.goals.day != date.today():
            self.show(self.page)
        started = self.started
        elapsed = self.update_timer()
        if self.started is not None:
            if self.timer_limit is not None and elapsed >= self.timer_limit:
                self.finish_limited_timer()
            elif elapsed >= self.timer_checkpoint + 5:
                self.save_timer_draft(elapsed, silent=True)

        delay = 250
        if self.started is not None:
            # Align the display to whole seconds while keeping regular progress updates.
            next_second = math.floor(elapsed) + 1 if self.started == started else 1
            until_next_second_ms = (next_second - self.elapsed_time()) * 1000
            delay = max(1, min(delay, math.ceil(until_next_second_ms)))
        self.timer_job = self.after(delay, self.tick)

    def toggle_timer(self):
        if self.pid is None and self.session_start is None:
            return
        if self.timer_limit is not None and self.elapsed_time() >= self.timer_limit:
            self.finish_limited_timer()
            return
        if self.started is None:
            if self.session_start is None:
                self.session_start = datetime.now().isoformat(timespec='seconds')
                self.session_program = self.pid
                self.session_function = self.fid
            if self.cycle_ready:
                self.session_start = datetime.now().isoformat(timespec='seconds')
                self.cycle_ready = False
            self.started = time.monotonic()
            self.timer_stopped_at = None
        else:
            self.pause_timer()
        self.save_timer_draft(self.elapsed)
        self.update_timer()

    def pause_timer(self):
        if self.started is not None:
            self.elapsed = self.elapsed_time()
            if self.timer_limit is not None:
                self.elapsed = min(self.elapsed, self.timer_limit)
            self.started = None
            self.timer_stopped_at = datetime.now().isoformat(timespec='seconds')

    def reset_timer(self):
        if self.pid == self.session_program and self.fid == self.session_function:
            self.fid = None
        self.elapsed = self.timer_checkpoint = 0
        self.started = self.session_start = self.session_program = self.session_function = self.timer_stopped_at = None
        self.timer_mode = 'stopwatch'
        self.timer_limit = None
        self.active_cycle = None
        self.cycle_phase = 0
        self.cycle_ready = False
        self.update_timer()

    def discard_timer(self):
        if self.session_start is None:
            return
        if self.confirm_delete('Discard timer', 'Discard the current timer?',
                               'DELETE FROM timer_draft WHERE id=1', ()):
            self.reset_timer()
            if self.page == 'Focus':
                self.show('Focus')
            self.notify('Timer discarded.')

    def insert_timer_session(self, conn, seconds, notes):
        if self.session_program is not None and self.timer_mode != 'break':
            conn.execute("""
                INSERT INTO sessions(program_id,function_id,started_at,ended_at,seconds,notes)
                VALUES(?,?,?,?,?,?)
            """, (self.session_program, self.session_function, self.session_start,
                  self.timer_stopped_at or datetime.now().isoformat(timespec='seconds'), seconds, notes))

    def persist_completed_timer(self, seconds, notes, *, count_round=False):
        with closing(connect()) as conn, conn:
            self.insert_timer_session(conn, seconds, notes)
            conn.execute('DELETE FROM timer_draft WHERE id=1')
            if count_round:
                conn.execute('UPDATE focus_settings SET rounds=rounds+1 WHERE id=1')
        self.reset_timer()

    def skip_cycle_break(self):
        if self.active_cycle and self.timer_mode == 'break':
            self.finish_cycle_phase(skipped=True)

    def finish_cycle_phase(self, *, skipped=False):
        cycle = self.active_cycle
        stages = cycle.phases()
        phase = stages[self.cycle_phase]
        self.pause_timer()
        if not skipped:
            self.elapsed = phase.seconds
        next_index = self.cycle_phase + 1
        following = stages[next_index] if next_index < len(stages) else None
        stamp = datetime.now().isoformat(timespec='seconds')
        try:
            with closing(connect(timeout=1)) as conn, conn:
                if phase.mode == 'pomodoro':
                    self.insert_timer_session(conn, phase.seconds, f'{cycle.name} · round {phase.round_number}')
                    conn.execute('UPDATE focus_settings SET rounds=rounds+1 WHERE id=1')
                if following is None:
                    conn.execute('DELETE FROM timer_draft WHERE id=1')
                else:
                    write_timer_draft(conn, (
                        self.session_program, self.session_function, stamp, stamp, 0, following.mode,
                        following.seconds, cycle.serialize(), next_index, int(not cycle.auto),
                    ))
        except sqlite3.Error:
            self.save_timer_draft(self.elapsed, silent=True)
            self.update_timer()
            self.notify('Could not finish this phase. Try again.')
            return
        if following is None:
            self.reset_timer()
            message = f'{cycle.name} cycle complete.'
        else:
            self.cycle_phase = next_index
            self.cycle_ready = not cycle.auto
            self.timer_mode = following.mode
            self.timer_limit = following.seconds
            self.session_start = stamp
            self.timer_stopped_at = stamp if self.cycle_ready else None
            self.elapsed = self.timer_checkpoint = 0
            self.started = None if self.cycle_ready else time.monotonic()
            message = (f'Next: {following.title.lower()} is ready.' if self.cycle_ready else
                       f'Next: {following.title.lower()} has started.')
        if self.page in TIMER_REFRESH_PAGES:
            self.show(self.page)
        else:
            self.update_timer()
        if not skipped:
            self.timer_alert(f'{phase.title} complete', message)
        self.notify(message)

    def finish_limited_timer(self):
        if self.active_cycle:
            self.finish_cycle_phase()
            return
        self.pause_timer()
        self.elapsed = self.timer_limit
        mode = self.timer_mode
        recorded = self.session_program is not None and mode != 'break'
        focus = mode == 'pomodoro'
        try:
            self.persist_completed_timer(self.timer_limit, 'Pomodoro focus' if focus else 'Countdown',
                                         count_round=focus)
            if focus:
                message = ('Focus round saved.' if recorded else 'Focus round complete.') + ' Take a break from Focus.'
            elif mode == 'break':
                message = 'Break complete.'
            else:
                message = 'Countdown session saved.' if recorded else 'Countdown complete.'
        except sqlite3.Error:
            self.save_timer_draft(self.elapsed, silent=True)
            self.update_timer()
            self.notify('Timer reached zero. Select Finish to retry saving.')
            return
        if self.page in TIMER_REFRESH_PAGES:
            self.show(self.page)
        title = {'pomodoro': 'Focus complete', 'break': 'Break complete', 'countdown': 'Countdown complete'}[mode]
        self.timer_alert(title, message)
        self.notify(message)

    def save_session(self):
        if self.timer_mode == 'break' or self.session_program is None:
            return
        if self.timer_limit is not None and self.elapsed_time() >= self.timer_limit:
            self.finish_limited_timer()
            return
        if self.started is not None:
            self.toggle_timer()
        seconds = int(self.elapsed)
        if seconds < 1:
            return
        program = self.all_programs.get(self.session_program, {}).get('name', '')

        def save(data):
            fallback = 'Pomodoro focus (partial)' if self.timer_mode == 'pomodoro' else (
                'Countdown (partial)' if self.timer_mode == 'countdown' else '')
            self.persist_completed_timer(seconds, data['notes'] or fallback)

        return self.form('Save timer session',
                         (('notes', f'{clip(program, 30)}  ·  {duration(seconds)}  ·  Session notes', '', 'text'),), save)

    def page_reports(self):
        self.heading('Reports', 'Findings, report status, rewards and notes.',
                     ('+ Add report', self.add_report) if self.pid is not None else None)
        if not self.need_program():
            return
        bar = self.tools()
        self.button(bar, 'Edit report', self.edit_report).pack(side='left')
        self.button(bar, 'Delete report', self.delete_report, kind='danger').pack(side='left', padx=6)
        records = rows('SELECT * FROM reports WHERE program_id=? ORDER BY id DESC', (self.pid,))
        self.label(bar, count_label(len(records), 'report'), 10, MUTED).pack(side='right')
        self.report_tree = self.tree(self.body, (('Report', 203), ('Severity', 80), ('Status', 148), ('Reward', 82), ('Notes', 184)))
        self.fill_tree(self.report_tree, records, lambda r: (
            r['title'], r['severity'],
            r['status'], number(r['bounty'] or 0, 2),
            (r['notes'] or '').replace('\n', ' ')))
        self.report_tree.bind('<Double-1>', lambda event: self.edit_report() if self.report_tree.identify_row(event.y) else None)
        self.report_tree.bind('<Return>', lambda _: self.edit_report())
        if not records:
            self.notify('No reports recorded yet.')

    def add_report(self):
        return self.report_form()

    def edit_report(self):
        report_id = self.selected(self.report_tree, 'report')
        if report_id is not None:
            return self.report_form(rows('SELECT * FROM reports WHERE id=?', (report_id,))[0])

    def delete_report(self):
        report_id = self.selected(self.report_tree, 'report')
        if report_id is None:
            return
        matches = rows('SELECT title FROM reports WHERE id=? AND program_id=?', (report_id, self.pid))
        if not matches:
            self.show(self.page)
            return
        if self.confirm_delete('Delete report', f"Delete “{matches[0]['title']}” and its notes?",
                               'DELETE FROM reports WHERE id=? AND program_id=?', (report_id, self.pid)):
            self.show(self.page)
            self.notify('Report deleted.')

    def report_form(self, report=None):
        initial = report or {'title': '', 'severity': 'Medium', 'status': 'Draft', 'bounty': 0, 'notes': ''}
        pid = self.pid
        fields = (
            ('title', 'Report title', initial['title'], None),
            ('severity', 'Severity', initial['severity'] or 'Medium', SEVERITY),
            ('status', 'Status', initial['status'] or 'Draft', REPORT_STATUS),
            ('bounty', 'Reward amount', str(initial['bounty'] or 0), None),
            ('notes', 'Report notes', initial['notes'], 'text'),
        )
        def save(data):
            if not data['title']:
                raise ValueError('Enter a report title.')
            try:
                bounty = float(data['bounty'].replace(',', '.') or 0)
            except ValueError:
                bounty = -1
            if not math.isfinite(bounty) or bounty < 0:
                raise ValueError('Enter a valid non-negative reward.')
            values = (data['title'], data['severity'], data['status'], bounty, data['notes'])
            if report:
                execute('UPDATE reports SET title=?,severity=?,status=?,bounty=?,notes=? WHERE id=?', (*values, report['id']))
            else:
                execute('INSERT INTO reports(program_id,title,severity,status,bounty,notes) VALUES(?,?,?,?,?,?)', (pid, *values))
        return self.form('Edit report' if report else 'New report', fields, save)

    def page_subdomains(self):
        self.heading('Subdomains', 'Import existing Subfinder or httpx JSONL output, or a hostname list.',
                     ('Import file', self.import_assets) if self.pid is not None else None)
        if not self.need_program():
            return
        if not self.program_by_id[self.pid]['scope_rules']:
            self.empty(self.body, 'Set the program scope first',
                       'Add exact hosts or wildcard rules. Use ! to exclude a host.',
                       ('Edit program scope', self.edit_goal))
            return
        assets = rows('SELECT * FROM assets WHERE program_id=? ORDER BY host', (self.pid,))
        if not assets:
            self.label(self.body, 'No hosts have been recorded yet.', 10, MUTED).pack(anchor='w')
            return
        table = self.tree(self.body, (('Host', 310), ('Source', 140), ('Status', 90)), export=True, multiple=True)
        self.fill_tree(table, assets, lambda item: (item['host'], item['source'], item['status']))

    def page_analytics(self):
        today = date.today()
        period = self.analytics_period
        start, end = bounds(self.analytics_date, period)
        previous_start, previous_end = previous_bounds(start, period)
        project_id = self.analytics_scope.get(self.workspace, self.pid)
        if project_id not in self.program_by_id:
            project_id = None
        goal_rows = rows('SELECT hours FROM analytics_goals WHERE workspace=? AND program_id IS ? AND period=?',
                         (self.workspace, project_id, period))
        target = goal_rows[0]['hours'] if goal_rows else None
        scope_name = self.program_by_id[project_id]['name'] if project_id is not None else f'All {self.project_label}s'
        self.heading('Analytics', 'Saved time by day, week, or month.',
                     ('Edit goal' if target is not None else '+ Set goal',
                      lambda: self.analytics_goal(project_id, target, scope_name)))

        toolbar = self.tools()
        for mode in PERIODS:
            self.button(toolbar, mode, lambda value=mode: self.select_analytics_period(value),
                        kind='primary' if period == mode else 'secondary').pack(side='left', padx=(0, 5))
        choices = [(None, f'All {self.project_label}s')]
        choices.extend((key, f"{clip(project['name'], 21)} · {key}") for key, project in self.program_by_id.items())
        scope = ttk.Combobox(toolbar, state='readonly', width=22, values=[label for _, label in choices])
        scope.current(next(index for index, (key, _) in enumerate(choices) if key == project_id))
        scope.pack(side='right')
        scope.bind('<<ComboboxSelected>>', lambda _: self.select_analytics_scope(choices[scope.current()][0]))

        dates = self.tools()
        dates.pack_configure(pady=(3, 10))
        self.button(dates, '‹', lambda: self.select_analytics_date(shift_period(self.analytics_date, period, -1)), width=3).pack(side='left')
        next_button = self.button(dates, '›', lambda: self.select_analytics_date(shift_period(self.analytics_date, period, 1)), width=3)
        next_button.pack(side='left', padx=(5, 9))
        if end > today:
            next_button.configure(state='disabled')
        self.label(dates, date_range_label(start, end, period), 13, bold=True).pack(side='left')
        self.button(dates, 'Today', lambda: self.select_analytics_date(today), kind='quiet').pack(side='right')

        chart_start = start - timedelta(days=6) if period == 'Daily' else start
        records = rows("""
            SELECT s.id,s.started_at,s.seconds,s.notes,s.program_id,s.function_id,p.name AS project_name,f.name AS item_name
            FROM sessions s JOIN programs p ON p.id=s.program_id
            LEFT JOIN functions f ON f.id=s.function_id AND f.program_id=p.id
            WHERE p.workspace=? AND (? IS NULL OR p.id=?) AND s.started_at>=? AND s.started_at<?
            ORDER BY s.started_at DESC, s.id DESC
        """, (self.workspace, project_id, project_id, min(chart_start, previous_start).isoformat(), end.isoformat()))
        start_key, end_key = start.isoformat(), end.isoformat()
        previous_keys = previous_start.isoformat(), previous_end.isoformat()
        selected_records = [record for record in records if start_key <= record['started_at'][:10] < end_key]
        detail = summarize(selected_records, by_item=project_id is not None)
        previous_total = sum(record['seconds'] for record in records
                             if previous_keys[0] <= record['started_at'][:10] < previous_keys[1])
        chart_totals = summarize(records, by_item=False).daily

        board = scroll_area(self.body)
        metrics = tk.Frame(board, bg=BG)
        metrics.pack(fill='x', pady=(0, 11))
        period_word = {'Daily': 'day', 'Weekly': 'week', 'Monthly': 'month'}[period]
        activity = (f'{duration(detail.seconds // detail.active_days)} / active day'
                    if detail.active_days else 'No active days in this period')
        for index, (title, value, caption, shade) in enumerate((
            ('Logged time', duration(detail.seconds), f'Previous {period_word} · {duration(previous_total)}', PURPLE_SURFACE),
            ('Sessions', str(detail.sessions), 'Saved work sessions', PANEL),
            ('Active days', str(detail.active_days), activity, PANEL),
        )):
            metrics.grid_columnconfigure(index, weight=1, uniform='metrics')
            tile = surface(metrics, shade)
            tile.grid(row=0, column=index, sticky='nsew', padx=(0 if index == 0 else 5, 0 if index == 2 else 5))
            self.label(tile, title, 10, MUTED, bold=True).pack(anchor='w', padx=13, pady=(9, 0))
            self.label(tile, value, 20, ACCENT if index == 0 else FG, bold=True).pack(anchor='w', padx=13)
            self.label(tile, caption, 9, MUTED).pack(anchor='w', padx=13, pady=(1, 10))

        panels = tk.Frame(board, bg=BG)
        panels.pack(fill='x', pady=(0, 11))
        panels.grid_columnconfigure(0, weight=2, uniform='insights')
        panels.grid_columnconfigure(1, weight=3, uniform='insights')
        goal = surface(panels)
        goal.grid(row=0, column=0, sticky='nsew', padx=(0, 6))
        self.label(goal, f'{period} goal', 12, bold=True).pack(anchor='w', padx=14, pady=(12, 4))
        if target is None:
            self.label(goal, 'No time goal set.', 10, MUTED).pack(anchor='w', padx=14, pady=3)
        else:
            target_seconds = round(target * 3600)
            fraction = detail.seconds / target_seconds
            color = GREEN if fraction >= 1 else ACCENT
            self.label(goal, f'{progress_percent(fraction)}%', 22, color, bold=True).pack(anchor='w', padx=14)
            self.label(goal, f'{duration(detail.seconds)} / {duration(target_seconds)}', 11, bold=True).pack(anchor='w', padx=14)
            progress_bar(goal, fraction).pack(fill='x', padx=14, pady=(7, 4))
            status = 'Goal reached' if fraction >= 1 else f'{duration(target_seconds - detail.seconds)} remaining'
            self.label(goal, status, 9, color).pack(anchor='w', padx=14, pady=(0, 10))
        chart = surface(panels)
        chart.grid(row=0, column=1, sticky='nsew', padx=(6, 0))
        self.label(chart, '7-day activity' if period == 'Daily' else 'Time by day', 12, bold=True).pack(
            anchor='w', padx=15, pady=(11, 1))
        activity_chart(self, chart, chart_totals, chart_start, end, selected_day=start if period == 'Daily' else None)

        group_title = self.item_label.title() if project_id is not None else self.project_label.title()
        tabs = ttk.Notebook(board)
        tabs.pack(fill='x')
        breakdown = tk.Frame(tabs, bg=BG)
        history = tk.Frame(tabs, bg=BG)
        tabs.add(breakdown, text=f'Time by {group_title.lower()}')
        tabs.add(history, text=f'Sessions ({detail.sessions})')
        if not detail.groups:
            for tab in (breakdown, history):
                self.label(tab, 'No saved sessions for this period.', 10, MUTED).pack(anchor='w', padx=7, pady=9)
        else:
            table = self.tree(breakdown, ((group_title, 230), ('Logged time', 120), ('Sessions', 85), ('Share of time', 135)),
                              height=min(len(detail.groups), 7))
            groups = [dict(group, id=index) for index, group in enumerate(detail.groups, 1)]
            self.fill_tree(table, groups, lambda group: (group['name'], duration(group['seconds']), group['sessions'],
                f"{number(100 * group['seconds'] / detail.seconds if detail.seconds else 0)}%"))
            table = self.tree(history, (('Started', 175), (group_title, 190), ('Time', 92), ('Notes', 180)),
                              height=min(detail.sessions, 7))
            self.fill_tree(table, selected_records, lambda record: (
                record['started_at'].replace('T', ' ', 1),
                (record['item_name'] or 'Unassigned') if project_id is not None else record['project_name'],
                duration(record['seconds']), ' '.join((record['notes'] or '').splitlines())))
        self.label(board, 'Time is assigned to the day each session started.', 9, MUTED).pack(anchor='w', pady=(8, 3))

    def select_analytics_period(self, period):
        self.analytics_period = period
        self.show('Analytics')

    def select_analytics_date(self, day):
        self.analytics_date = min(date.today(), day)
        self.show('Analytics')

    def select_analytics_scope(self, program_id):
        self.analytics_scope[self.workspace] = program_id
        self.show('Analytics')

    def analytics_goal(self, program_id, target, scope_name):
        workspace, period = self.workspace, self.analytics_period

        def save(data):
            value = data['hours']
            if value:
                try:
                    goal = float(value.replace(',', '.'))
                except ValueError:
                    goal = 0
                if not .01 <= goal <= 100000:
                    raise ValueError('Enter a target between 0.01 and 100,000 hours.')
            with closing(connect()) as conn, conn:
                conn.execute('BEGIN IMMEDIATE')
                args = (workspace, program_id, period)
                if not value:
                    conn.execute('DELETE FROM analytics_goals WHERE workspace=? AND program_id IS ? AND period=?', args)
                    return
                updated = conn.execute('UPDATE analytics_goals SET hours=? WHERE workspace=? AND program_id IS ? AND period=?',
                                       (goal, *args))
                if not updated.rowcount:
                    conn.execute('INSERT INTO analytics_goals(workspace,program_id,period,hours) VALUES(?,?,?,?)',
                                 (*args, goal))

        fields = (('hours', 'Target hours · leave empty to remove goal', str(target) if target is not None else '', None),)
        return self.form(f'{period} goal · {clip(scope_name, 24)}', fields, save)

    def page_settings(self):
        self.heading('Settings', 'Local storage and integrations.')
        storage = surface(self.body)
        storage.pack(fill='x', pady=(0, 11))
        self.label(storage, 'Database & backup', 13, bold=True).pack(anchor='w', padx=16, pady=(13, 3))
        self.label(storage, str(DB), 9, MUTED, wraplength=550, justify='left').pack(anchor='w', padx=16)
        actions = self.tools(storage)
        actions.pack_configure(padx=16, pady=(10, 13))
        self.button(actions, 'Back up SQLite', self.backup, kind='primary').pack(side='left', padx=(0, 7))
        self.button(actions, 'Open data folder', self.open_data).pack(side='left')
        if self.general:
            return
        integration = surface(self.body)
        integration.pack(fill='x')
        self.label(integration, 'Burp connection', 13, bold=True).pack(anchor='w', padx=16, pady=(13, 3))
        label = self.import_listener_message
        self.label(integration, label, 10, GREEN if label.startswith('Listening') else WARN,
                   wraplength=530, justify='left').pack(anchor='w', padx=16)
        self.label(integration, 'Only selected Burp requests that match the program import scope are accepted.',
                   9, MUTED, wraplength=500, justify='left').pack(anchor='w', padx=16, pady=(3, 0))
        self.label(integration, f"Program ID  ·  {self.pid if self.pid is not None else '—'}", 10, MUTED).pack(anchor='w', padx=16, pady=(3, 0))
        self.button(integration, 'Copy connection token', self.copy_token).pack(anchor='w', padx=16, pady=(10, 13))

    def open_data(self):
        if os.name == 'nt':
            os.startfile(BASE)
        else:
            self.notify(str(BASE))

    def copy_token(self):
        self.clipboard_clear()
        self.clipboard_append(TOKEN)
        self.notify('Burp connection token copied.')
    def is_active_data(self, path):
        target = Path(path)
        active_files = (DB, DB.with_suffix('.db-wal'), DB.with_suffix('.db-shm'), TOKEN_FILE, BASE / 'app.lock')
        return any(target.resolve() == active.resolve() or
                   (target.exists() and active.exists() and target.samefile(active))
                   for active in active_files)

    def backup(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.db',
                                            filetypes=[('SQLite database', '*.db')],
                                            initialfile='bountytracker-backup.db')
        if not path:
            return
        try:
            if self.is_active_data(path):
                raise OSError('Choose a separate backup file.')
            if self.session_start and not self.save_timer_draft(self.elapsed_time()):
                raise OSError('The timer could not be included in the backup. Try again.')
            with closing(connect()) as src:
                backup_database(src, Path(path))
        except (OSError, sqlite3.Error) as error:
            messagebox.showerror('Backup error', str(error), parent=self)
            return
        self.notify('Database backup saved.')

    def close(self):
        if self.session_start is not None:
            self.pause_timer()
            if not self.save_timer_draft(self.elapsed):
                self.update_timer()
                if not messagebox.askyesno('Timer draft unavailable',
                                           'Could not save the draft. Close with the last saved timer state?',
                                           default=messagebox.NO, parent=self):
                    return
        self.destroy()


    def destroy(self):
        for job in (self.timer_job, self.notice_job):
            if job is not None:
                self.after_cancel(job)
        self.timer_job = self.notice_job = None
        super().destroy()


def main():
    try:
        set_app_id()
        with acquire_instance(BASE / 'app.lock') as acquired:
            if not acquired:
                messagebox.showinfo('BountyTracker', 'BountyTracker is already open for this data folder.')
                return
            app = App()
            listener = start_import_listener(app)
            try:
                app.mainloop()
            finally:
                if listener is not None:
                    listener.shutdown()
                    listener.server_close()
    except (OSError, sqlite3.Error, tk.TclError) as error:
        messagebox.showerror('BountyTracker could not start', str(error))


if __name__ == '__main__':
    main()
