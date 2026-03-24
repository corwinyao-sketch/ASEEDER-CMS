import html
import json
import os
from base64 import b64encode
from datetime import datetime, time, timedelta
from uuid import uuid4

import pytz
import requests
import streamlit as st

CONFIG_PATH = "accounts.json"
MEETINGS_PATH = "meetings.json"
LOCKS_PATH = "locks.json"
REGIONS = ["华东", "华北", "华西", "中西"]
NAV_ITEMS = REGIONS + ["管理员"]
ADMIN_PASSWORD = "zoom-admin-2026"
SHANGHAI_TZ = pytz.timezone("Asia/Shanghai")


st.set_page_config(page_title="Zoom 多地区会议预约", page_icon="📅", layout="wide")


def load_json_file(path, default_value):
    if not os.path.exists(path):
        return default_value
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except Exception as exc:
        st.error(f"读取 {path} 失败: {exc}")
        return default_value


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def infer_region_from_name(account_name):
    for region in REGIONS:
        if str(account_name).startswith(region):
            return region
    return ""


def normalize_accounts(raw_accounts):
    if not isinstance(raw_accounts, dict):
        return {}, False

    normalized = {}
    changed = False

    for account_name, config in raw_accounts.items():
        if not isinstance(config, dict):
            changed = True
            continue

        region = config.get("region") or infer_region_from_name(account_name)
        if region not in REGIONS:
            region = ""

        normalized_config = dict(config)
        normalized_config["region"] = region
        normalized_config["account_id"] = config.get("account_id", "")
        normalized_config["client_id"] = config.get("client_id", "")
        normalized_config["client_secret"] = config.get("client_secret", "")
        normalized_config["user_email"] = config.get("user_email", "")

        if normalized_config != config:
            changed = True

        normalized[account_name] = normalized_config

    return normalized, changed


def load_accounts():
    raw_accounts = load_json_file(CONFIG_PATH, {})
    accounts, changed = normalize_accounts(raw_accounts)
    if changed:
        save_accounts(accounts)
    return accounts


def save_accounts(accounts):
    save_json_file(CONFIG_PATH, accounts)


def normalize_meetings(raw_meetings):
    if not isinstance(raw_meetings, dict):
        return {}, False

    normalized = {}
    changed = False

    for account_name, records in raw_meetings.items():
        if not isinstance(records, list):
            changed = True
            continue

        normalized_records = []
        for record in records:
            if not isinstance(record, dict):
                changed = True
                continue

            normalized_record = dict(record)
            if "region" not in normalized_record:
                inferred_region = infer_region_from_name(account_name)
                if inferred_region:
                    normalized_record["region"] = inferred_region
                    changed = True
            normalized_records.append(normalized_record)

        normalized[account_name] = normalized_records

    return normalized, changed


def load_meetings():
    raw_meetings = load_json_file(MEETINGS_PATH, {})
    meetings, changed = normalize_meetings(raw_meetings)
    if changed:
        save_meetings(meetings)
    return meetings


def save_meetings(meetings):
    save_json_file(MEETINGS_PATH, meetings)


def normalize_locks(raw_locks, accounts):
    if not isinstance(raw_locks, list):
        return [], False

    normalized = []
    changed = False

    for lock in raw_locks:
        if not isinstance(lock, dict):
            changed = True
            continue

        if not lock.get("start_time") or not lock.get("end_time"):
            changed = True
            continue

        normalized_lock = dict(lock)
        if not normalized_lock.get("id"):
            normalized_lock["id"] = f"lock_{uuid4().hex[:8]}"
            changed = True

        account_name = normalized_lock.get("account_name", "")
        account_region = accounts.get(account_name, {}).get("region", "")
        inferred_region = account_region or normalized_lock.get("region") or infer_region_from_name(account_name)
        if inferred_region != normalized_lock.get("region", ""):
            normalized_lock["region"] = inferred_region
            changed = True

        normalized_lock["reason"] = normalized_lock.get("reason", "")
        normalized.append(normalized_lock)

    return normalized, changed


def load_locks(accounts):
    raw_locks = load_json_file(LOCKS_PATH, [])
    locks, changed = normalize_locks(raw_locks, accounts)
    if changed:
        save_locks(locks)
    return locks


def save_locks(locks):
    save_json_file(LOCKS_PATH, locks)


def get_zoom_user_id(account_config):
    return account_config.get("user_email") or "me"


def account_is_ready(account_config):
    return (
        account_config.get("region") in REGIONS
        and bool(account_config.get("account_id"))
        and bool(account_config.get("client_id"))
        and bool(account_config.get("client_secret"))
    )


def get_accounts_for_region(accounts, region, ready_only=True):
    filtered = {}
    for account_name, config in accounts.items():
        if config.get("region") != region:
            continue
        if ready_only and not account_is_ready(config):
            continue
        filtered[account_name] = config
    return dict(sorted(filtered.items()))


def get_incomplete_accounts(accounts):
    return {
        account_name: config
        for account_name, config in sorted(accounts.items())
        if not account_is_ready(config)
    }


def localize_datetime(date_obj, time_obj):
    naive_dt = datetime.combine(date_obj, time_obj)
    return SHANGHAI_TZ.localize(naive_dt)


def serialize_local_datetime(dt):
    return dt.astimezone(SHANGHAI_TZ).isoformat()


def parse_iso_datetime(dt_str):
    parsed = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = SHANGHAI_TZ.localize(parsed)
    return parsed.astimezone(SHANGHAI_TZ)


def ranges_overlap(start_a, end_a, start_b, end_b):
    return start_a < end_b and start_b < end_a


def calculate_duration(start_time, end_time):
    start_minutes = start_time.hour * 60 + start_time.minute
    end_minutes = end_time.hour * 60 + end_time.minute
    if end_minutes <= start_minutes:
        end_minutes += 24 * 60
    return end_minutes - start_minutes


def get_booking_window(meeting_date, start_time, end_time):
    start_dt = localize_datetime(meeting_date, start_time)
    end_dt = localize_datetime(meeting_date, end_time)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def get_day_bounds(target_date):
    day_start = localize_datetime(target_date, time(0, 0))
    return day_start, day_start + timedelta(days=1)


def get_access_token(account_id, client_id, client_secret):
    url = "https://zoom.us/oauth/token"
    credentials = b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {credentials}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "grant_type": "account_credentials",
        "account_id": account_id,
    }
    response = requests.post(url, headers=headers, data=data, timeout=30)
    response.raise_for_status()
    return response.json()["access_token"]


def create_meeting(access_token, topic, start_time_utc, duration_minutes, timezone="Asia/Shanghai", user_id="me"):
    url = f"https://api.zoom.us/v2/users/{user_id}/meetings"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "topic": topic,
        "type": 2,
        "start_time": start_time_utc,
        "duration": duration_minutes,
        "timezone": timezone,
        "settings": {
            "join_before_host": True,
            "waiting_room": False,
        },
    }
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


def get_scheduled_meetings(access_token, user_id="me"):
    meetings = []
    next_page_token = ""

    while True:
        params = {
            "type": "scheduled",
            "page_size": 100,
        }
        if next_page_token:
            params["next_page_token"] = next_page_token

        response = requests.get(
            f"https://api.zoom.us/v2/users/{user_id}/meetings",
            headers={"Authorization": f"Bearer {access_token}"},
            params=params,
            timeout=30,
        )
        response.raise_for_status()

        payload = response.json()
        meetings.extend(payload.get("meetings", []))
        next_page_token = payload.get("next_page_token", "")

        if not next_page_token:
            break

    return meetings


def get_meeting_recordings(access_token, meeting_id):
    response = requests.get(
        f"https://api.zoom.us/v2/meetings/{meeting_id}/recordings",
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=30,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return response.json()


def format_meeting_id(meeting_id):
    mid = str(meeting_id)
    if len(mid) == 11:
        return f"{mid[:3]} {mid[3:7]} {mid[7:]}"
    if len(mid) == 10:
        return f"{mid[:3]} {mid[3:6]} {mid[6:]}"
    return mid


def local_to_utc(date_obj, time_obj, local_tz="Asia/Shanghai"):
    local = pytz.timezone(local_tz)
    local_dt = datetime.combine(date_obj, time_obj)
    local_dt = local.localize(local_dt)
    utc_dt = local_dt.astimezone(pytz.UTC)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def format_display_time(date_obj, time_obj):
    dt = datetime.combine(date_obj, time_obj)
    return dt.strftime("%B %d, %Y"), dt.strftime("%I:%M %p")


def format_range(start_dt, end_dt):
    if start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%H:%M')}"
    return f"{start_dt.strftime('%Y-%m-%d %H:%M')} - {end_dt.strftime('%Y-%m-%d %H:%M')}"


def build_zoom_event(meeting):
    if not meeting.get("start_time"):
        return None

    start_dt = parse_iso_datetime(meeting["start_time"])
    duration_minutes = meeting.get("duration") or 60
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    topic = meeting.get("topic") or "无主题"
    return {
        "id": str(meeting.get("id", uuid4().hex)),
        "source": "meeting",
        "title": topic,
        "display": topic,
        "start_dt": start_dt,
        "end_dt": end_dt,
        "meeting_id": meeting.get("id", ""),
        "join_url": meeting.get("join_url", ""),
        "password": meeting.get("password", ""),
    }


def build_lock_event(lock):
    start_dt = parse_iso_datetime(lock["start_time"])
    end_dt = parse_iso_datetime(lock["end_time"])
    reason = lock.get("reason") or "未填写原因"
    return {
        "id": lock["id"],
        "source": "lock",
        "title": f"管理员锁定: {reason}",
        "display": f"锁定: {reason}",
        "reason": reason,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


def sort_events(events):
    return sorted(events, key=lambda item: (item["start_dt"], item["end_dt"], item["source"], item["title"]))


def get_lock_events_for_account(account_name, locks):
    events = []
    for lock in locks:
        if lock.get("account_name") != account_name:
            continue
        try:
            events.append(build_lock_event(lock))
        except Exception:
            continue
    return sort_events(events)


def fetch_zoom_events_for_account(account_name, account_config):
    try:
        access_token = get_access_token(
            account_config["account_id"],
            account_config["client_id"],
            account_config["client_secret"],
        )
        meetings = get_scheduled_meetings(access_token, get_zoom_user_id(account_config))
        events = []
        for meeting in meetings:
            event = build_zoom_event(meeting)
            if event:
                events.append(event)
        return sort_events(events), access_token, None
    except requests.exceptions.HTTPError as exc:
        response = exc.response
        message = f"{response.status_code} - {response.text}" if response is not None else str(exc)
        return [], None, message
    except requests.exceptions.RequestException as exc:
        return [], None, str(exc)
    except Exception as exc:
        return [], None, str(exc)


def fetch_account_schedule(account_name, account_config, locks):
    meeting_events, access_token, error = fetch_zoom_events_for_account(account_name, account_config)
    lock_events = get_lock_events_for_account(account_name, locks)
    return {
        "account": account_name,
        "access_token": access_token,
        "meeting_events": meeting_events,
        "lock_events": lock_events,
        "events": sort_events(meeting_events + lock_events),
        "error": error,
    }


def filter_events_for_date(events, target_date):
    day_start, day_end = get_day_bounds(target_date)
    return [event for event in events if ranges_overlap(event["start_dt"], event["end_dt"], day_start, day_end)]


def filter_events_for_window(events, start_dt, end_dt):
    return [event for event in events if ranges_overlap(event["start_dt"], event["end_dt"], start_dt, end_dt)]


def describe_conflict(event):
    time_range = format_range(event["start_dt"], event["end_dt"])
    if event["source"] == "lock":
        return f"管理员锁定: {event.get('reason', '未填写原因')} ({time_range})"
    return f"已有会议: {event['title']} ({time_range})"


def evaluate_region_availability(region_accounts, locks, meeting_date, start_time, end_time):
    target_start, target_end = get_booking_window(meeting_date, start_time, end_time)
    day_start, day_end = get_day_bounds(meeting_date)

    results = []
    for account_name, account_config in region_accounts.items():
        schedule = fetch_account_schedule(account_name, account_config, locks)
        day_meeting_count = len(
            [
                event
                for event in schedule["meeting_events"]
                if ranges_overlap(event["start_dt"], event["end_dt"], day_start, day_end)
            ]
        )

        if schedule["error"]:
            results.append(
                {
                    "account": account_name,
                    "status": "error",
                    "reason": f"拉取失败: {schedule['error']}",
                    "meeting_count": day_meeting_count,
                    "schedule": schedule,
                }
            )
            continue

        overlapping_events = filter_events_for_window(schedule["events"], target_start, target_end)
        if overlapping_events:
            conflict = sort_events(overlapping_events)[0]
            results.append(
                {
                    "account": account_name,
                    "status": "busy",
                    "reason": describe_conflict(conflict),
                    "meeting_count": day_meeting_count,
                    "schedule": schedule,
                }
            )
        else:
            results.append(
                {
                    "account": account_name,
                    "status": "available",
                    "reason": f"当天已有 {day_meeting_count} 场会议",
                    "meeting_count": day_meeting_count,
                    "schedule": schedule,
                }
            )

    available_accounts = [item for item in results if item["status"] == "available"]
    recommended = None
    if available_accounts:
        recommended = sorted(available_accounts, key=lambda item: (item["meeting_count"], item["account"]))[0]

    return {
        "results": results,
        "available": available_accounts,
        "recommended": recommended,
        "target_start": target_start,
        "target_end": target_end,
    }


def add_meeting_record(account_name, region, meeting_data, output_text):
    meetings = load_meetings()
    meetings.setdefault(account_name, [])
    meetings[account_name].append(
        {
            "meeting_id": meeting_data["id"],
            "topic": meeting_data["topic"],
            "start_time": meeting_data["start_time"],
            "join_url": meeting_data["join_url"],
            "passcode": meeting_data.get("password", ""),
            "output": output_text,
            "region": region,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    save_meetings(meetings)


def delete_meeting_record(account_name, meeting_id):
    meetings = load_meetings()
    if account_name not in meetings:
        return
    meetings[account_name] = [item for item in meetings[account_name] if str(item.get("meeting_id")) != str(meeting_id)]
    if not meetings[account_name]:
        meetings.pop(account_name, None)
    save_meetings(meetings)


def sync_meeting_records_for_account(old_name, new_name, new_region):
    meetings = load_meetings()
    changed = False

    if old_name != new_name and old_name in meetings:
        old_records = meetings.pop(old_name)
        meetings.setdefault(new_name, [])
        meetings[new_name].extend(old_records)
        changed = True

    for record in meetings.get(new_name, []):
        if record.get("region") != new_region:
            record["region"] = new_region
            changed = True

    if changed:
        save_meetings(meetings)


def sync_locks_for_account(old_name, new_name, new_region):
    locks = load_locks(load_accounts())
    changed = False

    for lock in locks:
        if lock.get("account_name") == old_name:
            if old_name != new_name:
                lock["account_name"] = new_name
            if lock.get("region") != new_region:
                lock["region"] = new_region
            changed = True
        elif old_name == new_name and lock.get("account_name") == new_name and lock.get("region") != new_region:
            lock["region"] = new_region
            changed = True

    if changed:
        save_locks(locks)


def delete_account_related_data(account_name):
    meetings = load_meetings()
    if account_name in meetings:
        meetings.pop(account_name, None)
        save_meetings(meetings)

    accounts = load_accounts()
    locks = [lock for lock in load_locks(accounts) if lock.get("account_name") != account_name]
    save_locks(locks)


def format_booking_output(topic, meeting, meeting_date, start_time):
    date_str, time_str = format_display_time(meeting_date, start_time)
    meeting_id_formatted = format_meeting_id(meeting["id"])
    password = meeting.get("password", "N/A")
    join_url = meeting.get("join_url", "")
    uuid = meeting.get("uuid", "")

    return f"""Subject: {topic}
Time: {date_str}, {time_str} (Beijing/Shanghai Time)
Join Zoom Meeting
{join_url}

View Meeting Insights with Zoom AI Companion
https://zoom.us/launch/edl?muid={uuid}

Meeting ID: {meeting_id_formatted}
Passcode: {password}"""


def render_availability_results(availability):
    available_accounts = availability["available"]
    recommended = availability["recommended"]

    if recommended:
        st.success(
            f"当前时段可预约账号 {len(available_accounts)} 个，推荐账号："
            f"**{recommended['account']}**（当天 {recommended['meeting_count']} 场会议）"
        )
    else:
        st.error("当前时段没有可预约账号")

    if available_accounts:
        st.markdown("### 可用账号")
        for item in available_accounts:
            st.write(f"• {item['account']}：{item['reason']}")

    unavailable_accounts = [item for item in availability["results"] if item["status"] != "available"]
    if unavailable_accounts:
        with st.expander("查看不可用原因", expanded=False):
            for item in unavailable_accounts:
                prefix = "⚠️" if item["status"] == "error" else "❌"
                st.write(f"{prefix} {item['account']}：{item['reason']}")


def get_hour_event(events, target_date, hour):
    hour_start = localize_datetime(target_date, time(hour, 0))
    hour_end = hour_start + timedelta(hours=1)
    for event in events:
        if ranges_overlap(event["start_dt"], event["end_dt"], hour_start, hour_end):
            return event
    return None


def render_schedule_table(schedule_map, selected_accounts, target_date):
    html_content = """
    <style>
    .schedule-table { border-collapse: collapse; width: 100%; font-size: 12px; }
    .schedule-table th, .schedule-table td { border: 1px solid #ddd; padding: 4px; text-align: center; min-width: 30px; }
    .schedule-table th { background-color: #f5f5f5; font-weight: bold; }
    .schedule-table .account-name { text-align: left; font-weight: bold; min-width: 120px; }
    .available { background-color: #d7f5d7; }
    .occupied { background-color: #ff9f9f; cursor: pointer; }
    .locked { background-color: #ffd88a; cursor: pointer; }
    .tooltip { position: relative; display: inline-block; width: 100%; height: 100%; }
    .tooltip .tooltiptext {
        visibility: hidden;
        width: 220px;
        background-color: #333;
        color: #fff;
        text-align: left;
        border-radius: 6px;
        padding: 8px;
        position: absolute;
        z-index: 1;
        bottom: 125%;
        left: 50%;
        margin-left: -110px;
        font-size: 12px;
    }
    .tooltip:hover .tooltiptext { visibility: visible; }
    </style>
    <table class="schedule-table">
    <tr><th class="account-name">账号</th>
    """

    for hour in range(24):
        html_content += f"<th>{hour:02d}</th>"
    html_content += "</tr>"

    for account_name in selected_accounts:
        schedule = schedule_map.get(account_name, {})
        day_events = filter_events_for_date(schedule.get("events", []), target_date)
        html_content += f"<tr><td class='account-name'>{html.escape(account_name)}</td>"

        hour = 0
        while hour < 24:
            event = get_hour_event(day_events, target_date, hour)
            if event:
                span = 1
                for next_hour in range(hour + 1, 24):
                    next_event = get_hour_event(day_events, target_date, next_hour)
                    if next_event and next_event["id"] == event["id"] and next_event["source"] == event["source"]:
                        span += 1
                    else:
                        break

                cell_class = "locked" if event["source"] == "lock" else "occupied"
                tooltip = html.escape(
                    f"{'管理员锁定' if event['source'] == 'lock' else '会议'}\n"
                    f"{event['title']}\n{format_range(event['start_dt'], event['end_dt'])}"
                ).replace("\n", "<br>")
                cell_label = "锁定" if event["source"] == "lock" else "占用"
                html_content += (
                    f"<td class='{cell_class}' colspan='{span}'>"
                    f"<div class='tooltip'>{cell_label}<span class='tooltiptext'>{tooltip}</span></div>"
                    f"</td>"
                )
                hour += span
            else:
                html_content += "<td class='available'></td>"
                hour += 1

        html_content += "</tr>"

    html_content += "</table>"
    st.markdown(html_content, unsafe_allow_html=True)


def render_schedule_details(schedule_map, selected_accounts, target_date):
    st.markdown("---")
    st.subheader("📋 当日详情")

    for account_name in selected_accounts:
        schedule = schedule_map.get(account_name, {})
        day_events = filter_events_for_date(schedule.get("events", []), target_date)
        st.write(f"**{account_name}**")

        if schedule.get("error"):
            st.error(f"Zoom 数据拉取失败：{schedule['error']}")

        if not day_events:
            st.write("  • 当日无会议或锁定")
            continue

        for event in day_events:
            label = "管理员锁定" if event["source"] == "lock" else "会议"
            st.write(f"  • [{label}] {format_range(event['start_dt'], event['end_dt'])}：{event['title']}")


def fetch_region_api_records(region_accounts, query_start_date, query_end_date):
    api_meetings = []
    warnings = []

    for account_name, account_config in region_accounts.items():
        meeting_events, _, error = fetch_zoom_events_for_account(account_name, account_config)
        if error:
            warnings.append(f"获取 {account_name} 失败: {error}")
            continue

        for event in meeting_events:
            if query_start_date <= event["start_dt"].date() <= query_end_date:
                api_meetings.append(
                    {
                        "account": account_name,
                        "topic": event["title"],
                        "start_time": event["start_dt"].strftime("%Y-%m-%d %H:%M"),
                        "meeting_id": event["meeting_id"],
                        "join_url": event["join_url"],
                        "password": event["password"],
                    }
                )

    return api_meetings, warnings


def render_region_booking(region, region_accounts, locks):
    st.subheader("📝 预约会议")
    if not region_accounts:
        st.warning("当前地区没有可用账号，请先到管理员页添加并配置账号。")
        return

    st.caption(f"当前地区：**{region}**，可调度账号池：**{len(region_accounts)}** 个")

    topic = st.text_input("会议主题", placeholder="例如：SA meeting 4:Kelly——Qingqing", key=f"{region}_topic")
    meeting_date = st.date_input("会议日期", value=datetime.now().date(), key=f"{region}_meeting_date")

    col1, col2 = st.columns(2)
    with col1:
        start_time = st.time_input("开始时间", value=time(19, 0), key=f"{region}_start_time")
    with col2:
        end_time = st.time_input("结束时间", value=time(20, 0), key=f"{region}_end_time")

    duration = calculate_duration(start_time, end_time)
    st.caption(f"会议时长：{duration} 分钟")

    if st.button("检查可用账号", key=f"{region}_check_accounts"):
        with st.spinner("正在检查当前地区账号占用情况..."):
            availability = evaluate_region_availability(region_accounts, locks, meeting_date, start_time, end_time)
        render_availability_results(availability)

    if st.button("预约会议", key=f"{region}_book_meeting", type="primary"):
        if not topic.strip():
            st.error("请输入会议主题")
            return

        try:
            with st.spinner("正在自动分配账号并创建会议..."):
                availability = evaluate_region_availability(region_accounts, locks, meeting_date, start_time, end_time)
                if not availability["recommended"]:
                    render_availability_results(availability)
                    st.error("当前地区没有可预约账号，会议未创建。")
                    return

                selected_account = availability["recommended"]["account"]
                account_config = region_accounts[selected_account]
                access_token = get_access_token(
                    account_config["account_id"],
                    account_config["client_id"],
                    account_config["client_secret"],
                )
                start_time_utc = local_to_utc(meeting_date, start_time)
                meeting = create_meeting(
                    access_token,
                    topic.strip(),
                    start_time_utc,
                    duration,
                    user_id=get_zoom_user_id(account_config),
                )

            output = format_booking_output(topic.strip(), meeting, meeting_date, start_time)
            add_meeting_record(selected_account, region, meeting, output)
            st.success(f"会议预约成功！地区：{region}，分配账号：{selected_account}")
            st.code(output, language=None)
        except requests.exceptions.HTTPError as exc:
            response = exc.response
            message = f"{response.status_code} - {response.text}" if response is not None else str(exc)
            st.error(f"接口错误：{message}")
        except requests.exceptions.RequestException as exc:
            st.error(f"网络错误：{exc}")
        except KeyError as exc:
            st.error(f"缺少凭证：{exc}")
        except Exception as exc:
            st.error(f"错误：{exc}")


def render_region_schedule(region, region_accounts, locks):
    st.subheader("📊 时间占用")
    if not region_accounts:
        st.warning("当前地区没有可用账号。")
        return

    view_date = st.date_input("选择日期", value=datetime.now().date(), key=f"{region}_grid_date")
    selected_accounts = st.multiselect(
        "选择要查看的账号",
        list(region_accounts.keys()),
        default=list(region_accounts.keys()),
        key=f"{region}_grid_accounts",
    )

    if not selected_accounts:
        st.info("请选择至少一个账号")
        return

    schedule_map = {}
    with st.spinner("正在拉取会议和锁定数据..."):
        for account_name in selected_accounts:
            schedule_map[account_name] = fetch_account_schedule(account_name, region_accounts[account_name], locks)

    st.markdown("### 时间表（绿色=空闲，红色=会议占用，黄色=管理员锁定）")
    render_schedule_table(schedule_map, selected_accounts, view_date)
    render_schedule_details(schedule_map, selected_accounts, view_date)


def render_region_records(region, region_accounts):
    st.subheader("📋 会议记录")

    data_source = st.radio(
        "数据来源",
        ["从API查询", "本地记录"],
        horizontal=True,
        key=f"{region}_record_source",
    )

    if data_source == "从API查询":
        if not region_accounts:
            st.warning("当前地区没有可用账号。")
            return

        col_date1, col_date2, col_acc = st.columns([1, 1, 1])
        with col_date1:
            query_start_date = st.date_input(
                "开始日期",
                value=datetime.now().date() - timedelta(days=15),
                key=f"{region}_query_start_date",
            )
        with col_date2:
            query_end_date = st.date_input(
                "结束日期",
                value=datetime.now().date(),
                key=f"{region}_query_end_date",
            )
        with col_acc:
            query_account = st.selectbox(
                "选择账号",
                ["全部"] + list(region_accounts.keys()),
                key=f"{region}_query_account",
            )

        if query_end_date < query_start_date:
            st.error("结束日期不能早于开始日期")
            return

        search_keyword = st.text_input(
            "🔍 搜索会议主题",
            placeholder="输入关键词搜索...",
            key=f"{region}_api_search",
        )

        target_accounts = (
            {query_account: region_accounts[query_account]}
            if query_account != "全部"
            else region_accounts
        )

        with st.spinner("正在从 Zoom API 获取会议..."):
            api_meetings, warnings = fetch_region_api_records(target_accounts, query_start_date, query_end_date)

        for warning in warnings:
            st.warning(warning)

        if search_keyword:
            api_meetings = [item for item in api_meetings if search_keyword.lower() in item["topic"].lower()]

        st.write(f"共 **{len(api_meetings)}** 个会议")

        if not api_meetings:
            st.info("未找到会议")
            return

        for meeting in sorted(api_meetings, key=lambda item: item["start_time"], reverse=True):
            with st.expander(
                f"🎯 {meeting['topic']} - {meeting['start_time']} ({meeting['account']})",
                expanded=False,
            ):
                output = f"""Subject: {meeting['topic']}
Time: {meeting['start_time']} (Beijing/Shanghai Time)
Join Zoom Meeting
{meeting['join_url']}

Meeting ID: {format_meeting_id(meeting['meeting_id'])}
Passcode: {meeting['password']}"""
                st.code(output, language=None)

                if st.button(
                    "获取录制",
                    key=f"{region}_rec_api_{meeting['account']}_{meeting['meeting_id']}",
                ):
                    try:
                        account_config = region_accounts[meeting["account"]]
                        token = get_access_token(
                            account_config["account_id"],
                            account_config["client_id"],
                            account_config["client_secret"],
                        )
                        recording = get_meeting_recordings(token, meeting["meeting_id"])

                        if recording:
                            if recording.get("share_url"):
                                st.markdown(f"**观看链接：** [{recording['share_url']}]({recording['share_url']})")
                            if recording.get("password"):
                                st.markdown(f"**密码：** `{recording['password']}`")
                            st.success("录制信息获取成功！")
                        else:
                            st.warning("该会议暂无录制或录制未完成")
                    except Exception as exc:
                        st.error(f"获取录制失败：{exc}")
    else:
        meetings = load_meetings()
        region_account_names = set(region_accounts.keys())

        search_keyword = st.text_input(
            "🔍 搜索会议主题",
            placeholder="输入关键词搜索...",
            key=f"{region}_local_search",
        )
        filter_account = st.selectbox(
            "筛选账号",
            ["全部"] + list(region_accounts.keys()),
            key=f"{region}_filter_account",
        )

        total_count = sum(len(records) for account_name, records in meetings.items() if account_name in region_account_names)
        st.write(f"共 **{total_count}** 条会议记录")

        any_record = False
        for account_name in sorted(region_account_names):
            if filter_account != "全部" and account_name != filter_account:
                continue

            account_records = meetings.get(account_name, [])
            if search_keyword:
                account_records = [
                    item for item in account_records if search_keyword.lower() in item.get("topic", "").lower()
                ]

            if not account_records:
                continue

            any_record = True
            st.subheader(f"📁 {account_name} ({len(account_records)} 条)")
            for record in reversed(account_records):
                with st.expander(f"🎯 {record['topic']} - {record['created_at']}", expanded=False):
                    st.code(record["output"], language=None)
                    col1, col2, col3 = st.columns([1.3, 1, 1])
                    with col1:
                        st.markdown(f"**会议链接：** {record['join_url']}")
                    with col2:
                        if st.button(
                            "删除记录",
                            key=f"{region}_del_local_{account_name}_{record['meeting_id']}",
                        ):
                            delete_meeting_record(account_name, record["meeting_id"])
                            st.success("已删除")
                            st.rerun()
                    with col3:
                        if st.button(
                            "获取录制",
                            key=f"{region}_rec_local_{account_name}_{record['meeting_id']}",
                        ):
                            try:
                                account_config = region_accounts[account_name]
                                token = get_access_token(
                                    account_config["account_id"],
                                    account_config["client_id"],
                                    account_config["client_secret"],
                                )
                                recording = get_meeting_recordings(token, record["meeting_id"])

                                if recording:
                                    if recording.get("share_url"):
                                        st.markdown(
                                            f"**观看链接：** [{recording['share_url']}]({recording['share_url']})"
                                        )
                                    if recording.get("password"):
                                        st.markdown(f"**密码：** `{recording['password']}`")
                                    st.success("录制信息获取成功！")
                                else:
                                    st.warning("该会议暂无录制或录制未完成")
                            except Exception as exc:
                                st.error(f"获取录制失败：{exc}")

        if not any_record:
            st.info("暂无会议记录")


def render_region_page(region, accounts, locks):
    st.title(f"📍 {region} Zoom 预约中心")

    ready_accounts = get_accounts_for_region(accounts, region, ready_only=True)
    incomplete_accounts = {
        name: config
        for name, config in get_accounts_for_region(accounts, region, ready_only=False).items()
        if not account_is_ready(config)
    }

    if incomplete_accounts:
        st.warning(
            "当前地区有未完成配置的账号，尚未进入调度池："
            + "、".join(incomplete_accounts.keys())
        )

    tab1, tab2, tab3 = st.tabs(["📝 预约会议", "📊 时间占用", "📋 会议记录"])

    with tab1:
        render_region_booking(region, ready_accounts, locks)
    with tab2:
        render_region_schedule(region, ready_accounts, locks)
    with tab3:
        render_region_records(region, ready_accounts)


def validate_account_payload(account_name, payload, existing_accounts, old_name=None):
    if not account_name.strip():
        return False, "账号名称不能为空"
    if payload["region"] not in REGIONS:
        return False, "请选择有效地区"
    if not payload["account_id"] or not payload["client_id"] or not payload["client_secret"]:
        return False, "请填写 account_id、client_id 和 client_secret"
    if account_name != old_name and account_name in existing_accounts:
        return False, "该账号名称已存在"
    return True, ""


def validate_lock_payload(account_name, lock_date, start_time, end_time, reason):
    if not account_name:
        return False, None, None, "请选择账号"
    if not reason.strip():
        return False, None, None, "请填写锁定原因"

    start_dt = localize_datetime(lock_date, start_time)
    end_dt = localize_datetime(lock_date, end_time)
    if end_dt <= start_dt:
        return False, None, None, "锁定结束时间必须晚于开始时间"
    return True, start_dt, end_dt, ""


def ensure_lock_conflict_free(account_name, start_dt, end_dt, accounts, locks, exclude_lock_id=None):
    account_config = accounts.get(account_name)
    if not account_config or not account_is_ready(account_config):
        return False, "账号不存在或配置不完整，无法创建锁定"

    meeting_events, _, error = fetch_zoom_events_for_account(account_name, account_config)
    if error:
        return False, f"无法校验 Zoom 会议冲突：{error}"

    for event in meeting_events:
        if ranges_overlap(event["start_dt"], event["end_dt"], start_dt, end_dt):
            return False, f"与现有会议冲突：{describe_conflict(event)}"

    for lock in locks:
        if lock.get("account_name") != account_name:
            continue
        if lock.get("id") == exclude_lock_id:
            continue
        lock_event = build_lock_event(lock)
        if ranges_overlap(lock_event["start_dt"], lock_event["end_dt"], start_dt, end_dt):
            return False, f"与已有锁定冲突：{describe_conflict(lock_event)}"

    return True, ""


def render_admin_overview(accounts, locks):
    st.subheader("📈 全局概览")
    col1, col2, col3 = st.columns(3)
    col1.metric("账号总数", len(accounts))
    col2.metric("锁定总数", len(locks))
    col3.metric("已配置地区数", len({config.get('region') for config in accounts.values() if config.get('region')}))

    st.markdown("### 地区账号分布")
    for region in REGIONS:
        region_accounts = get_accounts_for_region(accounts, region, ready_only=False)
        ready_count = len([1 for config in region_accounts.values() if account_is_ready(config)])
        st.write(f"• {region}：共 {len(region_accounts)} 个账号，其中 {ready_count} 个已进入调度池")

    incomplete_accounts = get_incomplete_accounts(accounts)
    if incomplete_accounts:
        st.markdown("### 待补全账号")
        for account_name, config in incomplete_accounts.items():
            region_text = config.get("region") or "未分配地区"
            st.write(f"• {account_name}（{region_text}）")

    st.markdown("### 当前锁定")
    if not locks:
        st.info("暂无锁定记录")
        return

    for lock in sorted(locks, key=lambda item: item["start_time"]):
        start_dt = parse_iso_datetime(lock["start_time"])
        end_dt = parse_iso_datetime(lock["end_time"])
        st.write(
            f"• [{lock.get('region', '未分配地区')}] {lock['account_name']} | "
            f"{format_range(start_dt, end_dt)} | {lock.get('reason', '未填写原因')}"
        )


def render_admin_page(accounts, locks):
    st.title("🛠️ 管理员后台")

    if "admin_authenticated" not in st.session_state:
        st.session_state.admin_authenticated = False

    if not st.session_state.admin_authenticated:
        st.info("请输入管理员密码后继续。")
        with st.form("admin_login_form"):
            password = st.text_input("管理员密码", type="password")
            submitted = st.form_submit_button("进入后台")
        if submitted:
            if password == ADMIN_PASSWORD:
                st.session_state.admin_authenticated = True
                st.rerun()
            st.error("密码错误")
        return

    col_left, col_right = st.columns([5, 1])
    with col_left:
        st.caption("已通过管理员验证")
    with col_right:
        if st.button("退出登录", key="admin_logout"):
            st.session_state.admin_authenticated = False
            st.rerun()

    tab1, tab2, tab3, tab4 = st.tabs(["➕ 新增账号", "✏️ 账号管理", "🔒 锁定管理", "📈 全局总览"])

    with tab1:
        st.subheader("新增账号")
        with st.form("add_account_form"):
            account_name = st.text_input("账号名称", placeholder="例如：华东5")
            region = st.selectbox("所属地区", REGIONS)
            account_id = st.text_input("Account ID")
            client_id = st.text_input("Client ID")
            client_secret = st.text_input("Client Secret", type="password")
            user_email = st.text_input("用户邮箱（可选）", placeholder="用于指定 Zoom 用户")
            submitted = st.form_submit_button("添加账号")

        if submitted:
            payload = {
                "region": region,
                "account_id": account_id.strip(),
                "client_id": client_id.strip(),
                "client_secret": client_secret.strip(),
                "user_email": user_email.strip(),
            }
            is_valid, message = validate_account_payload(account_name.strip(), payload, accounts)
            if not is_valid:
                st.error(message)
            else:
                accounts[account_name.strip()] = payload
                save_accounts(dict(sorted(accounts.items())))
                st.success(f"账号 {account_name.strip()} 已添加")
                st.rerun()

    with tab2:
        st.subheader("账号管理 / 地区分配")
        if not accounts:
            st.info("暂无账号，请先新增账号。")
        else:
            sorted_accounts = dict(sorted(accounts.items()))
            selected_account = st.selectbox("选择账号", list(sorted_accounts.keys()), key="admin_edit_account")
            selected_config = sorted_accounts[selected_account]
            region_index = REGIONS.index(selected_config["region"]) if selected_config.get("region") in REGIONS else 0

            with st.form("edit_account_form"):
                new_name = st.text_input("账号名称", value=selected_account)
                new_region = st.selectbox("所属地区", REGIONS, index=region_index)
                new_account_id = st.text_input("Account ID", value=selected_config.get("account_id", ""))
                new_client_id = st.text_input("Client ID", value=selected_config.get("client_id", ""))
                new_client_secret = st.text_input(
                    "Client Secret",
                    value=selected_config.get("client_secret", ""),
                    type="password",
                )
                new_user_email = st.text_input("用户邮箱（可选）", value=selected_config.get("user_email", ""))
                submitted = st.form_submit_button("保存修改")

            if submitted:
                payload = {
                    "region": new_region,
                    "account_id": new_account_id.strip(),
                    "client_id": new_client_id.strip(),
                    "client_secret": new_client_secret.strip(),
                    "user_email": new_user_email.strip(),
                }
                is_valid, message = validate_account_payload(new_name.strip(), payload, accounts, old_name=selected_account)
                if not is_valid:
                    st.error(message)
                else:
                    if new_name.strip() != selected_account:
                        accounts.pop(selected_account, None)
                    accounts[new_name.strip()] = payload
                    accounts = dict(sorted(accounts.items()))
                    save_accounts(accounts)
                    sync_meeting_records_for_account(selected_account, new_name.strip(), new_region)
                    sync_locks_for_account(selected_account, new_name.strip(), new_region)
                    st.success("账号信息已更新")
                    st.rerun()

            st.markdown("---")
            st.subheader("删除账号")
            delete_account = st.selectbox("选择要删除的账号", list(sorted_accounts.keys()), key="admin_delete_account")
            st.caption("删除账号时，会同时删除该账号的本地会议记录和全部锁定记录。")
            if st.button("删除账号", key="admin_delete_account_btn"):
                accounts.pop(delete_account, None)
                save_accounts(accounts)
                delete_account_related_data(delete_account)
                st.success(f"账号 {delete_account} 已删除")
                st.rerun()

    with tab3:
        st.subheader("锁定管理")
        ready_accounts = {name: config for name, config in sorted(accounts.items()) if account_is_ready(config)}

        if not ready_accounts:
            st.warning("没有可用于锁定的账号，请先完成账号配置。")
        else:
            st.markdown("### 新增锁定")
            with st.form("add_lock_form"):
                account_name = st.selectbox("锁定账号", list(ready_accounts.keys()))
                lock_date = st.date_input("锁定日期", value=datetime.now().date(), key="new_lock_date")
                col1, col2 = st.columns(2)
                with col1:
                    start_lock_time = st.time_input("开始时间", value=time(9, 0), key="new_lock_start")
                with col2:
                    end_lock_time = st.time_input("结束时间", value=time(10, 0), key="new_lock_end")
                reason = st.text_area("锁定原因", placeholder="例如：管理员预留 / 培训占用")
                submitted = st.form_submit_button("创建锁定")

            if submitted:
                is_valid, start_dt, end_dt, message = validate_lock_payload(
                    account_name,
                    lock_date,
                    start_lock_time,
                    end_lock_time,
                    reason,
                )
                if not is_valid:
                    st.error(message)
                else:
                    conflict_free, error_message = ensure_lock_conflict_free(
                        account_name,
                        start_dt,
                        end_dt,
                        accounts,
                        locks,
                    )
                    if not conflict_free:
                        st.error(error_message)
                    else:
                        locks.append(
                            {
                                "id": f"lock_{uuid4().hex[:8]}",
                                "account_name": account_name,
                                "region": accounts[account_name]["region"],
                                "start_time": serialize_local_datetime(start_dt),
                                "end_time": serialize_local_datetime(end_dt),
                                "reason": reason.strip(),
                            }
                        )
                        save_locks(sort_locks(locks))
                        st.success("锁定已创建")
                        st.rerun()

            st.markdown("---")
            st.markdown("### 编辑 / 删除锁定")
            if not locks:
                st.info("暂无锁定记录")
            else:
                sorted_lock_list = sort_locks(locks)
                lock_lookup = {lock["id"]: lock for lock in sorted_lock_list}
                selected_lock_id = st.selectbox(
                    "选择锁定记录",
                    [lock["id"] for lock in sorted_lock_list],
                    format_func=lambda lock_id: format_lock_option(lock_lookup[lock_id]),
                    key="edit_lock_select",
                )
                selected_lock = lock_lookup[selected_lock_id]
                selected_start = parse_iso_datetime(selected_lock["start_time"])
                selected_end = parse_iso_datetime(selected_lock["end_time"])
                account_index = list(ready_accounts.keys()).index(selected_lock["account_name"])

                with st.form("edit_lock_form"):
                    edit_account = st.selectbox(
                        "锁定账号",
                        list(ready_accounts.keys()),
                        index=account_index,
                        key="edit_lock_account",
                    )
                    edit_date = st.date_input(
                        "锁定日期",
                        value=selected_start.date(),
                        key="edit_lock_date",
                    )
                    col1, col2 = st.columns(2)
                    with col1:
                        edit_start_time = st.time_input(
                            "开始时间",
                            value=time(selected_start.hour, selected_start.minute),
                            key="edit_lock_start_time",
                        )
                    with col2:
                        edit_end_time = st.time_input(
                            "结束时间",
                            value=time(selected_end.hour, selected_end.minute),
                            key="edit_lock_end_time",
                        )
                    edit_reason = st.text_area(
                        "锁定原因",
                        value=selected_lock.get("reason", ""),
                        key="edit_lock_reason",
                    )
                    submitted = st.form_submit_button("保存锁定修改")

                if submitted:
                    is_valid, start_dt, end_dt, message = validate_lock_payload(
                        edit_account,
                        edit_date,
                        edit_start_time,
                        edit_end_time,
                        edit_reason,
                    )
                    if not is_valid:
                        st.error(message)
                    else:
                        conflict_free, error_message = ensure_lock_conflict_free(
                            edit_account,
                            start_dt,
                            end_dt,
                            accounts,
                            locks,
                            exclude_lock_id=selected_lock_id,
                        )
                        if not conflict_free:
                            st.error(error_message)
                        else:
                            for lock in locks:
                                if lock["id"] == selected_lock_id:
                                    lock["account_name"] = edit_account
                                    lock["region"] = accounts[edit_account]["region"]
                                    lock["start_time"] = serialize_local_datetime(start_dt)
                                    lock["end_time"] = serialize_local_datetime(end_dt)
                                    lock["reason"] = edit_reason.strip()
                                    break
                            save_locks(sort_locks(locks))
                            st.success("锁定已更新")
                            st.rerun()

                if st.button("删除锁定", key=f"delete_lock_{selected_lock_id}"):
                    locks = [lock for lock in locks if lock["id"] != selected_lock_id]
                    save_locks(sort_locks(locks))
                    st.success("锁定已删除")
                    st.rerun()

    with tab4:
        render_admin_overview(accounts, locks)


def sort_locks(locks):
    return sorted(locks, key=lambda item: (item["start_time"], item["account_name"], item["id"]))


def format_lock_option(lock):
    start_dt = parse_iso_datetime(lock["start_time"])
    end_dt = parse_iso_datetime(lock["end_time"])
    return (
        f"[{lock.get('region', '未分配地区')}] {lock['account_name']} | "
        f"{format_range(start_dt, end_dt)} | {lock.get('reason', '未填写原因')}"
    )


def render_sidebar(accounts):
    with st.sidebar:
        st.title("📚 导航")
        selection = st.radio("选择页面", NAV_ITEMS, key="main_navigation")
        st.markdown("---")
        st.write(f"已加载 **{len(accounts)}** 个账号")
        for region in REGIONS:
            total_count = len(get_accounts_for_region(accounts, region, ready_only=False))
            ready_count = len(get_accounts_for_region(accounts, region, ready_only=True))
            st.caption(f"{region}：{ready_count}/{total_count} 已就绪")
        if st.session_state.get("admin_authenticated"):
            st.caption("管理员：已登录")
    return selection


def main():
    accounts = load_accounts()
    locks = load_locks(accounts)
    selected_page = render_sidebar(accounts)

    if selected_page in REGIONS:
        render_region_page(selected_page, accounts, locks)
    else:
        render_admin_page(accounts, locks)


main()
