"""Goals dashboard, details and editor."""

from collections import Counter
from contextlib import closing
from datetime import date, timedelta
import sqlite3
import tkinter as tk
from tkinter import ttk

from analytics import bounds
from goals import STATES, get_goals, goal_sessions, goal_tasks, save_goal, set_task_done
from ui import (ACCENT, ACCENT_SOFT, BG, BORDER, CARD, FG, FONT, GREEN, GREEN_SOFT, MUTED,
                PANEL, WARN, clip, count_label, display_date, duration, progress_bar,
                progress_percent, scroll_area, surface)


# The same state colors and labels are used on the dashboard and detail page.
PALETTES = {
    'Active': (ACCENT_SOFT, ACCENT, '#987cde', '◐'),
    'Reached': (GREEN_SOFT, GREEN, '#55a57c', '✓'),
    'Missed': ('#fbe8ed', WARN, '#d67b92', '–'),
    'Upcoming': ('#e9effc', '#36578b', '#87a4d7', '◷'),
}
SORTS = ('Priority', 'Deadline', 'Newest', 'Progress')


def date_window(progress):
    start = display_date(progress.goal['starts_on'])
    end = display_date(progress.goal['ends_on'])
    if progress.ends is None:
        return f'From {start} · No deadline'
    return start if progress.starts == progress.ends else f'{start} to {end}'


def metric(progress, value):
    return duration(value) if progress.kind == 'time' else count_label(value, 'task')


def result_text(progress):
    state = progress.state
    if state == 'Reached':
        extra = progress.amount - progress.target
        return f'{duration(extra)} above target' if extra > 0 and progress.kind == 'time' else 'Target reached'
    if state == 'Missed':
        shortfall = progress.target - progress.on_time
        if progress.kind == 'checklist' and not progress.remaining:
            return 'Finished after the deadline'
        return f'{metric(progress, shortfall)} short at deadline'
    if state == 'Upcoming':
        return f'Starts in {count_label((progress.starts - progress.today).days, "day")}'
    remaining = f'{metric(progress, progress.remaining)} left'
    if progress.ends is None:
        return remaining
    days = (progress.ends - progress.today).days
    return remaining + (' · Due today' if days == 0 else f' · {count_label(days, "day")} to go')


def ordered(records, mode):
    if mode == 'Newest':
        return sorted(records, key=lambda p: -p.goal['id'])
    if mode == 'Progress':
        return sorted(records, key=lambda p: (-p.fraction, -p.goal['id']))
    if mode == 'Deadline':
        return sorted(records, key=lambda p: (p.ends or date.max, -p.goal['id']))

    def priority(progress):
        state = progress.state
        if state == 'Active':
            return (0, (progress.ends or date.max).toordinal(), -progress.goal['id'])
        if state == 'Upcoming':
            return (1, progress.starts.toordinal(), -progress.goal['id'])
        return (2, -(progress.ends or progress.starts).toordinal(), -progress.goal['id'])

    return sorted(records, key=priority)


class GoalsPage:
    def __init__(self, app, connect, parse_duration):
        self.app = app
        self.connect = connect
        self.parse_duration = parse_duration
        self.filters = {}
        self.goal_id = None
        self.day = None

    def scope_name(self, goal):
        parts = [name for name in (goal['project_name'], goal['item_name']) if name]
        return ' / '.join(parts) or ('All of General' if self.app.general else 'All of Bug Bounty')

    def open(self, goal_id):
        self.goal_id = goal_id
        self.app.show('Goal detail')

    def render(self):
        app = self.app
        self.day = date.today()
        app.heading('Goals', 'Set a target and see what you reached before the deadline.', ('+ New goal', self.edit))
        with closing(self.connect()) as conn:
            goals = get_goals(conn, app.workspace, self.day)
        state = self.filters.setdefault(app.workspace, {'status': 'All', 'scope': None, 'search': '', 'sort': 'Priority'})

        if not goals:
            starter = surface(app.body, ACCENT_SOFT)
            starter.pack(fill='x')
            app.label(starter, 'Start with one goal', 17, ACCENT, bold=True).pack(anchor='w', padx=19, pady=(18, 5))
            app.label(starter, 'Set a time target from your logged sessions or create a checklist.', 10, MUTED,
                      wraplength=450, justify='left').pack(anchor='w', padx=19, pady=(0, 18))
            return

        totals = Counter(progress.state for progress in goals)
        dashboard = tk.Frame(app.body, bg=BG)
        dashboard.pack(fill='x', pady=(0, 12))
        tiles = {}

        def select(status):
            state['status'] = status
            refresh()

        for index, status in enumerate(STATES):
            shade, ink, edge, marker = PALETTES[status]
            dashboard.grid_columnconfigure(index, weight=1, uniform='goal-state')
            tile = surface(dashboard, shade)
            tile.grid(row=0, column=index, sticky='nsew', padx=(0, 7 if index < 3 else 0))
            caption = app.button(tile, f'{marker}  {status}', lambda key=status: select(key), kind='quiet')
            caption.configure(fg=ink, anchor='w', padx=2, pady=0)
            caption.pack(fill='x', padx=10, pady=(8, 0))
            value = app.label(tile, str(totals[status]), 22, ink, bold=True, cursor='hand2')
            value.pack(fill='x', padx=12, pady=(0, 9))
            for widget in (tile, value):
                widget.bind('<Button-1>', lambda _, key=status: select(key))
            tiles[status] = (tile, edge)

        controls = app.tools()
        controls.pack_configure(pady=(0, 6))
        all_goals = app.button(controls, 'All goals', lambda: select('All'))
        all_goals.pack(side='left', padx=(0, 7))
        scope_choices = [(None, 'All scopes'), (0, 'Workspace only')]
        scope_choices.extend((pid, clip(name, 25)) for pid, name in self.project_choices())
        scope_picker = ttk.Combobox(controls, values=[value for _, value in scope_choices], width=16, state='readonly')
        scope_picker.current(next((index for index, (key, _) in enumerate(scope_choices) if key == state['scope']), 0))
        scope_picker.pack(side='left', padx=(0, 6))
        sort = ttk.Combobox(controls, values=SORTS, width=10, state='readonly')
        sort.set(state['sort'])
        sort.pack(side='right', padx=(6, 0))
        query = tk.StringVar(value=state['search'])
        search = app.entry(controls, 14, textvariable=query)
        search.pack(side='right', fill='x', expand=True, ipady=3)
        app.label(controls, 'Search', 9, MUTED).pack(side='right', padx=(4, 6))

        summary = app.label(app.body, '', 10, MUTED, bold=True)
        summary.pack(anchor='w', pady=(1, 6))
        listing = scroll_area(app.body)
        cards = {progress.goal['id']: self.card(listing, progress) for progress in goals}
        empty = app.label(listing, 'No goals match these filters.', 11, MUTED)
        matching = []
        columns = 0

        def arrange(width, *, force=False):
            nonlocal columns
            count = 2 if width >= 840 and len(matching) > 1 else 1
            if not force and count == columns:
                return
            columns = count
            listing.grid_columnconfigure(0, weight=1, uniform='goal-card')
            listing.grid_columnconfigure(1, weight=int(count == 2), uniform='goal-card' if count == 2 else '')
            for card in cards.values():
                card.grid_forget()
            empty.grid_forget()
            for index, progress in enumerate(matching):
                row, column = divmod(index, columns)
                gap = 8 if count == 2 and column == 0 else 0
                cards[progress.goal['id']].grid(row=row, column=column, sticky='nsew', padx=(0, gap), pady=(0, 8))
            if not matching:
                empty.grid(row=0, column=0, sticky='w', pady=10)

        def refresh(*_):
            nonlocal matching
            selected_scope = scope_choices[scope_picker.current()][0]
            state.update(scope=selected_scope, search=query.get(), sort=sort.get())
            search_text = state['search'].strip().casefold()
            for status, (tile, edge) in tiles.items():
                selected = state['status'] == status
                tile.configure(highlightbackground=edge if selected else BORDER, highlightthickness=2 if selected else 1)
            selected_all = state['status'] == 'All'
            all_goals.configure(fg=ACCENT if selected_all else FG, highlightbackground=ACCENT if selected_all else CARD)
            filtered = []
            for progress in goals:
                goal = progress.goal
                searchable = f"{goal['title']} {goal['notes']} {self.scope_name(goal)}".casefold()
                if (selected_all or progress.state == state['status']) and search_text in searchable and (
                        selected_scope is None or (goal['program_id'] or 0) == selected_scope):
                    filtered.append(progress)
            matching = ordered(filtered, state['sort'])
            arrange(listing.winfo_width(), force=True)
            summary.configure(text=f"{state['status']}  ·  {count_label(len(matching), 'goal')}")
            listing.master.yview_moveto(0)

        listing.bind('<Configure>', lambda event: arrange(event.width), add=True)
        scope_picker.bind('<<ComboboxSelected>>', refresh)
        sort.bind('<<ComboboxSelected>>', refresh)
        watch = query.trace_add('write', refresh)
        controls.bind('<Destroy>', lambda _: query.trace_remove('write', watch), add=True)
        refresh()

    def project_choices(self):
        projects = self.app.program_by_id
        names = Counter(project['name'] for project in projects.values())
        choices = []
        for pid, project in projects.items():
            name = project['name']
            choices.append((pid, name if names[name] == 1 else f'{name} · #{pid}'))
        return choices

    def state_badge(self, parent, progress):
        shade, ink, _, marker = PALETTES[progress.state]
        return tk.Label(parent, text=f'{marker}  {progress.state}', bg=shade, fg=ink,
                        font=(FONT, 10, 'bold'), padx=9, pady=4)

    def card(self, parent, progress, *, details=False):
        app = self.app
        goal = progress.goal
        _, ink, edge, _ = PALETTES[progress.state]
        card = surface(parent)
        tk.Frame(card, bg=edge, width=4).pack(side='left', fill='y')
        content = tk.Frame(card, bg=PANEL)
        content.pack(fill='both', expand=True, padx=(12, 14), pady=9)
        top = app.tools(content)
        self.state_badge(top, progress).pack(side='right', padx=(8, 0))
        kind = 'Time goal' if progress.kind == 'time' else 'Checklist'
        if details:
            app.label(top, 'Time progress' if progress.kind == 'time' else 'Task progress', 13, bold=True).pack(side='left')
        else:
            heading = app.button(top, goal['title'], lambda: self.open(goal['id']), kind='quiet')
            heading.configure(font=(FONT, 13, 'bold'), fg=FG, anchor='w', padx=0, pady=2, justify='left')
            heading.pack(side='left', fill='x', expand=True)
            heading.bind('<Configure>', lambda event: heading.configure(wraplength=max(event.width - 8, 65)), add=True)
            app.label(content, f'{kind}  ·  {clip(self.scope_name(goal), 69)}', 9, MUTED).pack(anchor='w')
        values = app.tools(content)
        values.pack_configure(pady=(5, 2))
        actual = (f'{duration(progress.amount)} / {duration(progress.target)}' if progress.kind == 'time'
                  else f'{progress.amount} of {count_label(progress.target, "task")}')
        app.label(values, actual, 10, FG, bold=True).pack(side='left')
        app.label(values, f'{progress_percent(progress.fraction)}%', 10, ink, bold=True).pack(side='right')
        progress_bar(content, progress.fraction, color=edge).pack(fill='x')
        footer = app.tools(content)
        footer.pack_configure(pady=(4, 0))
        app.label(footer, date_window(progress), 9, MUTED).pack(side='left', padx=(0, 6))
        app.label(footer, result_text(progress), 9, ink, bold=True).pack(side='right')
        return card

    def detail(self):
        app = self.app
        self.day = date.today()
        if self.goal_id is None:
            return app.show('Goals')
        with closing(self.connect()) as conn:
            matches = get_goals(conn, app.workspace, self.day, self.goal_id)
            if matches:
                progress, = matches
                goal = progress.goal
                if progress.kind == 'time':
                    sessions, reached = goal_sessions(conn, progress)
                else:
                    tasks = goal_tasks(conn, self.goal_id)
                    reached = max((row['done_on'] for row in tasks if row['done_on']), default=None) if (
                        progress.state == 'Reached') else None
        if not matches:
            return app.show('Goals')

        app.button(app.body, '← Goals', lambda: app.show('Goals'), kind='quiet').pack(anchor='w')
        app.heading(goal['title'], self.scope_name(goal), ('Edit goal', lambda: self.edit(goal)))
        board = scroll_area(app.body)
        self.card(board, progress, details=True).pack(fill='x', pady=(0, 8))

        actions = app.tools(board)
        actions.pack_configure(pady=(0, 9))
        if progress.kind == 'time' and progress.state == 'Active':
            app.button(actions, 'Start focus', lambda: self.start_focus(goal), kind='primary').pack(side='left', padx=(0, 6))
            if goal['program_id'] is not None:
                app.button(actions, 'Log time', lambda: app.session_form(
                    project_id=goal['program_id'], linked_item=goal['function_id'],
                    lock_item=goal['function_id'] is not None, goal_window=(progress.starts, progress.ends)
                )).pack(side='left', padx=(0, 6))
        app.button(actions, 'Repeat goal', lambda: self.edit(goal, repeat=True)).pack(side='left')
        app.button(actions, 'Delete goal', lambda: self.remove(goal), kind='quiet').pack(side='right')

        if reached:
            app.label(board, f'Reached on {display_date(reached)}', 10, GREEN, bold=True).pack(anchor='w', pady=(0, 7))
        elif progress.kind == 'time' and progress.state == 'Active' and progress.ends is not None:
            days = (progress.ends - self.day).days + 1
            pace = (progress.remaining + days - 1) // days
            app.label(board, f'Pace to deadline  ·  {duration(pace)} per day', 10, ACCENT,
                      bold=True).pack(anchor='w', pady=(0, 7))
        if goal['notes']:
            notes = surface(board)
            notes.pack(fill='x', pady=(0, 8))
            app.label(notes, 'Notes', 12, bold=True).pack(anchor='w', padx=13, pady=(8, 2))
            note = app.label(notes, goal['notes'], 10, MUTED, justify='left')
            note.pack(fill='x', padx=13, pady=(0, 9))
            note.bind('<Configure>', lambda event: note.configure(wraplength=max(50, event.width - 4)), add=True)
        if progress.kind == 'time':
            self.session_list(board, sessions, goal['session_count'])
        else:
            self.checklist(board, progress, tasks)

    def session_list(self, board, sessions, count):
        app = self.app
        panel = surface(board)
        panel.pack(fill='x', pady=(0, 6))
        top = app.tools(panel)
        top.pack_configure(padx=13, pady=(8, 5))
        app.label(top, 'Matching work log', 12, bold=True).pack(side='left')
        total = count_label(count, 'session')
        app.label(top, f'Latest {len(sessions)} of {total}' if count > len(sessions) else total,
                  9, MUTED).pack(side='right')
        if not sessions:
            app.label(panel, 'Saved sessions within the goal dates will appear here.', 10,
                      MUTED).pack(anchor='w', padx=13, pady=(1, 11))
        for record in sessions:
            tk.Frame(panel, bg=BORDER, height=1).pack(fill='x', padx=13)
            row = app.tools(panel)
            row.pack_configure(padx=13, pady=5)
            app.label(row, duration(record['seconds']), 10, ACCENT, bold=True).pack(side='right', padx=(5, 0))
            notes = ' '.join((record['notes'] or '').split())
            title = notes or record['item_name'] or record['project_name']
            app.label(row, clip(title, 68), 10, bold=True).pack(anchor='w')
            app.label(row, f"{display_date(record['started_at'])}  ·  {clip(record['project_name'], 38)}",
                      9, MUTED).pack(anchor='w')

    def checklist(self, board, progress, tasks):
        app = self.app
        panel = surface(board)
        panel.pack(fill='x', pady=(0, 6))
        app.label(panel, 'Checklist', 12, bold=True).pack(anchor='w', padx=13, pady=(8, 5))
        for task in tasks:
            tk.Frame(panel, bg=BORDER, height=1).pack(fill='x', padx=13)
            row = app.tools(panel)
            row.pack_configure(padx=13, pady=5)
            mark = tk.BooleanVar(value=bool(task['done_on']))
            late = bool(progress.ends and task['done_on'] and task['done_on'] > progress.goal['ends_on'])
            date_text = f"Done {display_date(task['done_on'])}" if task['done_on'] else ''
            app.label(row, date_text + (' · Late' if late else ''), 9, WARN if late else MUTED).pack(side='right')
            box = tk.Checkbutton(row, text=task['label'], variable=mark, font=(FONT, 10), bg=PANEL,
                                 fg=GREEN if mark.get() else FG, activebackground=PANEL,
                                 selectcolor=CARD, justify='left', anchor='w',
                                 command=lambda tid=task['id'], value=mark: self.change_task(board, tid, value.get()))
            box.variable = mark
            if progress.state == 'Upcoming':
                box.configure(state='disabled')
            box.pack(side='left', fill='x', expand=True)
            box.bind('<Configure>', lambda event, widget=box: widget.configure(
                wraplength=max(50, event.width - 31)), add=True)

    def change_task(self, board, task_id, checked):
        offset = board.master.yview()[0]
        try:
            with closing(self.connect()) as conn, conn:
                set_task_done(conn, self.app.workspace, self.goal_id, task_id, checked, date.today())
        except sqlite3.Error:
            self.app.show('Goal detail')
            self.app.notify('Could not update the task. Try again.')
            return
        self.app.show('Goal detail')
        canvases = [widget for widget in self.app.body.winfo_children() if isinstance(widget, tk.Canvas)]
        if canvases:
            canvas = canvases[0]
            canvas.after_idle(lambda: canvas.winfo_exists() and canvas.yview_moveto(offset))

    def start_focus(self, goal):
        app = self.app
        app.focus_selection[app.workspace] = (goal['program_id'] or app.pid, goal['function_id'])
        app.show('Focus')
        if app.focus_selection[app.workspace][0] is None and app.session_start is None:
            app.notify(f'Choose a {app.project_label} to log time toward this goal.')

    def remove(self, goal):
        checklist = ' and its checklist' if goal['kind'] == 'checklist' else ''
        removed = self.app.confirm_delete(
            'Delete goal', f"Delete “{goal['title']}”{checklist}? Logged work will stay in Sessions.",
            'DELETE FROM work_goals WHERE id=? AND workspace=?', (goal['id'], goal['workspace'])
        )
        if removed:
            self.app.show('Goals')
            self.app.notify('Goal deleted.')

    def edit(self, goal=None, *, repeat=False):
        return GoalEditor(self, goal, repeat).window


class GoalEditor:
    def __init__(self, page, goal, repeat):
        self.page = page
        self.app = app = page.app
        self.workspace = app.workspace
        self.goal_id = goal['id'] if goal and not repeat else None
        self.today = date.today()
        self.projects = [(None, 'Entire workspace'), *page.project_choices()]
        self.items = []
        goal = goal or {}
        start, end = bounds(self.today, 'Weekly')
        end -= timedelta(days=1)
        if goal:
            start = date.fromisoformat(goal['starts_on'])
            end = date.fromisoformat(goal['ends_on']) if goal['ends_on'] else None
            if repeat:
                end = self.today + min(end - start, date.max - self.today) if end else None
                start = self.today

        self.window = window = tk.Toplevel(app, bg=BG)
        title = 'Repeat goal' if repeat else 'Edit goal' if self.goal_id is not None else 'New goal'
        window.title(title)
        window.transient(app)
        frame = tk.Frame(window, bg=BG, padx=19, pady=12)
        frame.pack(fill='both', expand=True)
        app.label(frame, title, 20, bold=True).pack(anchor='w', pady=(0, 6))
        footer = tk.Frame(frame, bg=BG)
        footer.pack(side='bottom', fill='x', pady=(6, 0))
        app.button(footer, 'Save goal', self.save, kind='primary').pack(side='right')
        app.button(footer, 'Cancel', window.destroy, kind='quiet').pack(side='right', padx=6)
        self.error = app.label(footer, '', 9, WARN, justify='left', wraplength=350)
        self.error.pack(side='left', fill='x', expand=True)
        self.content = content = scroll_area(frame)
        for column in (0, 1):
            content.grid_columnconfigure(column, weight=1, uniform='goal-fields')

        _, self.name = self.entry('Goal name', 0, 0, goal.get('title', ''), span=2)
        self.kind = self.combo('Goal type', 2, 0, ('Time target', 'Checklist'))
        self.kind.current(1 if goal.get('kind') == 'checklist' else 0)
        if self.goal_id is not None:
            self.kind.configure(state='disabled')
        self.target_label, self.target = self.entry(
            'Target hours or HH:MM', 2, 1, self.target_text(goal.get('target_seconds')))
        self.project = self.combo('Scope', 4, 0, [name for _, name in self.projects])
        pid = goal.get('program_id', app.pid)
        self.project.current(next((index for index, (key, _) in enumerate(self.projects) if key == pid), 0))
        self.item = self.combo(app.item_label.title(), 4, 1, ())
        self.reload_items(goal.get('function_id', app.fid))
        self.project.bind('<<ComboboxSelected>>', lambda _: self.reload_items())

        presets = tk.Frame(content, bg=BG)
        presets.grid(row=6, column=0, columnspan=2, sticky='ew', pady=(9, 0))
        for label, period in (('Today', 'Daily'), ('This week', 'Weekly'), ('This month', 'Monthly')):
            app.button(presets, label, lambda value=period: self.set_dates(value)).pack(side='left', padx=(0, 5))
        _, self.start = self.entry('Starts on (YYYY-MM-DD)', 7, 0, start.isoformat())
        _, self.end = self.entry('Deadline (optional)', 7, 1, end.isoformat() if end else '')
        self.tasks_label, self.tasks = self.text_field('Tasks · one per line', 9, 4)
        if goal.get('kind') == 'checklist':
            with closing(page.connect()) as conn:
                self.tasks.insert('1.0', '\n'.join(row['label'] for row in goal_tasks(conn, goal['id'])))
        _, self.notes = self.text_field('Notes (optional)', 11, 3)
        self.notes.insert('1.0', goal.get('notes', ''))
        self.kind.bind('<<ComboboxSelected>>', self.show_kind)
        self.show_kind()
        window.bind('<Escape>', lambda _: window.destroy())
        window.update_idletasks()
        width, height = 637, min(600, max(465, app.winfo_height() - 26))
        left = max(0, app.winfo_rootx() + (app.winfo_width() - width) // 2)
        top = max(0, app.winfo_rooty() + (app.winfo_height() - height) // 2)
        window.geometry(f'{width}x{height}+{left}+{top}')
        window.minsize(540, 430)
        window.grab_set()
        self.name.focus_set()

    def field_label(self, label, row, column, span=1):
        widget = self.app.label(self.content, label, 10, MUTED, bold=True)
        widget.grid(row=row, column=column, columnspan=span, sticky='ew', padx=(0, 8), pady=(7, 2))
        return widget

    def entry(self, label, row, column, value, span=1):
        caption = self.field_label(label, row, column, span)
        field = self.app.entry(self.content, 18)
        field.insert(0, value)
        field.grid(row=row + 1, column=column, columnspan=span, sticky='ew', padx=(0, 8), ipady=4)
        return caption, field

    def combo(self, label, row, column, values):
        self.field_label(label, row, column)
        field = ttk.Combobox(self.content, values=values, width=17, state='readonly')
        field.grid(row=row + 1, column=column, sticky='ew', padx=(0, 8))
        return field

    def text_field(self, label, row, height):
        caption = self.field_label(label, row, 0, 2)
        field = tk.Text(self.content, height=height, width=28, bg=CARD, fg=FG, font=(FONT, 11),
                        insertbackground=FG, wrap='word', padx=6, pady=5, relief='flat',
                        highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT)
        field.grid(row=row + 1, column=0, columnspan=2, sticky='ew', padx=(0, 8))
        return caption, field

    @staticmethod
    def target_text(seconds):
        if not seconds:
            return '2'
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f'{hours:02}:{minutes:02}:{seconds:02}' if seconds else f'{hours:02}:{minutes:02}'

    def reload_items(self, selected=None):
        pid = self.projects[self.project.current()][0]
        self.items = [(None, 'All items' if pid is not None else 'Choose a project first')]
        if pid is not None:
            order = 'id' if self.workspace == 'general' else 'name COLLATE NOCASE, id'
            with closing(self.page.connect()) as conn:
                self.items.extend((row['id'], f"{row['name']}  ·  #{row['id']}") for row in conn.execute(f"""
                    SELECT id,name FROM functions WHERE program_id=? AND work_kind='endpoint' ORDER BY {order}
                """, (pid,)))
        self.item.configure(values=[name for _, name in self.items], state='readonly' if pid is not None else 'disabled')
        self.item.current(next((index for index, (key, _) in enumerate(self.items) if key == selected), 0))

    def show_kind(self, _=None):
        checklist = self.kind.current() == 1
        for widget in (self.target_label, self.target):
            widget.grid_remove() if checklist else widget.grid()
        for widget in (self.tasks_label, self.tasks):
            widget.grid() if checklist else widget.grid_remove()

    def set_dates(self, period):
        self.error.configure(text='')
        start, stop = bounds(self.today, period)
        for field, value in ((self.start, start), (self.end, stop - timedelta(days=1))):
            field.delete(0, 'end')
            field.insert(0, value.isoformat())

    def save(self):
        title = self.name.get().strip()
        task_names = []
        try:
            if not 1 <= len(title) <= 100:
                raise ValueError('Enter a goal name of 1 to 100 characters.')
            try:
                start = date.fromisoformat(self.start.get().strip())
                end_text = self.end.get().strip()
                end = date.fromisoformat(end_text) if end_text else None
            except ValueError as error:
                raise ValueError('Enter valid dates in YYYY-MM-DD format.') from error
            if end is not None and end < start:
                raise ValueError('The deadline must be on or after the start date.')

            kind = 'checklist' if self.kind.current() == 1 else 'time'
            seconds = None
            if kind == 'time':
                try:
                    seconds = self.page.parse_duration(self.target.get().strip())
                except (ValueError, OverflowError) as error:
                    raise ValueError('Enter target hours, such as 2.5 or 02:30.') from error
            else:
                task_names = [line.strip() for line in self.tasks.get('1.0', 'end-1c').splitlines() if line.strip()]
                if not 1 <= len(task_names) <= 50 or any(len(name) > 180 for name in task_names):
                    raise ValueError('Enter 1 to 50 tasks, each up to 180 characters.')
                if len({name.casefold() for name in task_names}) != len(task_names):
                    raise ValueError('Give each task a different name.')
            notes = self.notes.get('1.0', 'end-1c').strip()
            if len(notes) > 4000:
                raise ValueError('Keep notes within 4,000 characters.')
            goal = dict(title=title, workspace=self.workspace, kind=kind, target_seconds=seconds,
                        program_id=self.projects[self.project.current()][0], function_id=self.items[self.item.current()][0],
                        starts_on=start.isoformat(), ends_on=end.isoformat() if end else None, notes=notes)
            goal_id = save_goal(self.page.connect, goal, task_names, self.goal_id)
        except ValueError as error:
            self.error.configure(text=str(error))
            return
        except sqlite3.Error:
            self.error.configure(text='Could not save the goal. Try again.')
            return
        self.window.destroy()
        self.page.open(goal_id)
        self.app.notify('Goal saved.')
