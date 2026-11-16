"""Dated time and checklist goals backed by the local work log."""

from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta


STATES = ('Active', 'Reached', 'Missed', 'Upcoming')


def ensure_schema(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS work_goals(
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL CHECK(length(trim(title)) BETWEEN 1 AND 100),
            workspace TEXT NOT NULL CHECK(workspace IN ('bounty','general')),
            program_id INTEGER REFERENCES programs(id) ON DELETE CASCADE,
            function_id INTEGER REFERENCES functions(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('time','checklist')),
            target_seconds INTEGER,
            starts_on TEXT NOT NULL,
            ends_on TEXT,
            notes TEXT NOT NULL DEFAULT '',
            CHECK(ends_on IS NULL OR ends_on >= starts_on),
            CHECK(function_id IS NULL OR program_id IS NOT NULL),
            CHECK((kind='time' AND target_seconds IS NOT NULL AND target_seconds > 0)
                  OR (kind='checklist' AND target_seconds IS NULL))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goal_tasks(
            id INTEGER PRIMARY KEY,
            goal_id INTEGER NOT NULL REFERENCES work_goals(id) ON DELETE CASCADE,
            position INTEGER NOT NULL,
            label TEXT NOT NULL,
            done_on TEXT
        )
    """)
    conn.execute('CREATE INDEX IF NOT EXISTS work_goals_by_workspace ON work_goals(workspace, program_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS work_goals_by_item ON work_goals(function_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS goal_tasks_by_goal ON goal_tasks(goal_id, position)')


# Sessions follow the local start date used in Analytics. Use the same match
# for progress, completion dates and the work log shown on the detail page.
MATCHED_SESSIONS = """
    FROM work_goals g
    JOIN programs p ON p.workspace=g.workspace AND (g.program_id IS NULL OR p.id=g.program_id)
    JOIN sessions s ON s.program_id=p.id
        AND s.started_at >= g.starts_on
        AND s.started_at < :tomorrow
        AND (g.ends_on IS NULL OR substr(s.started_at, 1, 10) <= g.ends_on)
        AND (g.function_id IS NULL OR s.function_id=g.function_id)
    WHERE g.workspace=:workspace AND g.kind='time' AND (:goal_id IS NULL OR g.id=:goal_id)
"""


def query_scope(workspace, today, goal_id):
    return {'workspace': workspace, 'today': today.isoformat(), 'goal_id': goal_id,
            'tomorrow': (today + timedelta(days=1)).isoformat()}


@dataclass(frozen=True)
class Progress:
    goal: dict
    today: date

    @property
    def kind(self):
        return self.goal['kind']

    @property
    def target(self):
        return self.goal['target_seconds'] if self.kind == 'time' else self.goal['tasks']

    @property
    def amount(self):
        return self.goal['logged_seconds'] if self.kind == 'time' else self.goal['checked_tasks']

    @property
    def on_time(self):
        return self.amount if self.kind == 'time' else self.goal['on_time_tasks']

    @property
    def starts(self):
        return date.fromisoformat(self.goal['starts_on'])

    @property
    def ends(self):
        return date.fromisoformat(self.goal['ends_on']) if self.goal['ends_on'] else None

    @property
    def state(self):
        if self.today < self.starts:
            return 'Upcoming'
        if self.target and self.on_time >= self.target:
            return 'Reached'
        if self.ends is not None and self.today > self.ends:
            return 'Missed'
        return 'Active'

    @property
    def fraction(self):
        return self.amount / self.target if self.target else 0

    @property
    def remaining(self):
        return max(0, self.target - self.amount)


def get_goals(conn, workspace, today, goal_id=None):
    records = conn.execute(f"""
        WITH logged AS (
            SELECT g.id AS goal_id, SUM(s.seconds) AS seconds, COUNT(*) AS sessions
            {MATCHED_SESSIONS}
            GROUP BY g.id
        ), checks AS (
            SELECT t.goal_id, COUNT(*) AS total,
                   COUNT(CASE WHEN t.done_on <= :today THEN 1 END) AS checked,
                   COUNT(CASE WHEN t.done_on <= :today
                          AND (g.ends_on IS NULL OR t.done_on <= g.ends_on) THEN 1 END) AS on_time
            FROM goal_tasks t JOIN work_goals g ON g.id=t.goal_id
            WHERE g.workspace=:workspace AND (:goal_id IS NULL OR g.id=:goal_id)
            GROUP BY t.goal_id
        )
        SELECT g.*, p.name AS project_name, f.name AS item_name,
               COALESCE(l.seconds, 0) AS logged_seconds, COALESCE(l.sessions, 0) AS session_count,
               COALESCE(c.total, 0) AS tasks, COALESCE(c.checked, 0) AS checked_tasks,
               COALESCE(c.on_time, 0) AS on_time_tasks
        FROM work_goals g
        LEFT JOIN programs p ON p.id=g.program_id
        LEFT JOIN functions f ON f.id=g.function_id
        LEFT JOIN logged l ON l.goal_id=g.id
        LEFT JOIN checks c ON c.goal_id=g.id
        WHERE g.workspace=:workspace AND (:goal_id IS NULL OR g.id=:goal_id)
    """, query_scope(workspace, today, goal_id))
    return [Progress(dict(record), today) for record in records]


def goal_sessions(conn, progress):
    scope = query_scope(progress.goal['workspace'], progress.today, progress.goal['id'])
    recent = [dict(row) for row in conn.execute(f"""
        SELECT matched.*, f.name AS item_name
        FROM (
            SELECT s.id, s.function_id, s.started_at, s.seconds, s.notes, p.name AS project_name
            {MATCHED_SESSIONS}
            ORDER BY s.started_at DESC, s.id DESC LIMIT 10
        ) matched LEFT JOIN functions f ON f.id=matched.function_id
        ORDER BY matched.started_at DESC, matched.id DESC
    """, scope)]
    reached_on = None
    if progress.state == 'Reached':
        reached_on = conn.execute(f"""
            WITH days AS (
                SELECT substr(s.started_at, 1, 10) AS day, SUM(s.seconds) AS logged
                {MATCHED_SESSIONS}
                GROUP BY day
            ), totals AS (
                SELECT day, SUM(logged) OVER (ORDER BY day) AS logged FROM days
            )
            SELECT MIN(day) FROM totals WHERE logged >= :target
        """, {**scope, 'target': progress.target}).fetchone()[0]
    return recent, reached_on


def goal_tasks(conn, goal_id):
    return [dict(row) for row in conn.execute(
        'SELECT * FROM goal_tasks WHERE goal_id=? ORDER BY position, id', (goal_id,)
    )]


def save_goal(connect, goal, tasks, goal_id=None):
    workspace = goal['workspace']
    program_id, function_id = goal['program_id'], goal['function_id']
    with closing(connect()) as conn, conn:
        if program_id is not None and conn.execute(
                'SELECT 1 FROM programs WHERE id=? AND workspace=?', (program_id, workspace)).fetchone() is None:
            raise ValueError('Choose a project from this workspace.')
        if function_id is not None and conn.execute("""
                SELECT 1 FROM functions WHERE id=? AND program_id=? AND work_kind='endpoint'
        """, (function_id, program_id)).fetchone() is None:
            raise ValueError('Choose an item from the selected project.')
        if goal_id is None:
            goal_id = conn.execute("""
                INSERT INTO work_goals(title, workspace, program_id, function_id, kind,
                                       target_seconds, starts_on, ends_on, notes)
                VALUES(:title, :workspace, :program_id, :function_id, :kind,
                       :target_seconds, :starts_on, :ends_on, :notes)
            """, goal).lastrowid
        else:
            updated = conn.execute("""
                UPDATE work_goals SET title=:title, program_id=:program_id, function_id=:function_id,
                    target_seconds=:target_seconds, starts_on=:starts_on, ends_on=:ends_on, notes=:notes
                WHERE id=:id AND workspace=:workspace AND kind=:kind
            """, {**goal, 'id': goal_id})
            if not updated.rowcount:
                raise ValueError('This goal is no longer available.')
        if goal['kind'] == 'checklist':
            previous = {row['label'].casefold(): row['done_on'] for row in goal_tasks(conn, goal_id)}
            conn.execute('DELETE FROM goal_tasks WHERE goal_id=?', (goal_id,))
            conn.executemany("""
                INSERT INTO goal_tasks(goal_id,position,label,done_on) VALUES(?,?,?,?)
            """, [(goal_id, index, label, previous.get(label.casefold())) for index, label in enumerate(tasks)])
    return goal_id


def set_task_done(conn, workspace, goal_id, task_id, checked, today):
    conn.execute("""
        UPDATE goal_tasks SET done_on=CASE WHEN :checked THEN COALESCE(done_on, :today) ELSE NULL END
        WHERE id=:task_id AND goal_id=:goal_id AND EXISTS (
            SELECT 1 FROM work_goals g WHERE g.id=:goal_id AND g.workspace=:workspace AND g.starts_on <= :today
        )
    """, {**query_scope(workspace, today, goal_id), 'checked': checked, 'task_id': task_id})
