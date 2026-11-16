# BountyTracker 1.3.0

BountyTracker tracks programs and study subjects locally. Open the separate Windows x64 EXE, or extract the source ZIP and run `run-windows.bat` with Python 3.11+ and Tcl/Tk. Use `build-windows.bat` to build the EXE.

## Set the program import scope

Open a Bug Bounty program and choose **Edit program**. Enter one rule per line in **Import scope**. For example:

```text
example.com
*.example.com
!admin.example.com
```

An exact host covers that host. A wildcard covers its subdomains. Add the apex separately when it is authorized. To allow only a path and its descendants, enter a rule such as `api.example.com/v1/users`. An exclusion prefixed by `!` takes precedence. Use **Scope notes** for free-form notes. Empty rules reject new imports. Existing records are retained.

## Import selected Burp requests

1. Open BountyTracker and find the program ID in **Settings → Burp connection**.
2. In Burp Suite, go to **Extensions → Installed → Add**, select Java, and choose the released JAR. The Java source is in `burp-extension` and can also be built there with Gradle and Java 17.
3. Select already captured requests in HTTP history. Use **Send selected requests to BountyTracker** and enter the program ID. Burp reports accepted, out-of-scope, and rejected counts.
4. Open the program's **Overview** or **Endpoints & Tests**, then click **Refresh**. Use the disclosure arrows to expand the tree. Double-click a host, path or method to open a work page. Endpoint methods appear at the relevant path. Older records without a uniquely identifiable host appear under **Unassigned host**.

The extension transfers the method, URL without query or fragment, and response status to the local receiver on `127.0.0.1:8765`. The method catalog deduplicates endpoints by method and normalized URL. The new extension includes the original capture timestamp when Burp provides it. Exporting that same capture again preserves its existing work record. Request and response bodies are excluded.

## Path workspaces and captured requests

Double-click a path such as **/settings**. This opens a page with its own notes, sessions, timer and **Set progress** selector. **Time on this item** is the time logged directly on the path. **Branch total** includes the path, subpaths, methods and individual captures under it.

- **Subpaths & methods** opens independent work pages for paths such as **/settings/user/password** and individual method endpoints.
- **Requests** lists all imported captures for this path and its descendants. Search or double-click a row to open its own notes, time and progress state. Two captured requests with the same method and URL can have different work records.
- **Sessions** lists only time logged on the current path. Changing progress affects the current item. The method endpoint page still offers its test status and checklist.

Existing request rows carry forward as saved captures. The old release grouped repeated requests into one row, so re-export from Burp's original history if you need the distinct older captures. If Burp supplies no original timestamp, each export creates a new capture.

## Import subdomain results

Use **Subdomains → Import file** to import a one-host-per-line text list or a Subfinder/httpx JSONL file. The host must match a host-wide or wildcard rule, and exclusions apply. Path-only rules apply to requests. Imports report accepted, out-of-scope, and invalid counts. The application reads existing result files without running discovery tools.

## Progress states

Open a topic, method endpoint, path or captured request and use **Set progress**. The choices are **Not Started**, **In Progress**, **Reviewed**, **Completed**, **Needs Revisit**, and **On Hold**. The app saves the selection immediately. Endpoint progress and technical test status stay separate. Older **Done** marks appear as **Completed**.

## Edit and delete records

Open a subject or program to edit or delete it. Open a topic or method endpoint to edit its details or delete it. Use the session and report tables to edit or delete rows. Each delete action asks for confirmation.

Deleting a topic or method endpoint keeps its logged time and session notes under **Unassigned**. Captured requests remain available after deleting a method endpoint. The method can reappear if it is imported again. Deleting a whole subject or program removes its associated records. Deleting a session also removes its time from totals. Save a running or paused timer before deleting the item it belongs to.

## Analytics and period goals

Open **Analytics** from either workspace. Switch between **Daily**, **Weekly**, and **Monthly** and use the arrows to view past periods. Week boundaries are Monday to Sunday. The project selector switches between the whole workspace and one program or subject.

**Set goal** creates a recurring goal for the selected period and scope. Leave the hours field empty in **Edit goal** to remove it. This goal is separate from the all-time project goal. Saved sessions count toward the local calendar day they started. The screen shows logged time, a previous-period total, sessions, active days, an activity chart, time shares by project or item, and session details.

## Goals and outcomes

Open **Goals → + New goal** in either workspace. Choose a **Time target** or **Checklist**, then select the whole workspace, a project, or an individual topic or endpoint. Use **Today**, **This week**, or **This month** to fill the dates, or enter a custom date range. Leave the deadline empty for an open-ended goal.

Time goals update from saved sessions with a start date in the goal's date range, including its deadline. Open a goal to see the work log, how much time is left, and the daily pace needed before the deadline. **Start focus** opens the timer with the goal's project or item selected. Use **Log time** on an active project or item goal to enter a session directly. Workspace goals count sessions saved to any project in that workspace.

Checklist goals track when each task was checked. An unfinished goal stays **Active** through its deadline and becomes **Missed** the next day. A checklist finished late shows the late result. When all tasks are checked by the deadline, or a time target is met, the goal shows as **Reached**. Open a goal to edit it, repeat it as a new goal, or delete it. Deleting a goal keeps its saved sessions. Deleting a project or item also removes the goals linked to that record.

The Goals screen tracks dated and checklist goals. Recurring Analytics targets and all-time project targets remain available in their original screens.

## General and existing data

Select **General** to see subjects and logged time. Open a subject and click a topic card to see its study time, notes, completed work, and session history. Use Log time or Start timer to record work against that topic. Set progress updates the status shown in the topic list. Only Completed counts toward topic completion. Each topic has a separate card with a study-time summary and a labeled progress badge. The subject overview and the full Topics page show the earliest added topics first. Use Search and Progress to filter the full Topics page. The timer stays attached to the program or subject and endpoint or topic it started with.

The app uses the existing database at `%LOCALAPPDATA%\BountyTracker\tracker.db`. Use **Settings → Back up SQLite** to make a checked, standalone backup. To restore, close every BountyTracker window, rename the existing BountyTracker data folder to keep it as a rollback copy, then create a new BountyTracker folder in the same location and put the backup inside as `tracker.db`. This keeps the old SQLite journal files with their original database. A backup contains pending timer time as well as saved sessions. Keep any older BountyTracker copies closed before opening this version.

## Pomodoro and countdown

Open **Focus** from either workspace, or use the **Focus** button on a topic or endpoint page to preselect that item. **Cycle → + New** saves a reusable cycle with a name, focus minutes, short break minutes, long break minutes, and number of rounds. Enter zero for a break to skip it. **Edit** changes the selected preset for future cycles. The initial preset uses your previous timer lengths.

Use **Start cycle** to run the selected plan. Manual transitions wait for you to start the next phase. Automatic transitions start the next phase at the end of the current one. The cycle ends after the last scheduled break. Use **Skip break** to advance without waiting. Closing the app preserves the current phase and remaining time as a paused draft. Saving partial work or discarding the timer ends that cycle.

**Quick timers** provides a custom countdown and standalone breaks. Select a program or subject and optional item to save completed focus rounds and countdowns automatically. Breaks and standalone timers do not create work sessions. Each timed completion sends a Windows notification and a three-tone chime when enabled. Use **Test alert** to check both, or switch them independently in **Alert settings**.

## Timer and local import status

The timer remains attached to the program, subject, endpoint, path, capture, or topic it started with. Closing the window saves a paused draft. On reopening, use **Resume**, **Save**, or **Discard**. During work, the app also checkpoints the timer periodically. Analytics includes sessions after you save them.

Open **Settings → Burp connection** in Bug Bounty to see the local receiver state. If it reports **Unavailable**, check whether an older copy of BountyTracker still has the port open. The app supports one running copy per data folder. The Java extension is under the standard Gradle source directory in the source ZIP.
