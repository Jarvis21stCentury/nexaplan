import calendar as calendar_module
import io
import json
import os
import re
from datetime import date, datetime, timedelta

from nicegui import events, ui

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

try:
    import pyttsx3
    import speech_recognition as sr
except ImportError:
    pyttsx3 = None
    sr = None

try:
    from PyPDF2 import PdfReader
except ImportError:
    try:
        from pypdf import PdfReader
    except ImportError:
        PdfReader = None

try:
    import docx
except ImportError:
    docx = None

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ImportError:
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None

    class HttpError(Exception):
        pass


MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
TODO_LIMIT = 20
TEXTBOOK_CHAR_LIMIT = 18000
STUDY_BLOCK_MINUTES = 60
STUDY_BLOCK_TIMES = ["16:00", "18:00", "19:30"]
TODO_DAY_START_HOUR = 7
TODO_DAY_END_HOUR = 21
TODO_DAY_SLOT_MINUTES = 60
TEST_KEYWORDS = [
    "test",
    "exam",
    "quiz",
    "midterm",
    "final",
    "assessment",
    "benchmark",
]

GOOGLE_SCOPES = ["https://www.googleapis.com/auth/calendar"]
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

rn = datetime.now()

EVENT_SCHEMA = {
    "type": "json_schema",
    "name": "calendar_chat_response",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "reply": {"type": "string"},
            "should_create_event": {"type": "boolean"},
            "title": {"type": "string"},
            "date": {"type": "string"},
            "start_time": {"type": "string"},
            "duration_minutes": {"type": "integer"},
            "needs_clarification": {"type": "boolean"},
        },
        "required": [
            "reply",
            "should_create_event",
            "title",
            "date",
            "start_time",
            "duration_minutes",
            "needs_clarification",
        ],
    },
}

SYSTEM_PROMPT = f"""
You are NexaPlanner, a helpful calendar chatbot inside a calm calendar app.
Understand natural user messages and decide whether to add a calendar event.
When the student mentions a test, exam, quiz, final, midterm, or assessment, still return the actual test date as
the event. The app will automatically create study blocks before it.
Use ISO dates (YYYY-MM-DD), 24-hour start times (HH:MM), and concise friendly replies.
The date and time right now is {rn}. Always use 12 hr time in your reply.
"""

STUDY_SYSTEM_PROMPT = """
You are NexaStudy, a focused study coach inside NexaPlanner.
Ask the student one clear question at a time about their chosen subject.
When textbook notes are provided, prioritize that material.
If the student answers, briefly grade the answer, explain the key idea, and ask the next question.
Mix recall, explanation, application, and comparison questions.
Keep responses concise and encouraging.
"""

state = {
    "events": [],
    "todos": [],
    "chat": [
        {
            "role": "assistant",
            "content": "Tell me what you want to plan, or add something manually from the calendar.",
        }
    ],
    "study": {
        "subject": "",
        "textbook_name": "",
        "textbook_text": "",
            "chat": [
            {
                "role": "assistant",
                "content": "Choose a subject, then I can quiz you one question at a time.",
            }
        ],
    },
    "month": {"year": date.today().year, "month": date.today().month},
    "week_anchor": date.today().isoformat(),
    "todo_day": date.today().isoformat(),
    "theme": {
        "accent": "#21b8a0",
        "background": "#f7faf6",
    },
    "google": {
        "connected": False,
        "email": "",
        "status": "Not connected",
    },
    "selected_event_index": None,
    "selected_todo_id": None,
    "next_todo_id": 1,
}


def local_tz():
    return datetime.now().astimezone().tzinfo


def to_local_datetime(day, time_value):
    naive = datetime.strptime(f"{day} {time_value or '09:00'}", "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=local_tz())


def parse_google_datetime(value):
    if not value:
        return None
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).astimezone(local_tz())
    except ValueError:
        return None


def format_time_windows(event):
    start = datetime.strptime(f"{event['date']} {event.get('time', '09:00')}", "%Y-%m-%d %H:%M")
    end = start + timedelta(minutes=event.get("duration", 60))
    return start.strftime("%I:%M %p").lstrip("0"), end.strftime("%I:%M %p").lstrip("0")


def month_label(month_state):
    return date(month_state["year"], month_state["month"], 1).strftime("%B %Y")


def parse_day(day_key):
    return datetime.strptime(day_key, "%Y-%m-%d").date()


def week_start(day_value=None):
    if day_value is None:
        day_value = parse_day(state["week_anchor"])
    elif isinstance(day_value, str):
        day_value = parse_day(day_value)
    return day_value - timedelta(days=(day_value.weekday() + 1) % 7)


def week_days():
    start = week_start()
    return [start + timedelta(days=index) for index in range(7)]


def week_label():
    days = week_days()
    return f"{days[0].strftime('%b %d')} - {days[-1].strftime('%b %d, %Y')}"


def todo_day():
    return parse_day(state["todo_day"])


def todo_day_label():
    return todo_day().strftime("%A, %B %d")


def todo_day_slots():
    start = datetime.combine(todo_day(), datetime.min.time()).replace(hour=TODO_DAY_START_HOUR)
    end = datetime.combine(todo_day(), datetime.min.time()).replace(hour=TODO_DAY_END_HOUR)
    slots = []
    cursor = start
    while cursor <= end:
        slots.append(cursor.strftime("%H:%M"))
        cursor += timedelta(minutes=TODO_DAY_SLOT_MINUTES)
    return slots


def events_for_time_slot(day_key, time_value):
    slot_start = datetime.strptime(f"{day_key} {time_value}", "%Y-%m-%d %H:%M")
    slot_end = slot_start + timedelta(minutes=TODO_DAY_SLOT_MINUTES)
    return [
        item
        for item in events_for_date(day_key)
        if slot_start
        <= datetime.strptime(f"{item['date']} {item.get('time', '09:00')}", "%Y-%m-%d %H:%M")
        < slot_end
    ]


def refresh_month_headings():
    label = month_label(state["month"])
    if "calendar_month_heading" in globals():
        calendar_month_heading.set_text(label)


def refresh_week_heading():
    if "ai_week_heading" in globals():
        ai_week_heading.set_text(week_label())


def refresh_todo_day_heading():
    if "todo_day_heading" in globals():
        todo_day_heading.set_text(todo_day_label())


def shift_month(offset):
    year = state["month"]["year"]
    month = state["month"]["month"] + offset

    while month < 1:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1

    state["month"] = {"year": year, "month": month}
    calendar_panel.refresh()
    refresh_month_headings()


def shift_week(offset):
    anchor = parse_day(state["week_anchor"]) + timedelta(days=offset * 7)
    state["week_anchor"] = anchor.isoformat()
    refresh_named_panels("week_calendar_panel")
    refresh_week_heading()


def shift_todo_day(offset):
    state["todo_day"] = (todo_day() + timedelta(days=offset)).isoformat()
    refresh_named_panels("todo_day_schedule_panel")
    refresh_todo_day_heading()


def go_todo_today():
    state["todo_day"] = date.today().isoformat()
    refresh_named_panels("todo_day_schedule_panel")
    refresh_todo_day_heading()


def go_today():
    today = date.today()
    state["month"] = {"year": today.year, "month": today.month}
    state["week_anchor"] = today.isoformat()
    state["todo_day"] = today.isoformat()
    calendar_panel.refresh()
    refresh_named_panels("week_calendar_panel", "todo_day_schedule_panel")
    refresh_month_headings()
    refresh_week_heading()
    refresh_todo_day_heading()


def events_for_date(day_key):
    return sorted(
        [item for item in state["events"] if item["date"] == day_key],
        key=lambda item: (item.get("time", "00:00"), item.get("title", "")),
    )


def sorted_events():
    return sorted(
        state["events"],
        key=lambda item: (item["date"], item.get("time", "00:00"), item.get("title", "")),
    )


def upcoming_events(limit=6):
    today = date.today().isoformat()
    return [event for event in sorted_events() if event["date"] >= today][:limit]


def events_between(start_date, end_date):
    return [
        event
        for event in sorted_events()
        if start_date.isoformat() <= event["date"] <= end_date.isoformat()
    ]


def planned_minutes(day_key):
    return sum(int(event.get("duration") or 60) for event in events_for_date(day_key))


def next_upcoming_event():
    items = upcoming_events(limit=1)
    return items[0] if items else None


def refresh_named_panels(*names):
    for name in names:
        panel = globals().get(name)
        if panel is None:
            continue
        try:
            panel.refresh()
        except Exception:
            pass


def refresh_planner_panels():
    refresh_named_panels(
        "calendar_panel",
        "week_calendar_panel",
        "agenda_panel",
        "today_lens_panel",
        "planner_metrics_panel",
        "todo_day_schedule_panel",
    )


def google_sync_window():
    visible_month_start = date(state["month"]["year"], state["month"]["month"], 1)
    time_min = datetime.combine(
        visible_month_start - timedelta(days=14),
        datetime.min.time(),
        tzinfo=local_tz(),
    )
    time_max = datetime.combine(
        date.today() + timedelta(days=365),
        datetime.max.time(),
        tzinfo=local_tz(),
    )
    return time_min.isoformat(), time_max.isoformat()


def openai_client():
    if OpenAI is None:
        raise RuntimeError("Install the openai package to use AI features.")

    api_key = OPENAI_API_KEY.strip()
    if not api_key:
        raise RuntimeError("Set OPENAI_API_KEY before using AI features.")

    return OpenAI(api_key=api_key)


def parse_ai_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise


def call_nexa_ai(message):
    client = openai_client()
    recent_events = state["events"][-10:]
    user_content = (
        "Current calendar events as JSON:\n"
        f"{json.dumps(recent_events)}\n\n"
        f"User message:\n{message}"
    )

    if not hasattr(client, "responses"):
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT.strip()},
                {"role": "user", "content": user_content},
            ],
            response_format={"type": "json_object"},
        )
        return parse_ai_json(response.choices[0].message.content)

    response = client.responses.create(
        model=MODEL,
        input=[
            {"role": "system", "content": SYSTEM_PROMPT.strip()},
            {"role": "user", "content": user_content},
        ],
        text={"format": EVENT_SCHEMA},
    )
    return json.loads(response.output_text)


def textbook_excerpt():
    text = state["study"]["textbook_text"].strip()
    if not text:
        return "No textbook uploaded."
    return text[:TEXTBOOK_CHAR_LIMIT]


def call_study_ai(user_message):
    client = openai_client()
    subject = (study_subject.value or state["study"]["subject"] or "the selected subject").strip()
    state["study"]["subject"] = subject

    recent_chat = state["study"]["chat"][-10:]
    prompt = (
        f"Subject: {subject}\n"
        f"Uploaded textbook: {state['study']['textbook_name'] or 'none'}\n\n"
        "Textbook notes/excerpt:\n"
        f"{textbook_excerpt()}\n\n"
        "Recent study chat:\n"
        f"{json.dumps(recent_chat)}\n\n"
        f"Student message: {user_message}"
    )

    if not hasattr(client, "responses"):
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": STUDY_SYSTEM_PROMPT.strip()},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content

    response = client.responses.create(
        model=MODEL,
        input=[
            {"role": "system", "content": STUDY_SYSTEM_PROMPT.strip()},
            {"role": "user", "content": prompt},
        ],
    )
    return response.output_text


def read_upload_content(upload: events.UploadEventArguments):
    content = upload.content
    if hasattr(content, "read"):
        data = content.read()
    else:
        data = content
    return data if isinstance(data, bytes) else bytes(data)


def extract_text_from_upload(upload: events.UploadEventArguments):
    data = read_upload_content(upload)
    name = upload.name or "uploaded textbook"
    extension = os.path.splitext(name)[1].lower()

    if extension == ".pdf":
        if PdfReader is None:
            raise RuntimeError("Install PyPDF2 or pypdf to upload PDF textbooks.")
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if extension == ".docx":
        if docx is None:
            raise RuntimeError("Install python-docx to upload DOCX textbooks.")
        document = docx.Document(io.BytesIO(data))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="ignore")


def handle_textbook_upload(upload: events.UploadEventArguments):
    try:
        text = extract_text_from_upload(upload)
    except Exception as error:
        ui.notify(f"Could not read textbook: {error}", color="negative")
        return

    clean_text = re.sub(r"\s+", " ", text).strip()
    if not clean_text:
        ui.notify("I could not find readable text in that file.", color="warning")
        return

    state["study"]["textbook_name"] = upload.name or "uploaded textbook"
    state["study"]["textbook_text"] = clean_text
    state["study"]["chat"].append(
        {
            "role": "assistant",
            "content": f"Textbook uploaded: {state['study']['textbook_name']}. I can now quiz you from it.",
        }
    )
    ui.notify("Textbook uploaded for Study Coach.", color="positive")
    study_panel.refresh()
    textbook_panel.refresh()


def ask_study_question():
    subject = (study_subject.value or "").strip()
    if not subject:
        ui.notify("Add a subject first, like Biology or U.S. History.", color="warning")
        return

    try:
        reply = call_study_ai("Ask me the first or next study question.")
    except Exception as error:
        reply = f"I could not reach NexaStudy yet: {error}"

    state["study"]["chat"].append({"role": "assistant", "content": reply})
    study_panel.refresh()


def send_study_answer():
    answer = (study_answer.value or "").strip()
    if not answer:
        return

    study_answer.value = ""
    state["study"]["chat"].append({"role": "user", "content": answer})
    study_panel.refresh()

    try:
        reply = call_study_ai(answer)
    except Exception as error:
        reply = f"I could not reach NexaStudy yet: {error}"

    state["study"]["chat"].append({"role": "assistant", "content": reply})
    study_panel.refresh()


def clear_study_session():
    state["study"]["chat"] = [
        {
            "role": "assistant",
            "content": "Study session cleared. Choose a subject, then I can start a fresh quiz.",
        }
    ]
    study_panel.refresh()


recognizer = sr.Recognizer() if sr else None
engine = pyttsx3.init() if pyttsx3 else None


def voice_main():
    if not sr or not recognizer:
        raise RuntimeError("Install pyttsx3 and SpeechRecognition to use voice commands.")

    while True:
        try:
            with sr.Microphone() as source:
                recognizer.adjust_for_ambient_noise(source, duration=0.2)
                audio = recognizer.listen(source)
                text = recognizer.recognize_google(audio)
                return text.lower()
        except sr.UnknownValueError:
            continue


def keyword_recog():
    if not engine:
        raise RuntimeError("Install pyttsx3 to use voice commands.")

    while True:
        response = voice_main()
        if response == "nexa":
            engine.say("Yes?")
            engine.runAndWait()

            response = voice_main()

            engine.say("Ok")
            engine.runAndWait()

            ai_result = call_nexa_ai(response)
            if valid_event(ai_result):
                event = save_event(make_event(ai_result, response))
                if is_test_event(event):
                    for block in build_study_plan(event):
                        save_event(block, notify=False, refresh=False)
                    refresh_planner_panels()


def valid_event(ai_result):
    if not ai_result.get("should_create_event") or ai_result.get("needs_clarification"):
        return False
    if (not ai_result.get("title") or not ai_result.get("date") or not ai_result.get("start_time")):
        return False

    try:
        datetime.strptime(ai_result["date"], "%Y-%m-%d")
        datetime.strptime(ai_result["start_time"], "%H:%M")
    except ValueError:
        return False

    return True


def make_event(ai_result, source):
    return {
        "title": ai_result["title"].strip(),
        "description": "",
        "date": ai_result["date"],
        "time": ai_result["start_time"],
        "duration": max(int(ai_result.get("duration_minutes") or 60), 5),
        "source": source,
        "calendar": "NexaPlanner",
    }


def event_overlaps(candidate, existing):
    candidate_start = datetime.strptime(
        f"{candidate['date']} {candidate.get('time', '09:00')}", "%Y-%m-%d %H:%M"
    )
    candidate_end = candidate_start + timedelta(minutes=int(candidate.get("duration") or 60))
    existing_start = datetime.strptime(
        f"{existing['date']} {existing.get('time', '09:00')}", "%Y-%m-%d %H:%M"
    )
    existing_end = existing_start + timedelta(minutes=int(existing.get("duration") or 60))
    return candidate_start < existing_end and existing_start < candidate_end


def first_open_study_time(day_key, duration=STUDY_BLOCK_MINUTES):
    for time_value in STUDY_BLOCK_TIMES:
        candidate = {"date": day_key, "time": time_value, "duration": duration}
        if not any(event_overlaps(candidate, event) for event in events_for_date(day_key)):
            return time_value
    return STUDY_BLOCK_TIMES[-1]


def is_test_event(event):
    if event.get("event_type") == "study_block":
        return False

    text = f"{event.get('title', '')} {event.get('source', '')}".lower()
    return any(keyword in text for keyword in TEST_KEYWORDS)


def study_subject_for_event(event):
    title = event.get("title", "test").strip()
    subject = re.sub(
        r"\b(test|exam|quiz|midterm|final|assessment|benchmark)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    subject = re.sub(r"\s+", " ", subject).strip(" -:") or title
    return subject


def study_tasks_for(subject):
    return [
        f"Preview the {subject} test topics, gather notes, and list the hardest units.",
        f"Review notes/textbook sections for {subject}; make a one-page key ideas sheet.",
        f"Practice problems or sample questions for {subject}; mark anything missed.",
        f"Relearn weak spots for {subject}; rewrite confusing ideas in your own words.",
        f"Final active recall for {subject}: self-quiz, review formulas/terms, and pack materials.",
    ]


def build_study_plan(test_event):
    try:
        test_day = datetime.strptime(test_event["date"], "%Y-%m-%d").date()
    except ValueError:
        return []

    today = date.today()
    latest_study_day = test_day - timedelta(days=1)
    if latest_study_day < today:
        return []

    available_days = (latest_study_day - today).days + 1
    block_count = min(5, available_days)
    first_study_day = latest_study_day - timedelta(days=block_count - 1)
    subject = study_subject_for_event(test_event)
    tasks = study_tasks_for(subject)[-block_count:]
    plan_id = f"{test_event['date']}::{test_event['title'].lower()}"

    study_blocks = []
    for index, task in enumerate(tasks):
        study_day = first_study_day + timedelta(days=index)
        day_key = study_day.isoformat()
        study_blocks.append(
            {
                "title": f"Study for {subject}: {task.split(';')[0]}",
                "date": day_key,
                "time": first_open_study_time(day_key),
                "duration": STUDY_BLOCK_MINUTES,
                "source": f"Auto study plan for {test_event['title']}",
                "calendar": "NexaPlanner",
                "event_type": "study_block",
                "study_plan_id": plan_id,
                "description": (
                    f"Prep for {test_event['title']} on {test_event['date']}.\n\n"
                    f"During this block: {task}"
                ),
            }
        )

    return study_blocks


def format_study_plan_summary(study_blocks):
    lines = []
    for block in study_blocks:
        start, end = format_time_windows(block)
        pretty_date = datetime.strptime(block["date"], "%Y-%m-%d").strftime("%a, %b %d")
        task = block.get("description", "").split("During this block: ", 1)[-1]
        lines.append(f"- {pretty_date}, {start}-{end}: {task}")
    return "\n".join(lines)


def google_dependencies_ready():
    return all([Request, Credentials, InstalledAppFlow, HttpError, build])


def google_credentials(allow_interactive_login=False):
    if not google_dependencies_ready():
        raise RuntimeError(
            "Install Google libraries: pip install google-api-python-client google-auth-httplib2 google-auth-oauthlib"
        )

    creds = None
    if os.path.exists(GOOGLE_TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(GOOGLE_TOKEN_FILE)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
        with open(GOOGLE_TOKEN_FILE, "w", encoding="utf-8") as token:
            token.write(creds.to_json())
        return creds

    if not allow_interactive_login:
        return None

    if not os.path.exists(GOOGLE_CREDENTIALS_FILE):
        raise RuntimeError("Google OAuth credentials are missing.")

    flow = InstalledAppFlow.from_client_secrets_file(GOOGLE_CREDENTIALS_FILE, GOOGLE_SCOPES)
    creds = flow.run_local_server(port=0)

    with open(GOOGLE_TOKEN_FILE, "w", encoding="utf-8") as token:
        token.write(creds.to_json())

    return creds


def google_service(allow_interactive_login=False):
    creds = google_credentials(allow_interactive_login)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def load_google_status_at_startup():
    try:
        state["google"]["connected"] = bool(google_credentials())
        state["google"]["status"] = "Connected" if state["google"]["connected"] else "Not connected"
    except Exception as error:
        state["google"]["connected"] = False
        state["google"]["email"] = ""
        state["google"]["status"] = f"Not connected: {error}"


def google_event_to_nexa(item):
    start_data = item.get("start", {})
    end_data = item.get("end", {})

    if "date" in start_data:
        start = datetime.strptime(start_data["date"], "%Y-%m-%d").replace(hour=9, minute=0)
        end = start + timedelta(minutes=60)
    else:
        start = parse_google_datetime(start_data.get("dateTime"))
        end = parse_google_datetime(end_data.get("dateTime"))
        if not start:
            return None
        if not end or end <= start:
            end = start + timedelta(minutes=60)

    return {
        "title": item.get("summary", "Untitled"),
        "description": item.get("description", ""),
        "date": start.date().isoformat(),
        "time": start.strftime("%H:%M"),
        "duration": max(int((end - start).total_seconds() // 60), 5),
        "source": "Google Calendar",
        "calendar": "Google",
        "google_id": item.get("id"),
    }


def load_google_events():
    try:
        service = google_service()
        if not service:
            ui.notify("Connect Google Calendar first.", color="warning")
            return

        time_min, time_max = google_sync_window()
        results = (
            service.events()
            .list(
                calendarId=GOOGLE_CALENDAR_ID,
                timeMin=time_min,
                timeMax=time_max,
                maxResults=250,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as error:
        ui.notify(f"Google Calendar refresh failed: {error}", color="negative")
        return
    except Exception as error:
        ui.notify(f"Could not refresh Google Calendar: {error}", color="negative")
        return

    google_events = []
    for item in results.get("items", []):
        event = google_event_to_nexa(item)
        if event:
            google_events.append(event)

    google_events_by_id = {
        event["google_id"]: event
        for event in google_events
        if event.get("google_id")
    }

    synced_events = []
    for event in state["events"]:
        google_id = event.get("google_id")
        if google_id in google_events_by_id:
            synced_events.append(google_events_by_id.pop(google_id))
        elif event.get("calendar") != "Google":
            synced_events.append(event)

    synced_events.extend(google_events_by_id.values())
    state["events"] = synced_events

    ui.notify(f"Synced {len(google_events)} Google event(s).", color="positive")
    refresh_planner_panels()


def connect_google_calendar():
    try:
        google_service(allow_interactive_login=True)
        state["google"]["connected"] = True
        state["google"]["status"] = "Connected"
        ui.notify("Google Calendar connected.", color="positive")
        load_google_events()
    except HttpError as error:
        ui.notify(f"Google Calendar connection failed: {error}", color="negative")
    except Exception as error:
        ui.notify(f"Could not connect Google Calendar: {error}", color="negative")

    google_panel.refresh()


def sync_google_if_connected():
    if state["google"]["connected"]:
        load_google_events()


def push_event_to_google(event):
    service = google_service()
    if not service:
        return event

    start = to_local_datetime(event["date"], event.get("time", "09:00"))
    end = start + timedelta(minutes=event.get("duration", 60))

    body = {
        "summary": event["title"],
        "description": event.get("description", ""),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }

    created = service.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=body).execute()

    event["google_id"] = created.get("id")
    event["calendar"] = "Google"
    event["source"] = "NexaPlanner + Google"
    return event


def google_event_body(event):
    start = to_local_datetime(event["date"], event.get("time", "09:00"))
    end = start + timedelta(minutes=event.get("duration", 60))
    return {
        "summary": event["title"],
        "description": event.get("description", ""),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def update_event_in_google(event):
    if not event.get("google_id"):
        return event

    service = google_service()
    if not service:
        return event

    service.events().update(
        calendarId=GOOGLE_CALENDAR_ID,
        eventId=event["google_id"],
        body=google_event_body(event),
    ).execute()
    return event


def delete_event_from_google(event):
    if not event.get("google_id"):
        return

    service = google_service()
    if not service:
        return

    service.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=event["google_id"]).execute()


def save_event(event, notify=True, refresh=True):
    try:
        if state["google"]["connected"]:
            event = push_event_to_google(event)
            if notify:
                ui.notify("Event added to Google Calendar.", color="positive")
        else:
            if notify:
                ui.notify("Task added.", color="positive")
    except Exception as error:
        if notify:
            ui.notify(f"Saved locally, but Google sync failed: {error}", color="warning")

    state["events"].append(event)
    if refresh:
        refresh_planner_panels()
    return event


def find_event_index(event):
    for index, item in enumerate(state["events"]):
        if item is event:
            return index

    google_id = event.get("google_id")
    if google_id:
        for index, item in enumerate(state["events"]):
            if item.get("google_id") == google_id:
                return index

    for index, item in enumerate(state["events"]):
        if (
            item.get("title") == event.get("title")
            and item.get("date") == event.get("date")
            and item.get("time") == event.get("time")
        ):
            return index

    return None


def selected_event():
    index = state.get("selected_event_index")
    if index is None or index < 0 or index >= len(state["events"]):
        return None
    return state["events"][index]


def open_event_details(event):
    index = find_event_index(event)
    if index is None:
        ui.notify("I could not find that event anymore.", color="warning")
        return

    state["selected_event_index"] = index
    detail_title.value = event.get("title", "")
    detail_description.value = event.get("description", "")
    detail_date.value = event.get("date", date.today().isoformat())
    detail_time.value = event.get("time", "09:00")
    detail_duration.value = int(event.get("duration") or 60)
    event_details_dialog.open()


def update_selected_event():
    event = selected_event()
    if not event:
        ui.notify("Choose an event first.", color="warning")
        return

    try:
        datetime.strptime(detail_date.value, "%Y-%m-%d")
        datetime.strptime(detail_time.value or "09:00", "%H:%M")
    except ValueError:
        ui.notify("Use YYYY-MM-DD and HH:MM for the event.", color="warning")
        return

    event.update(
        {
            "title": (detail_title.value or "Untitled").strip(),
            "description": (detail_description.value or "").strip(),
            "date": detail_date.value,
            "time": detail_time.value or "09:00",
            "duration": int(detail_duration.value or 60),
        }
    )

    try:
        if event.get("google_id"):
            update_event_in_google(event)
            ui.notify("Event updated in Google Calendar.", color="positive")
        else:
            ui.notify("Event updated.", color="positive")
    except Exception as error:
        ui.notify(f"Updated locally, but Google update failed: {error}", color="warning")

    event_details_dialog.close()
    refresh_planner_panels()


def delete_selected_event():
    event = selected_event()
    if not event:
        ui.notify("Choose an event first.", color="warning")
        return

    try:
        if event.get("google_id"):
            delete_event_from_google(event)
    except Exception as error:
        ui.notify(f"Deleted locally, but Google delete failed: {error}", color="warning")

    index = state.get("selected_event_index")
    if index is not None and 0 <= index < len(state["events"]):
        state["events"].pop(index)

    state["selected_event_index"] = None
    event_details_dialog.close()
    ui.notify("Event deleted.", color="positive")
    refresh_planner_panels()


def add_manual_event():
    try:
        datetime.strptime(event_date.value, "%Y-%m-%d")
        datetime.strptime(event_time.value or "09:00", "%H:%M")
    except ValueError:
        ui.notify("Use YYYY-MM-DD and HH:MM for the event.", color="warning")
        return

    event = {
        "title": (event_title.value or "Untitled").strip(),
        "description": (event_description.value or "").strip(),
        "date": event_date.value,
        "time": event_time.value or "09:00",
        "duration": int(event_duration.value or 60),
        "source": "Manual",
        "calendar": "NexaPlanner",
    }

    save_event(event)
    event_dialog.close()


def open_event_dialog(day_key=None, time_value="09:00", title="", description="", duration=60):
    event_date.value = day_key or date.today().isoformat()
    event_time.value = time_value or "09:00"
    event_title.value = title
    event_description.value = description
    event_duration.value = duration
    event_dialog.open()


def open_task_slot(day_key, time_value):
    open_event_dialog(
        day_key=day_key,
        time_value=time_value,
        title="",
        description="",
        duration=TODO_DAY_SLOT_MINUTES,
    )


def handle_slot_click(event_args, day_key, time_value):
    if event_arg_value(event_args):
        return
    open_task_slot(day_key, time_value)


def event_arg_value(event_args):
    args = getattr(event_args, "args", None)
    if isinstance(args, list) and args:
        return args[0]
    if isinstance(args, dict):
        if "task_id" in args:
            return args["task_id"]
        if "task_index" in args:
            return args["task_index"]
        if "args" in args and isinstance(args["args"], list) and args["args"]:
            return args["args"][0]
        if args:
            return next(iter(args.values()))
    return args


def ensure_todo_ids():
    next_id = int(state.get("next_todo_id") or 1)
    changed = False
    for item in state["todos"]:
        if "id" not in item:
            item["id"] = next_id
            next_id += 1
            changed = True

    if changed or next_id > int(state.get("next_todo_id") or 1):
        state["next_todo_id"] = next_id


def find_todo_index(todo_id):
    ensure_todo_ids()
    try:
        todo_id = int(todo_id)
    except (TypeError, ValueError):
        return None

    for index, item in enumerate(state["todos"]):
        if item.get("id") == todo_id:
            return index
    return None


def selected_todo():
    index = find_todo_index(state.get("selected_todo_id"))
    if index is None:
        return None
    return state["todos"][index]


def refresh_todo_views():
    try:
        render_todo_items()
        ui.timer(0.05, render_todo_items, once=True)
    except Exception:
        pass
    refresh_named_panels("todo_day_schedule_panel", "today_lens_panel", "planner_metrics_panel")


def handle_task_drop(event_args, day_key, time_value):
    todo_id = event_arg_value(event_args)
    index = find_todo_index(todo_id)
    if index is None:
        ui.notify("Drag a task onto a time slot.", color="warning")
        return

    task = state["todos"][index]["task"]
    save_event(
        {
            "title": task,
            "description": f"Planned from To Do: {task}",
            "date": day_key,
            "time": time_value,
            "duration": TODO_DAY_SLOT_MINUTES,
            "source": "To Do",
            "calendar": "NexaPlanner",
            "todo_id": state["todos"][index].get("id"),
        },
        notify=False,
    )
    ui.notify("Task added to the day view.", color="positive")


def send_chat():
    message = (chat_input.value or "").strip()
    if not message:
        return

    chat_input.value = ""
    state["chat"].append({"role": "user", "content": message})
    chat_panel.refresh()

    try:
        ai_result = call_nexa_ai(message)
        reply = ai_result.get("reply", "Done.")

        if valid_event(ai_result):
            event = save_event(make_event(ai_result, message))
            start, end = format_time_windows(event)
            readable_date = datetime.strptime(event["date"], "%Y-%m-%d").strftime("%A, %B %d")
            reply = f"{reply}\n\nAdded: {event['title']} on {readable_date}, {start} to {end}."

            if is_test_event(event):
                study_blocks = build_study_plan(event)
                for block in study_blocks:
                    save_event(block, notify=False, refresh=False)

                if study_blocks:
                    refresh_planner_panels()
                    ui.notify(f"Added {len(study_blocks)} study block(s) before the test.", color="positive")
                    reply = (
                        f"{reply}\n\nI also made a study plan before the test:\n"
                        f"{format_study_plan_summary(study_blocks)}"
                    )
                else:
                    reply = (
                        f"{reply}\n\nI recognized this as a test, but there are no future days before it "
                        "to place study blocks."
                    )
    except Exception as error:
        reply = f"I could not reach NexaPlanner yet: {error}"

    state["chat"].append({"role": "assistant", "content": reply})
    chat_panel.refresh()


def add_todo():
    task = (todo_input.value or "").strip()
    if task and len(state["todos"]) < TODO_LIMIT:
        todo_id = int(state.get("next_todo_id") or 1)
        state["todos"].append({"id": todo_id, "task": task, "done": False})
        state["next_todo_id"] = todo_id + 1
        todo_input.value = ""
        ui.notify(f"Added task: {task}", color="positive")
        refresh_todo_views()
    elif len(state["todos"]) >= TODO_LIMIT:
        ui.notify(f"You can keep up to {TODO_LIMIT} tasks in the list.", color="warning")


def toggle_todo(todo_id, done):
    index = find_todo_index(todo_id)
    if index is not None:
        state["todos"][index]["done"] = done
        refresh_todo_views()


def open_todo_details(todo_id):
    index = find_todo_index(todo_id)
    if index is None:
        ui.notify("I could not find that task anymore.", color="warning")
        return

    item = state["todos"][index]
    state["selected_todo_id"] = item["id"]
    todo_detail_task.value = item.get("task", "")
    todo_detail_done.value = bool(item.get("done"))
    todo_details_dialog.open()


def update_selected_todo():
    item = selected_todo()
    if not item:
        ui.notify("Choose a task first.", color="warning")
        return

    task = (todo_detail_task.value or "").strip()
    if not task:
        ui.notify("Task name cannot be empty.", color="warning")
        return

    item["task"] = task
    item["done"] = bool(todo_detail_done.value)
    todo_details_dialog.close()
    ui.notify("Task updated.", color="positive")
    refresh_todo_views()


def remove_todo(todo_id):
    index = find_todo_index(todo_id)
    if index is not None:
        state["todos"].pop(index)
        if state.get("selected_todo_id") == todo_id:
            state["selected_todo_id"] = None
        refresh_todo_views()


def delete_selected_todo():
    todo_id = state.get("selected_todo_id")
    remove_todo(todo_id)
    todo_details_dialog.close()
    ui.notify("Task deleted.", color="positive")


def clear_todos():
    state["todos"] = []
    state["selected_todo_id"] = None
    refresh_todo_views()


def hex_to_rgb(hex_color):
    clean = (hex_color or "").strip().lstrip("#")
    if len(clean) != 6:
        clean = "21b8a0"
    try:
        return tuple(int(clean[index:index + 2], 16) for index in (0, 2, 4))
    except ValueError:
        return (33, 184, 160)


def readable_text_for(hex_color):
    red, green, blue = hex_to_rgb(hex_color)
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    return "#10201d" if luminance > 0.58 else "#ffffff"


def relative_luminance(hex_color):
    channels = []
    for value in hex_to_rgb(hex_color):
        normalized = value / 255
        if normalized <= 0.03928:
            channels.append(normalized / 12.92)
        else:
            channels.append(((normalized + 0.055) / 1.055) ** 2.4)
    return (0.2126 * channels[0]) + (0.7152 * channels[1]) + (0.0722 * channels[2])


def contrast_ratio(first, second):
    light = max(relative_luminance(first), relative_luminance(second))
    dark = min(relative_luminance(first), relative_luminance(second))
    return (light + 0.05) / (dark + 0.05)


def readable_word_color(preferred, background, light_fallback, dark_fallback):
    if contrast_ratio(preferred, background) >= 4.5:
        return preferred
    return light_fallback if readable_text_for(background) == "#10201d" else dark_fallback


def background_theme_values(hex_color):
    red, green, blue = hex_to_rgb(hex_color)
    luminance = (0.299 * red + 0.587 * green + 0.114 * blue) / 255
    is_dark = luminance <= 0.48

    if is_dark:
        return {
            "ink": "#f4fbf8",
            "muted": "#a9bbb6",
            "panel": "rgba(16,24,27,.84)",
            "line": "rgba(244,251,248,.16)",
            "glass_border": "rgba(255,255,255,.12)",
            "glass_shadow": "0 22px 70px rgba(0,0,0,.36)",
            "soft_bg": "rgba(16,24,27,.70)",
            "day_bg": "rgba(16,24,27,.74)",
            "day_hover": "rgba(24,36,39,.94)",
            "assistant_bg": "rgba(16,24,27,.78)",
            "tile_a": "rgba(24,36,39,.78)",
            "tile_b": "rgba(12,18,20,.50)",
            "dropzone_a": "rgba(18,28,31,.78)",
            "dropzone_b": "rgba(18,28,31,.52)",
            "is_dark": "true",
        }

    return {
        "ink": "#20302d",
        "muted": "#687773",
        "panel": "rgba(255,255,255,.78)",
        "line": "rgba(32,48,45,.12)",
        "glass_border": "rgba(255,255,255,.76)",
        "glass_shadow": "0 22px 70px rgba(59,77,75,.13)",
        "soft_bg": "rgba(255,255,255,.62)",
        "day_bg": "rgba(255,255,255,.60)",
        "day_hover": "rgba(255,255,255,.86)",
        "assistant_bg": "rgba(255,255,255,.74)",
        "tile_a": "rgba(255,255,255,.58)",
        "tile_b": "rgba(255,255,255,.28)",
        "dropzone_a": "rgba(255,255,255,.72)",
        "dropzone_b": "rgba(255,255,255,.42)",
        "is_dark": "false",
    }


def apply_theme():
    accent = state["theme"]["accent"]
    background = state["theme"]["background"]
    accent_rgb = ", ".join(str(value) for value in hex_to_rgb(accent))
    values = background_theme_values(background)
    accent_word = readable_word_color(accent, background, "#0f766e", "#7ddfd2")
    danger_word = readable_word_color("#e11d48", background, "#be123c", "#fda4af")
    theme_vars = {
        "--accent": accent,
        "--accent-rgb": accent_rgb,
        "--mint": accent,
        "--app-bg": background,
        "--on-accent": readable_text_for(accent),
        "--chip-ink": values["ink"],
        "--accent-word": accent_word,
        "--danger-word": danger_word,
        "--ink": values["ink"],
        "--muted": values["muted"],
        "--panel": values["panel"],
        "--line": values["line"],
        "--glass-border": values["glass_border"],
        "--glass-shadow": values["glass_shadow"],
        "--soft-bg": values["soft_bg"],
        "--day-bg": values["day_bg"],
        "--day-hover": values["day_hover"],
        "--assistant-bg": values["assistant_bg"],
        "--tile-a": values["tile_a"],
        "--tile-b": values["tile_b"],
        "--dropzone-a": values["dropzone_a"],
        "--dropzone-b": values["dropzone_b"],
    }
    assignments = "\n".join(
        f"    document.documentElement.style.setProperty('{name}', {json.dumps(value)});"
        for name, value in theme_vars.items()
    )
    script = f"""
{assignments}
    document.body.classList.toggle('theme-dark', {values["is_dark"]});
    """
    ui.run_javascript(script)


def set_theme_color(kind, color):
    if kind not in state["theme"] or not color:
        return
    state["theme"][kind] = color
    apply_theme()


ui.add_head_html(
    """
    <style>
    :root {
      --ink: #20302d;
      --muted: #72817d;
      --panel: rgba(255,255,255,.78);
      --line: rgba(32,48,45,.12);
      --mint: #21b8a0;
      --accent: #21b8a0;
      --accent-rgb: 33, 184, 160;
      --app-bg: #f7faf6;
      --on-accent: #06201d;
      --chip-ink: #06201d;
      --accent-word: #0f766e;
      --danger-word: #be123c;
      --glass-border: rgba(255,255,255,.76);
      --glass-shadow: 0 22px 70px rgba(59,77,75,.13);
      --soft-bg: rgba(255,255,255,.62);
      --day-bg: rgba(255,255,255,.60);
      --day-hover: rgba(255,255,255,.86);
      --assistant-bg: rgba(255,255,255,.74);
      --tile-a: rgba(255,255,255,.58);
      --tile-b: rgba(255,255,255,.28);
      --dropzone-a: rgba(255,255,255,.72);
      --dropzone-b: rgba(255,255,255,.42);
    }

    body {
      color: var(--ink);
      background:
        radial-gradient(circle at 16% 8%, rgba(var(--accent-rgb),.20), transparent 28rem),
        radial-gradient(circle at 86% 18%, rgba(239,125,99,.16), transparent 24rem),
        linear-gradient(135deg, var(--app-bg) 0%, color-mix(in srgb, var(--app-bg), white 35%) 52%, color-mix(in srgb, var(--app-bg), var(--accent) 10%) 100%);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }

    .q-page { min-height: 100vh; }
    .q-page,
    .q-card,
    .q-tab-panel,
    .q-tab-panels,
    .q-dialog__inner {
      color: var(--ink);
    }

    .q-field__label,
    .q-field__marginal,
    .q-placeholder {
      color: var(--muted) !important;
    }

    .q-field__native,
    .q-field__input,
    .q-textarea textarea,
    .q-input input {
      color: var(--ink) !important;
    }

    .app-frame { max-width: 1440px; margin: 0 auto; padding: 28px; }

    .glass {
      background: var(--panel);
      border: 1px solid var(--glass-border);
      box-shadow: var(--glass-shadow);
      backdrop-filter: blur(18px);
    }

    .round-xl { border-radius: 34px; }
    .round-lg { border-radius: 26px; }
    .soft-button { border-radius: 999px; font-weight: 700; text-transform: none; }

    .bg-teal-600,
    .q-btn.bg-teal-600 {
      background: var(--accent) !important;
      color: var(--on-accent) !important;
    }

    .text-teal-600,
    .text-teal-700 {
      color: var(--accent-word) !important;
    }

    .text-rose-500,
    .text-rose-600 {
      color: var(--danger-word) !important;
    }

    .text-gray-500,
    .text-gray-600 {
      color: var(--muted) !important;
    }

    .bg-white\\/55,
    .bg-white\\/60,
    .bg-white\\/65 {
      background-color: var(--soft-bg) !important;
      color: var(--ink);
    }

    .theme-word {
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      text-transform: none;
    }

    .calendar-grid {
      display: grid;
      grid-template-columns: repeat(7, minmax(105px, 1fr));
      gap: 14px;
    }

    .weekday {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      text-align: center;
      text-transform: uppercase;
    }

    .day-card {
      min-height: 126px;
      border-radius: 28px;
      border: 1px solid var(--line);
      background: var(--day-bg);
      padding: 12px;
      transition: transform .18s ease, box-shadow .18s ease, background .18s ease;
    }

    .day-card:hover {
      background: var(--day-hover);
      box-shadow: 0 14px 34px rgba(32, 48, 45, .11);
      transform: translateY(-2px);
    }

    .muted-day { opacity: .42; }

    .today-card {
      border-color: rgba(var(--accent-rgb),.50);
      box-shadow: inset 0 0 0 2px rgba(var(--accent-rgb),.18);
    }

    .day-number {
      width: 34px;
      height: 34px;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-weight: 900;
      background: rgba(var(--accent-rgb),.12);
      color: var(--ink);
    }

    .event-chip {
      border-radius: 999px;
      background: rgba(var(--accent-rgb),.12);
      border: 1px solid rgba(var(--accent-rgb),.16);
      color: var(--chip-ink);
      padding: 5px 9px;
      font-size: 12px;
      line-height: 1.25;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
      max-width: 100%;
    }

    .event-chip .q-btn__content {
      display: flex;
      min-width: 0;
      max-width: 100%;
      width: 100%;
      align-items: center;
      justify-content: flex-start;
      gap: 6px;
      overflow: hidden;
      white-space: nowrap;
    }

    .event-time {
      color: color-mix(in srgb, var(--chip-ink), transparent 18%);
      flex: 0 0 auto;
      font-weight: 900;
      min-width: max-content;
      overflow: hidden;
      white-space: nowrap;
    }

    .event-title {
      color: var(--chip-ink);
      flex: 1 1 auto;
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .week-list {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
    }

    .week-day-card {
      min-height: 0;
    }

    .week-day-meta {
      color: var(--muted);
      font-size: 12px;
      font-weight: 800;
    }

    .theme-button {
      border-radius: 999px;
      color: var(--muted);
      font-weight: 900;
      text-transform: none;
    }

    body.body--dark .event-chip {
      background: rgba(var(--accent-rgb),.18);
      border-color: rgba(var(--accent-rgb),.28);
      color: var(--chip-ink);
    }

    .chat-scroll {
      max-height: 420px;
      min-height: 180px;
      overflow-y: auto;
      overscroll-behavior: contain;
      padding: 4px 8px 4px 2px;
      scrollbar-color: rgba(var(--accent-rgb),.55) transparent;
      scrollbar-width: thin;
    }

    .chat-scroll::-webkit-scrollbar {
      width: 8px;
    }

    .chat-scroll::-webkit-scrollbar-track {
      background: transparent;
    }

    .chat-scroll::-webkit-scrollbar-thumb {
      background: rgba(var(--accent-rgb),.45);
      border-radius: 999px;
    }

    .message-user, .message-assistant {
      border-radius: 24px;
      padding: 12px 14px;
      max-width: 88%;
      white-space: pre-wrap;
    }

    .message-user {
      background: var(--accent);
      color: var(--on-accent);
      margin-left: auto;
    }

    .message-assistant {
      background: var(--assistant-bg);
      border: 1px solid var(--line);
    }

    .textbook-dropzone {
      border: 1.5px dashed rgba(var(--accent-rgb),.46);
      border-radius: 28px;
      background:
        linear-gradient(135deg, var(--dropzone-a), var(--dropzone-b)),
        radial-gradient(circle at top right, rgba(var(--accent-rgb),.18), transparent 16rem);
      padding: 18px;
    }

    .textbook-icon {
      width: 48px;
      height: 48px;
      border-radius: 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      background: rgba(var(--accent-rgb),.14);
      color: var(--accent-word);
    }

    .textbook-chip {
      border-radius: 999px;
      border: 1px solid rgba(var(--accent-rgb),.20);
      background: rgba(var(--accent-rgb),.10);
      color: var(--accent-word);
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 800;
    }

    .textbook-uploader {
      border-radius: 22px;
      overflow: hidden;
      box-shadow: 0 12px 28px rgba(32,48,45,.08);
    }

    .lux-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 12px;
    }

    .lux-tile {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(145deg, var(--tile-a), var(--tile-b));
      padding: 13px;
      position: relative;
      overflow: hidden;
      transition: transform .22s ease, border-color .22s ease, background .22s ease;
    }

    .lux-tile::after {
      content: "";
      position: absolute;
      inset: auto -20% -55% 35%;
      height: 80%;
      background: linear-gradient(115deg, rgba(255,255,255,0), rgba(var(--accent-rgb),.14));
      transform: rotate(-8deg);
      pointer-events: none;
    }

    .lux-tile:hover {
      transform: translateY(-2px);
      border-color: rgba(var(--accent-rgb),.28);
    }

    .stat-icon {
      width: 36px;
      height: 36px;
      border-radius: 12px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--accent-word);
      background: rgba(var(--accent-rgb),.14);
    }

    .todo-done {
      color: var(--muted);
      text-decoration: line-through;
    }

    .todo-task-row {
      color: var(--ink);
      cursor: grab;
      user-select: none;
      border: 1px solid rgba(var(--accent-rgb),.18);
      min-height: 50px;
      transition: background .18s ease, border-color .18s ease, box-shadow .18s ease, transform .18s ease;
    }

    .todo-task-row:hover {
      border-color: rgba(var(--accent-rgb),.34);
      box-shadow: 0 8px 22px rgba(17, 24, 39, .06);
    }

    .todo-task-row:active {
      cursor: grabbing;
      transform: scale(.99);
    }

    .todo-task-label {
      color: var(--ink);
      min-width: 0;
      overflow-wrap: anywhere;
      white-space: normal;
      line-height: 1.25;
    }

    .todo-action-button {
      flex: 0 0 auto;
    }

    .todo-day-shell {
      display: grid;
      grid-template-columns: 92px minmax(0, 1fr);
      gap: 10px;
      max-height: 620px;
      overflow-y: auto;
      padding-right: 4px;
      scrollbar-color: rgba(var(--accent-rgb),.50) transparent;
      scrollbar-width: thin;
    }

    .time-label {
      color: var(--muted);
      font-size: 12px;
      font-weight: 900;
      padding-top: 14px;
      text-align: right;
      white-space: nowrap;
    }

    .time-slot {
      min-height: 72px;
      border: 1px dashed var(--button-border);
      border-radius: 18px;
      background: color-mix(in srgb, var(--soft-bg), transparent 18%);
      padding: 10px;
      transition: background .18s ease, border-color .18s ease, box-shadow .18s ease;
    }

    .time-slot:hover,
    .time-slot.drag-over {
      background: var(--button-bg);
      border-color: var(--accent-word);
      box-shadow: inset 0 0 0 1px rgba(var(--accent-rgb), .18);
    }

    .slot-empty {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
    }

    .slot-event-pill {
      border-radius: 999px;
      border: 1px solid rgba(var(--accent-rgb),.18);
      background: rgba(var(--accent-rgb),.12);
      color: var(--ink);
      padding: 5px 9px;
      font-size: 12px;
      font-weight: 800;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .slot-event-pill .q-btn__content {
      display: flex;
      min-width: 0;
      max-width: 100%;
      width: 100%;
      align-items: center;
      justify-content: flex-start;
      gap: 6px;
      overflow: hidden;
      white-space: nowrap;
    }

    @media (max-width: 920px) {
      .app-frame { padding: 16px; }
      .calendar-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .lux-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .weekday { display: none; }
      .day-card { min-height: 112px; }
      .todo-day-shell { grid-template-columns: 68px minmax(0, 1fr); }
    }
    </style>
    """
)


def stat_tile(icon, label, value, detail=None):
    with ui.element("div").classes("lux-tile"):
        with ui.row().classes("items-center gap-3"):
            with ui.element("div").classes("stat-icon"):
                ui.icon(icon)
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(str(value)).classes("text-xl font-black truncate")
                ui.label(label).classes("text-xs text-gray-500")
        if detail:
            ui.label(detail).classes("text-xs text-gray-500 mt-2")


@ui.refreshable
def planner_metrics_panel():
    today_key = date.today().isoformat()
    week_events = events_between(date.today(), date.today() + timedelta(days=7))
    calendars = {event.get("calendar", "NexaPlanner") for event in state["events"]}

    with ui.element("div").classes("lux-grid w-full"):
        stat_tile("today", "Today", f"{len(events_for_date(today_key))} events", f"{planned_minutes(today_key)} min planned")
        stat_tile("calendar_view_week", "Next 7 days", len(week_events), "rolling horizon")
        stat_tile("schedule", "Day view", f"{planned_minutes(state['todo_day'])} min", todo_day_label())
        stat_tile("layers", "Calendars", len(calendars), "active sources")


@ui.refreshable
def today_lens_panel():
    next_event = next_upcoming_event()
    today_key = date.today().isoformat()
    todays_events = events_for_date(today_key)
    load = min(planned_minutes(today_key) / 480, 1)

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-3"):
            ui.icon("flare").classes("text-teal-700 text-2xl")
            with ui.column().classes("gap-0"):
                ui.label("Today lens").classes("text-2xl font-black")
                ui.label(date.today().strftime("%A, %B %d")).classes("text-sm text-gray-500")

        planner_metrics_panel()
        ui.linear_progress(value=load, show_value=False, color="teal").classes("w-full rounded-full")

        if next_event:
            start, end = format_time_windows(next_event)
            ui.label(f"Next: {next_event['title']} at {start}").classes("font-bold")
            ui.label(f"{next_event['date']} - {start} to {end}").classes("text-xs text-gray-500")
        elif todays_events:
            ui.label("Today is planned, with no future events left in view.").classes("text-sm text-gray-500")
        else:
            ui.label("Open day. A clean canvas is its own luxury.").classes("text-sm text-gray-500")


@ui.refreshable
def agenda_panel():
    with ui.column().classes("w-full gap-3"):
        ui.label("Upcoming").classes("text-lg font-black")
        items = upcoming_events()

        if not items:
            ui.label("No events yet. Add one from a day bubble or ask the AI planner.").classes(
                "text-sm text-gray-500"
            )

        for item in items:
            start, end = format_time_windows(item)
            with ui.row().classes("w-full items-center gap-3 rounded-full bg-white/60 px-3 py-2"):
                ui.icon("event").classes("text-teal-600")
                with ui.column().classes("gap-0 min-w-0"):
                    ui.button(item["title"], on_click=lambda event=item: open_event_details(event)).props(
                        "flat dense no-caps"
                    ).classes("font-bold truncate px-0")
                    pretty = datetime.strptime(item["date"], "%Y-%m-%d").strftime("%a, %b %d")
                    ui.label(f"{pretty} - {start} - {end} - {item.get('calendar', 'NexaPlanner')}").classes(
                        "text-xs text-gray-500"
                    )


@ui.refreshable
def calendar_panel():
    month = state["month"]["month"]
    year = state["month"]["year"]
    weeks = calendar_module.Calendar(firstweekday=6).monthdatescalendar(year, month)
    today = date.today().isoformat()
    labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]

    with ui.element("div").classes("calendar-grid w-full"):
        for label in labels:
            ui.label(label).classes("weekday")

        for week in weeks:
            for day in week:
                day_key = day.isoformat()
                classes = "day-card"

                if day.month != month:
                    classes += " muted-day"
                if day_key == today:
                    classes += " today-card"

                with ui.element("div").classes(classes):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label(str(day.day)).classes("day-number")
                        ui.button(icon="add", on_click=lambda d=day_key: open_event_dialog(d)).props(
                            "flat round dense"
                        ).classes("text-teal-700")

                    day_events = events_for_date(day_key)
                    with ui.column().classes("w-full gap-1 mt-2"):
                        for item in day_events[:3]:
                            start, _ = format_time_windows(item)
                            with ui.button(
                                on_click=lambda event=item: open_event_details(event),
                            ).props("flat dense no-caps").classes("event-chip w-full justify-start"):
                                ui.label(start).classes("event-time")
                                ui.label(item["title"]).classes("event-title")

                        if len(day_events) > 3:
                            ui.label(f"+{len(day_events) - 3} more").classes("text-xs text-gray-500 px-2")


@ui.refreshable
def week_calendar_panel():
    today = date.today().isoformat()

    with ui.element("div").classes("week-list w-full"):
        for day in week_days():
            day_key = day.isoformat()
            classes = "day-card week-day-card"
            if day_key == today:
                classes += " today-card"

            with ui.element("div").classes(classes):
                with ui.row().classes("w-full items-center justify-between gap-3"):
                    with ui.row().classes("items-center gap-3 min-w-0"):
                        ui.label(str(day.day)).classes("day-number")
                        with ui.column().classes("gap-0 min-w-0"):
                            ui.label(day.strftime("%A")).classes("font-black")
                            ui.label(day.strftime("%B %d")).classes("week-day-meta")
                    ui.button(icon="add", on_click=lambda d=day_key: open_event_dialog(d)).props(
                        "flat round dense"
                    ).classes("text-teal-700")

                day_events = events_for_date(day_key)
                with ui.column().classes("w-full gap-1 mt-2"):
                    if not day_events:
                        ui.label("No events").classes("text-xs text-gray-500 px-2")
                    for item in day_events:
                        start, end = format_time_windows(item)
                        with ui.button(
                            on_click=lambda event=item: open_event_details(event),
                        ).props("flat dense no-caps").classes("event-chip w-full justify-start"):
                            ui.label(f"{start} - {end}").classes("event-time")
                            ui.label(item["title"]).classes("event-title")


@ui.refreshable
def chat_panel():
    with ui.column().classes("chat-scroll w-full gap-3"):
        for message in state["chat"]:
            klass = "message-user" if message["role"] == "user" else "message-assistant"
            ui.label(message["content"]).classes(klass)


@ui.refreshable
def study_panel():
    with ui.column().classes("chat-scroll w-full gap-3"):
        for message in state["study"]["chat"]:
            klass = "message-user" if message["role"] == "user" else "message-assistant"
            ui.label(message["content"]).classes(klass)


@ui.refreshable
def textbook_panel():
    name = state["study"]["textbook_name"]
    has_textbook = bool(name)

    with ui.column().classes("w-full gap-3"):
        with ui.element("div").classes("textbook-dropzone w-full"):
            with ui.row().classes("w-full items-center gap-4"):
                with ui.element("div").classes("textbook-icon shrink-0"):
                    ui.icon("menu_book" if has_textbook else "cloud_upload").classes("text-3xl")

                with ui.column().classes("gap-1 grow min-w-0"):
                    ui.label(name or "Import a textbook").classes("text-lg font-black truncate")
                    ui.label(
                        "Upload a PDF, DOCX, TXT, MD, or CSV file so NexaStudy can quiz you from it."
                    ).classes("text-sm text-gray-600 leading-snug")

            with ui.row().classes("w-full items-center gap-2 mt-4"):
                status = "Ready for textbook-based questions" if has_textbook else "No textbook selected yet"
                ui.label(status).classes("textbook-chip")
                ui.label("Max 25 MB").classes("textbook-chip")

            ui.upload(
                label="Choose or drop textbook",
                on_upload=handle_textbook_upload,
                auto_upload=True,
                max_file_size=25_000_000,
            ).props("accept=.pdf,.txt,.md,.csv,.docx flat bordered").classes("textbook-uploader w-full mt-4")


@ui.refreshable
def google_panel():
    connected = state["google"]["connected"]

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-3 rounded-full bg-white/65 px-3 py-2"):
            ui.icon("check_circle" if connected else "link").classes(
                "text-teal-600" if connected else "text-gray-500"
            )
            with ui.column().classes("gap-0 grow"):
                ui.label(state["google"]["status"]).classes("font-bold")
                ui.label("New NexaPlanner events sync to your primary Google Calendar.").classes(
                    "text-xs text-gray-500"
                )

        with ui.row().classes("gap-2"):
            ui.button("Connect Google", icon="account_circle", on_click=connect_google_calendar).classes(
                "soft-button bg-teal-600 text-white"
            )
            ui.button("Refresh", icon="sync", on_click=load_google_events).props("outline").classes(
                "soft-button text-teal-700"
            )


def render_todo_items():
    container = globals().get("todo_list_container")
    if container is None:
        return

    container.clear()
    ensure_todo_ids()
    completed = sum(1 for item in state["todos"] if item["done"])
    remaining = len(state["todos"]) - completed

    with container:
        ui.label(f"{remaining} left - {completed} done").classes("text-sm text-gray-500")

        with ui.column().classes("w-full gap-2"):
            if not state["todos"]:
                ui.label("Your task list has space to breathe.").classes("text-sm text-gray-500")

            for item in state["todos"]:
                todo_id = item["id"]
                with ui.row().classes(
                    "todo-task-row w-full flex-nowrap items-center gap-3 rounded-xl bg-white/80 px-3 py-2"
                ) as task_row:
                    task_row.props("draggable=true")
                    task_row.on(
                        "dragstart",
                        js_handler=(
                            f"(event) => {{"
                            f"event.dataTransfer.setData('text/plain', '{todo_id}');"
                            "event.dataTransfer.effectAllowed = 'copy';"
                            "}}"
                        ),
                    )
                    ui.checkbox(
                        value=item["done"],
                        on_change=lambda e, task_id=todo_id: toggle_todo(task_id, e.value),
                    ).props("dense")
                    ui.icon("drag_indicator").classes("text-gray-500")
                    task_label_classes = "grow font-medium todo-task-label"
                    if item["done"]:
                        task_label_classes += " todo-done"
                    ui.label(item["task"]).classes(task_label_classes)
                    ui.button(
                        icon="edit",
                        on_click=lambda task_id=todo_id: open_todo_details(task_id),
                    ).props("flat round dense").classes("todo-action-button text-teal-700")
                    ui.button(
                        icon="delete",
                        on_click=lambda task_id=todo_id: remove_todo(task_id),
                    ).props("flat round dense").classes("todo-action-button text-rose-500")


def todo_panel():
    global todo_list_container
    todo_list_container = ui.column().classes("w-full gap-3 pt-2")
    render_todo_items()


@ui.refreshable
def todo_day_schedule_panel():
    global todo_day_heading
    day_key = state["todo_day"]

    with ui.column().classes("w-full gap-3"):
        with ui.row().classes("w-full items-center gap-2"):
            todo_day_heading = ui.label(todo_day_label()).classes("text-2xl font-black grow")
            ui.button(icon="chevron_left", on_click=lambda: shift_todo_day(-1)).props("flat round")
            ui.button("Today", on_click=go_todo_today).props("outline").classes("soft-button")
            ui.button(icon="chevron_right", on_click=lambda: shift_todo_day(1)).props("flat round")

        with ui.element("div").classes("todo-day-shell w-full"):
            for time_value in todo_day_slots():
                start = datetime.strptime(time_value, "%H:%M")
                label = start.strftime("%I:%M %p").lstrip("0")
                ui.label(label).classes("time-label")

                with ui.element("div").classes("time-slot") as slot:
                    slot.on(
                        "click",
                        lambda e, d=day_key, t=time_value: handle_slot_click(e, d, t),
                        args=["!!event.target.closest('.slot-event-pill')"],
                    )
                    slot.on(
                        "dragover",
                        js_handler=(
                            "(event) => {"
                            "event.preventDefault();"
                            "event.currentTarget.classList.add('drag-over');"
                            "}"
                        ),
                    )
                    slot.on(
                        "dragleave",
                        js_handler="(event) => event.currentTarget.classList.remove('drag-over')",
                    )
                    slot.on(
                        "drop",
                        lambda e, d=day_key, t=time_value: handle_task_drop(e, d, t),
                        args=["event.dataTransfer.getData('text/plain')"],
                        js_handler=(
                            "(event) => {"
                            "event.preventDefault();"
                            "event.currentTarget.classList.remove('drag-over');"
                            "}"
                        ),
                    )

                    slot_events = events_for_time_slot(day_key, time_value)
                    if not slot_events:
                        ui.label("Available").classes("slot-empty")
                    else:
                        with ui.column().classes("w-full gap-1"):
                            for item in slot_events:
                                start_text, end_text = format_time_windows(item)
                                with ui.button(
                                    on_click=lambda event=item: open_event_details(event),
                                ).props("flat dense no-caps").classes("slot-event-pill w-full justify-start"):
                                    ui.label(f"{start_text} - {end_text}").classes("event-time")
                                    ui.label(item["title"]).classes("event-title")


with ui.dialog() as event_dialog, ui.card().classes("glass round-lg w-[420px] max-w-[92vw] p-5 gap-4"):
    ui.label("Add task").classes("text-2xl font-black")

    event_title = ui.input("Task title", placeholder="Finish science worksheet").classes("w-full").props(
        "rounded outlined"
    )

    event_description = ui.textarea(
        "Description",
        placeholder="Location, notes, supplies, agenda, or anything else to remember",
    ).classes("w-full").props("rounded outlined autogrow")

    with ui.row().classes("w-full gap-3"):
        event_date = ui.input("Date", placeholder="YYYY-MM-DD").classes("grow").props("rounded outlined")
        event_time = ui.input("Time", placeholder="HH:MM").classes("grow").props("rounded outlined")

    event_duration = ui.number("Duration minutes", value=60, min=5, step=5).classes("w-full").props(
        "rounded outlined"
    )

    with ui.row().classes("w-full justify-end gap-2"):
        ui.button("Cancel", on_click=event_dialog.close).props("flat").classes("soft-button")
        ui.button("Add", icon="check", on_click=add_manual_event).classes(
            "soft-button bg-teal-600 text-white"
        )


with ui.dialog() as event_details_dialog, ui.card().classes("glass round-lg w-[480px] max-w-[92vw] p-5 gap-4"):
    ui.label("Event details").classes("text-2xl font-black")

    detail_title = ui.input("Title").classes("w-full").props("rounded outlined")
    detail_description = ui.textarea("Description").classes("w-full").props("rounded outlined autogrow")

    with ui.row().classes("w-full gap-3"):
        detail_date = ui.input("Date", placeholder="YYYY-MM-DD").classes("grow").props("rounded outlined")
        detail_time = ui.input("Time", placeholder="HH:MM").classes("grow").props("rounded outlined")

    detail_duration = ui.number("Duration minutes", value=60, min=5, step=5).classes("w-full").props(
        "rounded outlined"
    )

    with ui.row().classes("w-full justify-between gap-2"):
        ui.button("Delete", icon="delete", on_click=delete_selected_event).props("outline").classes(
            "soft-button text-rose-600"
        )
        with ui.row().classes("gap-2"):
            ui.button("Cancel", on_click=event_details_dialog.close).props("flat").classes("soft-button")
            ui.button("Save", icon="check", on_click=update_selected_event).classes(
                "soft-button bg-teal-600 text-white"
            )


with ui.dialog() as todo_details_dialog, ui.card().classes("glass round-lg w-[420px] max-w-[92vw] p-5 gap-4"):
    ui.label("Task details").classes("text-2xl font-black")

    todo_detail_task = ui.input("Task", placeholder="Finish science worksheet").classes("w-full").props(
        "rounded outlined"
    )
    todo_detail_done = ui.checkbox("Done").props("dense")

    with ui.row().classes("w-full justify-between gap-2"):
        ui.button("Delete", icon="delete", on_click=delete_selected_todo).props("outline").classes(
            "soft-button text-rose-600"
        )
        with ui.row().classes("gap-2"):
            ui.button("Cancel", on_click=todo_details_dialog.close).props("flat").classes("soft-button")
            ui.button("Save", icon="check", on_click=update_selected_todo).classes(
                "soft-button bg-teal-600 text-white"
            )


with ui.dialog() as accent_theme_dialog, ui.card().classes("glass round-lg w-[360px] max-w-[92vw] p-5 gap-4"):
    ui.label("Accent color").classes("text-2xl font-black")
    ui.color_input(
        "Accent",
        value=state["theme"]["accent"],
        on_change=lambda e: set_theme_color("accent", e.value),
        preview=True,
    ).classes("w-full").props("outlined")
    ui.button("Done", icon="check", on_click=accent_theme_dialog.close).classes(
        "soft-button bg-teal-600 text-white"
    )


with ui.dialog() as background_theme_dialog, ui.card().classes("glass round-lg w-[360px] max-w-[92vw] p-5 gap-4"):
    ui.label("Background color").classes("text-2xl font-black")
    ui.color_input(
        "Background",
        value=state["theme"]["background"],
        on_change=lambda e: set_theme_color("background", e.value),
        preview=True,
    ).classes("w-full").props("outlined")
    ui.button("Done", icon="check", on_click=background_theme_dialog.close).classes(
        "soft-button bg-teal-600 text-white"
    )


with ui.element("main").classes("app-frame"):
    with ui.column().classes("w-full gap-6"):
        with ui.row().classes("w-full items-center justify-between gap-4"):
            with ui.column().classes("gap-1"):
                ui.label("NexaPlanner").classes("text-5xl font-black")
                ui.label("A calmer planning space for chat, tasks, and studying.").classes(
                    "text-base text-gray-600"
                )

            with ui.row().classes("items-center gap-3"):
                with ui.row().classes("items-center gap-1 rounded-full bg-white/55 px-2 py-2"):
                    ui.button("Accent", on_click=accent_theme_dialog.open).props(
                        "flat dense no-caps"
                    ).classes("theme-button")
                    ui.button("Background", on_click=background_theme_dialog.open).props(
                        "flat dense no-caps"
                    ).classes("theme-button")

                ui.button("New event", icon="add", on_click=open_event_dialog).classes(
                    "soft-button bg-teal-600 text-white px-5 py-3"
                )

        with ui.tabs().classes("w-full rounded-full bg-white/55 p-1") as tabs:
            ai_tab = ui.tab("AI Planner", icon="auto_awesome").classes("soft-button")
            study_tab = ui.tab("Study Coach", icon="school").classes("soft-button")
            calendar_tab = ui.tab("Calendar", icon="calendar_month").classes("soft-button")
            todo_tab = ui.tab("Day View", icon="view_day").classes("soft-button")

        with ui.tab_panels(tabs, value=ai_tab).classes("w-full bg-transparent"):
            with ui.tab_panel(ai_tab).classes("p-0"):
                with ui.grid(columns=2).classes("w-full gap-6 max-[980px]:grid-cols-1"):
                    with ui.card().classes("glass round-xl p-5 gap-4"):
                        ui.label("AI scheduling").classes("text-2xl "
                                                           "font-black")
                        chat_panel()

                        chat_input = ui.input(
                            "Ask NexaPlanner",
                            placeholder="Schedule design review tomorrow at 2pm for 45 minutes",
                            on_change=None,
                        ).classes("w-full").props("rounded outlined")

                        chat_input.on("keydown.enter", lambda _: send_chat())

                        with ui.row().classes("gap-2"):
                            ui.button("Send", icon="send", on_click=send_chat).classes(
                                "soft-button bg-teal-600 text-white"
                            )

                            for example in [
                                "Schedule robotics practice tomorrow at 4pm for 2 hours",
                                "Add math homework next Friday at 6:30pm",
                            ]:
                                ui.button(example, on_click=lambda e=example: chat_input.set_value(e)).props(
                                    "outline"
                                ).classes("soft-button text-teal-700")

                    with ui.column().classes("gap-6"):
                        with ui.card().classes("glass round-xl p-5 gap-4"):
                            today_lens_panel()

                        with ui.card().classes("glass round-xl p-5 gap-4"):
                            ui.label("This week").classes("text-2xl font-black")

                            with ui.row().classes("items-center gap-2"):
                                ai_week_heading = ui.label(week_label()).classes(
                                    "text-xl font-black grow"
                                )
                                ui.button(icon="chevron_left", on_click=lambda: shift_week(-1)).props("flat round")
                                ui.button("Today", on_click=go_today).props("outline").classes("soft-button")
                                ui.button(icon="chevron_right", on_click=lambda: shift_week(1)).props("flat round")

                            week_calendar_panel()

                        with ui.card().classes("glass round-xl p-5 gap-4"):
                            agenda_panel()

            with ui.tab_panel(study_tab).classes("p-0"):
                with ui.grid(columns=2).classes("w-full gap-6 max-[980px]:grid-cols-1"):
                    with ui.card().classes("glass round-xl p-5 gap-4"):
                        ui.label("AI study coach").classes("text-2xl font-black")

                        study_subject = ui.input(
                            "Subject",
                            placeholder="Biology, Algebra 2, World History...",
                            value=state["study"]["subject"],
                        ).classes("w-full").props("rounded outlined")

                        study_panel()

                        study_answer = ui.textarea(
                            "Your answer",
                            placeholder="Answer here, then NexaStudy will check it and ask another.",
                        ).classes("w-full").props("rounded outlined autogrow")

                        study_answer.on("keydown.ctrl.enter", lambda _: send_study_answer())

                        with ui.row().classes("gap-2"):
                            ui.button("Ask me", icon="quiz", on_click=ask_study_question).classes(
                                "soft-button bg-teal-600 text-white"
                            )
                            ui.button("Submit answer", icon="send", on_click=send_study_answer).props(
                                "outline"
                            ).classes("soft-button text-teal-700")
                            ui.button("Clear", icon="restart_alt", on_click=clear_study_session).props(
                                "outline"
                            ).classes("soft-button text-rose-600")

                    with ui.card().classes("glass round-xl p-5 gap-4"):
                        ui.label("How it studies").classes("text-2xl font-black")
                        ui.label(
                            "Set a subject, then the coach will keep questioning you one at a time."
                        ).classes("text-gray-600 leading-relaxed")

            with ui.tab_panel(calendar_tab).classes("p-0"):
                with ui.column().classes("w-full gap-6"):
                    with ui.card().classes("glass round-xl p-5 gap-4"):
                        with ui.row().classes("w-full items-center gap-2"):
                            calendar_month_heading = ui.label(month_label(state["month"])).classes(
                                "text-3xl font-black grow"
                            )
                            ui.button(icon="chevron_left", on_click=lambda: shift_month(-1)).props("flat round")
                            ui.button("Today", on_click=go_today).props("outline").classes("soft-button")
                            ui.button(icon="chevron_right", on_click=lambda: shift_month(1)).props("flat round")

                        calendar_panel()

                    with ui.card().classes("glass round-xl p-5 gap-4"):
                        agenda_panel()

            with ui.tab_panel(todo_tab).classes("p-0"):
                with ui.card().classes("glass round-xl p-5 gap-4"):
                    todo_day_schedule_panel()

# To turn voice commands back on, install pyttsx3 and SpeechRecognition, then uncomment:
# import threading
# threading.Thread(target=keyword_recog, daemon=True).start()

ui.run(title="NexaPlanner NiceGUI", port=8080, reload=False)
