"""Local-only, authenticated management of *monitor-only* query tasks.

This module edits only QUERY_JOBS in env.py. It never accepts passenger data
or account credentials and never exposes an endpoint that places orders.
"""
import ast
import hmac
import os
import pprint
import re
import stat
import tempfile
import threading
import time
from datetime import date, timedelta
from functools import wraps

from flask import Blueprint, jsonify, request, send_file

from py12306.config import Config
from py12306.helpers.api import LEFT_TICKETS
from py12306.helpers.station import Station
from py12306.helpers.type import SeatType
from py12306.query.query import Query

manage = Blueprint('task_manage', __name__)
_lock = threading.RLock()
_TRAIN_RE = re.compile(r'^[A-Za-z0-9]{1,8}$')
_TRAIN_MODELS = ('复兴号', '和谐号', '火车')
_TIME_RE = re.compile(r'^(?:[01][0-9]|2[0-3]):[0-5][0-9]$')
_train_query_lock = threading.Lock()
_train_query_cache = {}


class BadTask(ValueError):
    pass


def _auth_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        supplied = request.authorization
        user = Config().WEB_USER or {}
        username = str(user.get('username') or '')
        password = str(user.get('password') or '')
        # Refuse unconfigured or trivial admin passwords.
        valid_setup = username and password and password not in ('password', '123456', 'admin')
        if not (valid_setup and supplied and
                hmac.compare_digest(supplied.username or '', username) and
                hmac.compare_digest(supplied.password or '', password)):
            response = jsonify(error='请在 env.py 中设置强密码 WEB_USER 后使用管理员账号登录')
            response.status_code = 401
            response.headers['WWW-Authenticate'] = 'Basic realm="py12306 task manager"'
            response.headers['Cache-Control'] = 'no-store'
            return response
        return fn(*args, **kwargs)
    return wrapper


def _write_allowed():
    if Config().is_cluster_enabled():
        raise BadTask('网页任务管理暂不支持集群模式')
    if request.headers.get('X-Py12306-Manage') != '1' or not request.is_json:
        raise BadTask('仅允许本站发起的 JSON 管理请求')
    origin = request.headers.get('Origin')
    if origin and origin.rstrip('/') != request.host_url.rstrip('/'):
        raise BadTask('跨站请求被拒绝')


def _csv(value):
    if not isinstance(value, str) or len(value) > 320:
        raise BadTask('车次筛选格式无效')
    values = [s.strip().upper() for s in re.split(r'[,，\s]+', value) if s.strip()]
    if len(values) > 30 or any(not _TRAIN_RE.fullmatch(s) for s in values):
        raise BadTask('车次只能包含字母或数字，最多 30 个')
    return list(dict.fromkeys(values))


def _time(value, allow_24=False):
    if not isinstance(value, str) or not (_TIME_RE.fullmatch(value) or (allow_24 and value == '24:00')):
        raise BadTask('出发时间必须是 HH:MM 格式')
    return value


def _validated(payload, previous=None):
    if not isinstance(payload, dict):
        raise BadTask('任务内容必须是对象')
    left = str(payload.get('left', '')).strip()
    arrive = str(payload.get('arrive', '')).strip()
    if left == arrive:
        raise BadTask('出发站和到达站不能相同')
    if not Station.get_station_by_name(left) or not Station.get_station_by_name(arrive):
        raise BadTask('出发站或到达站不存在，请选择车站全名')

    d = payload.get('date')
    try:
        departure = date.fromisoformat(d) if isinstance(d, str) else None
    except ValueError:
        departure = None
    if not departure or not date.today() <= departure <= date.today() + timedelta(days=32):
        raise BadTask('乘车日期必须在今天起的 32 天内')

    name = str(payload.get('job_name') or ('{}→{}'.format(left, arrive))).strip()
    if not (1 <= len(name) <= 60):
        raise BadTask('任务名称长度应为 1～60 个字符')
    seats = payload.get('seats')
    if not isinstance(seats, list) or not seats or len(seats) > len(SeatType.dicts):
        raise BadTask('至少选择一个席别')
    if any(not isinstance(seat, str) or seat not in SeatType.dicts for seat in seats):
        raise BadTask('包含不支持的席别')
    seats = list(dict.fromkeys(seats))
    models = payload.get('train_models', list(_TRAIN_MODELS))
    if not isinstance(models, list) or not models or any(
            not isinstance(model, str) or model not in _TRAIN_MODELS for model in models):
        raise BadTask('至少选择一种有效车型')
    models = list(dict.fromkeys(models))
    trains = _csv(payload.get('train_numbers', ''))
    excluded = _csv(payload.get('except_train_numbers', ''))
    if trains and excluded:
        raise BadTask('指定车次和排除车次不能同时使用')
    start = _time(payload.get('from_time', '00:00'))
    end = _time(payload.get('to_time', '24:00'), allow_24=True)
    if start >= end:
        raise BadTask('结束时间必须晚于开始时间')

    task = dict(previous or {})
    task.update({
        'job_name': name,
        'account_key': 0,
        'left_dates': [departure.isoformat()],
        'stations': {'left': left, 'arrive': arrive},
        'members': [],
        'allow_less_member': 0,
        'seats': seats,
        'train_models': models,
        'train_numbers': trains,
        'except_train_numbers': excluded,
        'period': {'from': start, 'to': end},
        'monitor_only': True,
        'enabled': bool(payload.get('enabled', True)),
    })
    return task


def _safe_tasks(tasks):
    output = []
    for idx, job in enumerate(tasks):
        stations = job.get('stations') or {}
        if isinstance(stations, list):
            stations = stations[0] if stations else {}
        period = job.get('period') or {}
        dates = job.get('left_dates') or []
        output.append({
            'index': idx,
            'job_name': job.get('job_name') or '{}→{}'.format(stations.get('left', ''), stations.get('arrive', '')),
            'left': stations.get('left', ''),
            'arrive': stations.get('arrive', ''),
            'date': dates[0] if dates else '',
            'seats': job.get('seats') or [],
            'train_models': job.get('train_models') or list(_TRAIN_MODELS),
            'train_numbers': ','.join(job.get('train_numbers') or []),
            'except_train_numbers': ','.join(job.get('except_train_numbers') or []),
            'from_time': period.get('from', '00:00'),
            'to_time': period.get('to', '24:00'),
            'enabled': job.get('enabled', True),
            'monitor_only': bool(job.get('monitor_only', False)),
        })
    return output


def _save_jobs(tasks):
    filename = Config().CONFIG_FILE
    with open(filename, 'r', encoding='utf-8') as f:
        content = f.read()
    tree = ast.parse(content, filename=filename)
    nodes = [node for node in tree.body if isinstance(node, ast.Assign) and
             len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and
             node.targets[0].id == 'QUERY_JOBS']
    if len(nodes) != 1:
        raise BadTask('env.py 必须包含唯一的顶层 QUERY_JOBS = [...] 赋值')
    node = nodes[0]
    lines = content.splitlines(keepends=True)
    assignment = 'QUERY_JOBS = ' + pprint.pformat(tasks, width=96, sort_dicts=False) + '\n'
    lines[node.lineno - 1:node.end_lineno] = [assignment]
    updated = ''.join(lines)
    # Store via same-directory atomic replace. Do not rewrite account credentials.
    fd, tempname = tempfile.mkstemp(prefix='.env-web-', suffix='.tmp', dir=os.path.dirname(filename))
    try:
        os.fchmod(fd, stat.S_IMODE(os.stat(filename).st_mode))
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(updated)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tempname, filename)
    finally:
        if os.path.exists(tempname):
            os.unlink(tempname)
    # Apply immediately; the existing config watcher will observe the same data.
    Config().QUERY_JOBS = tasks
    Query().update_query_jobs(auto=True)


def _mutate(action, index=None):
    _write_allowed()
    with _lock:
        current = list(Config().QUERY_JOBS)
        if action == 'add':
            if len(current) >= 30:
                raise BadTask('最多支持 30 个任务')
            changed = _validated(request.get_json())
            current.append(changed)
        else:
            if index is None or index < 0 or index >= len(current):
                raise BadTask('任务不存在，请刷新页面')
            if action == 'edit':
                changed = _validated(request.get_json(), current[index])
                current[index] = changed
            elif action == 'toggle':
                payload = request.get_json()
                if not isinstance(payload, dict) or type(payload.get('enabled')) is not bool:
                    raise BadTask('enabled 必须为布尔值')
                changed = dict(current[index])
                changed['enabled'] = payload['enabled']
                changed['monitor_only'] = True
                current[index] = changed
            elif action == 'delete':
                current.pop(index)
            else:
                raise BadTask('操作无效')
        names = [t.get('job_name') or str(t.get('stations')) for t in current]
        if len(set(names)) != len(names):
            raise BadTask('任务名称不能重复')
        _save_jobs(current)
        return jsonify(tasks=_safe_tasks(current))


@manage.errorhandler(BadTask)
def bad_task(error):
    return jsonify(error=str(error)), 400


@manage.route('/manage')
@_auth_required
def manage_page():
    response = send_file(os.path.join(Config().PROJECT_DIR, 'py12306/web/static/manage.html'))
    response.headers['Cache-Control'] = 'no-store'
    return response


@manage.route('/manage/api/tasks', methods=['GET'])
@_auth_required
def tasks_list():
    return jsonify(tasks=_safe_tasks(Config().QUERY_JOBS), can_edit=True)


@manage.route('/manage/api/availability', methods=['GET'])
@_auth_required
def availability():
    """Read already queried in-memory snapshots; never requests tickets or orders."""
    groups = []
    # A list copy is important because paused/edited jobs may be removed in a
    # different thread while the browser is polling.
    for job in list(Query().jobs):
        if not getattr(job, 'is_alive', True):
            continue
        if not (getattr(job, 'monitor_only', False) or not Config().USER_ACCOUNTS):
            continue
        monitor = getattr(job, 'seat_monitor', None)
        if monitor is None:
            continue
        groups.append({
            'job_name': job.job_name,
            'routes': monitor.get_dashboard(),
        })
    response = jsonify(groups=groups)
    response.headers['Cache-Control'] = 'no-store'
    return response


@manage.route('/manage/api/train-options', methods=['GET'])
@_auth_required
def train_options():
    """Read-only one-shot 12306 train discovery for unsaved task drafts."""
    left = request.args.get('left', '').strip()
    arrive = request.args.get('arrive', '').strip()
    date_text = request.args.get('date', '').strip()
    if not left or not arrive or left == arrive:
        raise BadTask('请选择不同的出发站和到达站')
    left_info = Station.get_station_by_name(left)
    arrive_info = Station.get_station_by_name(arrive)
    if not left_info or not arrive_info:
        raise BadTask('车站不存在，请从列表选择')
    try:
        query_date = date.fromisoformat(date_text)
    except ValueError:
        raise BadTask('乘车日期格式错误')
    if not date.today() <= query_date <= date.today() + timedelta(days=32):
        raise BadTask('乘车日期必须在 32 天以内')
    key = (date_text, left_info['key'], arrive_info['key'])
    with _train_query_lock:
        now = time.monotonic()
        cached = _train_query_cache.get(key)
        if cached and now - cached[0] < 60:
            return jsonify(trains=cached[1], cached=True)
        query = Query()
        if not query.api_type:
            raise BadTask('余票查询接口尚未就绪，请稍后重试')
        url = LEFT_TICKETS['url'].format(
            type=query.api_type, left_date=date_text,
            left_station=key[1], arrive_station=key[2])
        try:
            response = query.session.get(url, timeout=6)
            if response.status_code != 200:
                raise BadTask('12306 查询失败，请稍后重试')
            payload = response.json()
            values = payload.get('data.result')
            if not isinstance(values, list):
                data = payload.get('data')
                values = data.get('result') if isinstance(data, dict) else None
            if not isinstance(values, list):
                raise BadTask('12306 未返回有效车次数据')
        except BadTask:
            raise
        except Exception:
            raise BadTask('12306 车次查询暂不可用，请稍后重试')
        names = {}
        for row in values:
            if not isinstance(row, str):
                continue
            fields = row.split('|')
            if len(fields) <= 9:
                continue
            name = fields[3].strip().upper()
            if _TRAIN_RE.fullmatch(name):
                names[name] = {'train': name, 'departure': fields[8], 'arrival': fields[9]}
        trains = sorted(names.values(), key=lambda x: (x['departure'], x['train']))
        _train_query_cache[key] = (now, trains)
        if len(_train_query_cache) > 80:
            oldest = min(_train_query_cache, key=lambda k: _train_query_cache[k][0])
            _train_query_cache.pop(oldest, None)
    return jsonify(trains=trains, cached=False)


@manage.route('/manage/api/stations/all', methods=['GET'])
@_auth_required
def all_stations():
    """Return the locally cached 12306 station dataset once for searchable dropdowns."""
    stations = [{'name': info['name'], 'pinyin': info.get('pinyin', '')}
                for info in Station().stations if info.get('name')]
    response = jsonify(stations=list({info['name']: info for info in stations}.values()))
    response.headers['Cache-Control'] = 'private, max-age=3600'
    return response


@manage.route('/manage/api/stations', methods=['GET'])
@_auth_required
def station_lookup():
    query = request.args.get('q', '').strip()
    if not query or len(query) > 30:
        return jsonify(stations=[])
    stations = [s['name'] for s in Station().stations if query in s['name'] or query.lower() in s['pinyin'].lower()]
    return jsonify(stations=list(dict.fromkeys(stations))[:12])


@manage.route('/manage/api/tasks', methods=['POST'])
@_auth_required
def tasks_add():
    return _mutate('add')


@manage.route('/manage/api/tasks/<int:index>', methods=['PUT', 'DELETE'])
@_auth_required
def tasks_edit_delete(index):
    return _mutate('edit' if request.method == 'PUT' else 'delete', index)


@manage.route('/manage/api/tasks/<int:index>/enabled', methods=['PATCH'])
@_auth_required
def tasks_toggle(index):
    return _mutate('toggle', index)
