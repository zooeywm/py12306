"""Local-only booking controls; auth is identical to /manage."""
from io import BytesIO
from flask import Blueprint, jsonify, request, send_file

from py12306.config import Config
from py12306.order.booking_manager import BookingError, BookingManager
from py12306.user.user import User
from py12306.web.handler.manage import (_auth_required, _lock, _save_jobs,
                                         _write_allowed, BadTask)

booking = Blueprint('booking', __name__)


def _contact_info(user):
    if not user.is_ready:
        return []
    contacts = []
    for passenger in user.passengers:
        name = passenger.get('passenger_name')
        if not name:
            continue
        # UserJob matches a contact's numeric code as a string; for legacy
        # numeric/non-numeric codes use the contact's name instead.
        code = passenger.get('code')
        value = code if isinstance(code, str) and code.isdecimal() else name
        contacts.append({'name': name, 'value': value})
    return contacts


@booking.errorhandler(BookingError)
@booking.errorhandler(BadTask)
def bad_booking(error):
    return jsonify(error=str(error)), 400


@booking.route('/manage/order-ui.js')
@_auth_required
def booking_ui():
    response = send_file(Config().PROJECT_DIR + 'py12306/web/static/order-ui.js', mimetype='text/javascript')
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@booking.route('/manage/api/booking/state')
@_auth_required
def booking_state():
    users = list(User().users)
    for user in users:
        if user.is_ready:
            user.ensure_web_contacts()
    accounts = [
        {'key': str(user.key), 'label': str(user.user_name or user.key),
         'ready': bool(user.is_ready), 'passengers': _contact_info(user),
         'qr': user.get_web_qr_status() if user.type == 'qr' else None}
        for user in users
    ]
    tasks = [
        {'index': idx, 'job_name': task.get('job_name', ''),
         'date': (task.get('left_dates') or [''])[0],
         'seats': list(task.get('seats') or []),
         'enabled': task.get('enabled', True),
         'auto_order': dict(task.get('auto_order') or {})}
        for idx, task in enumerate(Config().QUERY_JOBS)
    ]
    return jsonify(accounts=accounts, tasks=tasks,
                   order=BookingManager().get_status(),
                   booking_enabled=bool(Config().USER_ACCOUNTS))




@booking.route('/manage/api/booking/qr/<account_key>/image', methods=['GET'])
@_auth_required
def qr_image(account_key):
    """Serve QR bytes only to the local, authenticated task manager."""
    user = User.get_user(account_key)
    if not user or user.type != 'qr':
        return jsonify(error='12306 扫码账号不存在'), 404
    with user._qr_lock:
        image = user.qr_image if not user.is_ready else None
    if not image:
        response = jsonify(error='二维码尚未生成或已失效')
        response.status_code = 404
        response.headers['Cache-Control'] = 'no-store'
        return response
    response = send_file(BytesIO(image), mimetype='image/png')
    response.headers['Cache-Control'] = 'no-store, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@booking.route('/manage/api/booking/qr/<account_key>/refresh', methods=['POST'])
@_auth_required
def refresh_qr(account_key):
    _write_allowed()
    if request.get_json(silent=True) != {}:
        raise BookingError('无效的扫码请求')
    user = User.get_user(account_key)
    if not user or user.type != 'qr':
        raise BookingError('请选择已配置扫码登录的 12306 账号')
    return jsonify(message=user.request_web_qr_login())


@booking.route('/manage/api/booking/contacts', methods=['POST'])
@_auth_required
def load_contacts():
    _write_allowed()
    payload = request.get_json()
    if not isinstance(payload, dict) or set(payload) != {'account_key'}:
        raise BookingError('账号无效')
    user = User.get_user(str(payload['account_key']))
    if not user or not user.is_ready:
        raise BookingError('请先完成 12306 登录')
    if not user.passengers:
        from py12306.helpers.api import API_USER_PASSENGERS
        response = user.session.post(API_USER_PASSENGERS, timeout=8)
        data = response.json()
        passengers = data.get('data.normal_passengers')
        if not isinstance(passengers, list) or not passengers:
            raise BookingError('联系人暂不可用，请稍后重试或重新登录')
        user.passengers = passengers
    return jsonify(passengers=_contact_info(user))


@booking.route('/manage/api/booking/manual', methods=['POST'])
@_auth_required
def manual_booking():
    _write_allowed()
    payload = request.get_json()
    if not isinstance(payload, dict) or set(payload) - {'job_name', 'date', 'train', 'seat', 'account_key', 'members'}:
        raise BookingError('无效的下单请求')
    info = BookingManager().manual(payload.get('job_name'), payload.get('date'),
                                   payload.get('train'), payload.get('seat'),
                                   payload.get('account_key'), payload.get('members'))
    return jsonify(order=info), 202


@booking.route('/manage/api/booking/auto/<int:index>', methods=['PUT'])
@_auth_required
def update_auto_booking(index):
    _write_allowed()
    payload = request.get_json()
    if not isinstance(payload, dict) or set(payload) - {'enabled', 'account_key', 'members'}:
        raise BookingError('无效的自动抢票配置')
    if type(payload.get('enabled')) is not bool:
        raise BookingError('enabled 必须为布尔值')
    enabled = payload['enabled']
    with _lock:
        current = list(Config().QUERY_JOBS)
        if not 0 <= index < len(current):
            raise BookingError('任务已改变，请刷新')
        if enabled:
            account = User.get_user(str(payload.get('account_key', '')))
            if not account or not account.is_ready:
                raise BookingError('请先完成 12306 扫码登录')
            members = payload.get('members')
            if not isinstance(members, list) or not 1 <= len(members) <= 5 or len(set(members)) != len(members):
                raise BookingError('必须选择 1～5 位不重复的乘车人')
            known = {p['value'] for p in _contact_info(account)}
            if any(not isinstance(member, str) or member not in known for member in members):
                raise BookingError('乘车人必须来自已登录账号的常用联系人')
            if not current[index].get('seats'):
                raise BookingError('请先设置目标席别')
            rule = {'enabled': True, 'account_key': str(account.key), 'members': members}
        else:
            rule = {'enabled': False, 'account_key': '', 'members': []}
        task = dict(current[index])
        task['monitor_only'] = True
        task['auto_order'] = rule
        current[index] = task
        # Clear the restart-safe failure latch only after explicit user opt-in.
        # Do this before persisting enabled=True: if latch update fails, no
        # automatic booking rule should be enabled on the next launch.
        if enabled:
            BookingManager().resume_auto(str(account.key))
        _save_jobs(current)
    return jsonify(auto_order=rule)
