"""Opt-in manual and automatic reservations for locally authenticated 12306 users.

This module does not handle payment, and never exposes secretStr or identity numbers
through the Web API. Every order is bound to one immutable query snapshot.
"""
import copy
import json
import os
import tempfile
import threading
import time
from datetime import datetime

from py12306.config import Config
from py12306.helpers.type import SeatType
from py12306.query.seat_monitor import train_model_category
from py12306.user.user import User


class BookingError(ValueError):
    pass


class BookingManager:
    _instance = None
    _instance_lock = threading.Lock()

    def __new__(cls):
        with cls._instance_lock:
            if cls._instance is None:
                instance = super().__new__(cls)
                instance.lock = threading.RLock()
                instance.snapshots = {}  # (task, date, station codes) -> private ticket rows
                instance.inflight = False
                instance.last_attempts = {}  # auto retry cooldown, scoped to candidate
                instance.finished_accounts = set()  # protect against duplicate purchases
                instance.auto_paused_accounts = cls._read_paused_accounts()  # restart-safe failure latch
                instance.status = {'state': 'idle', 'message': '尚未提交订单'}
                cls._instance = instance
            return cls._instance

    @staticmethod
    def _pause_file():
        return os.path.join(Config().RUNTIME_DIR, 'booking-paused-accounts.json')

    @classmethod
    def _read_paused_accounts(cls):
        try:
            with open(cls._pause_file(), encoding='utf-8') as stream:
                values = json.load(stream)
            if isinstance(values, list):
                return {item for item in values if isinstance(item, str) and item}
        except (OSError, ValueError, TypeError):
            pass
        return set()

    @classmethod
    def _write_paused_accounts(cls, accounts):
        """Atomic, local-only latch. It contains account keys, not credentials."""
        destination = cls._pause_file()
        folder = os.path.dirname(destination)
        staged = None
        try:
            os.makedirs(folder, mode=0o700, exist_ok=True)
            descriptor, staged = tempfile.mkstemp(prefix='.booking-pause-', dir=folder)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(sorted(accounts), stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, destination)
            return True
        except (OSError, ValueError, TypeError):
            return False
        finally:
            if staged and os.path.exists(staged):
                try:
                    os.unlink(staged)
                except OSError:
                    pass

    @staticmethod
    def _task(name):
        return next((t for t in Config().QUERY_JOBS if t.get('job_name') == name and t.get('enabled', True)), None)

    @staticmethod
    def _account(key):
        return User.get_user(str(key))

    @staticmethod
    def _is_available(fields, seat, count):
        idx = SeatType.dicts.get(seat)
        if idx is None or len(fields) <= max(11, idx):
            return False
        if fields[11] != 'Y' or fields[1] != '预订':
            return False
        raw = fields[idx].strip()
        return raw == '有' or (raw.isdecimal() and int(raw) >= count)

    @staticmethod
    def _has_right_filters(job, fields, seat):
        if seat not in job.allow_seats:
            return False
        try:
            clone = copy.copy(job)
            clone.ticket_info = list(fields)
            return bool(clone.is_trains_number_valid())
        except (ValueError, IndexError, TypeError, AttributeError):
            return False

    def on_query(self, job, results):
        """Capture raw secrets privately, then possibly start one opted-in auto order."""
        key = (job.job_name, job.left_date, job.left_station_code, job.arrive_station_code)
        entries = {}
        for line in results:
            f = line.split('|')
            if len(f) <= 32:
                continue
            if self._has_right_filters(job, f, next(iter(job.allow_seats), '')):
                entries[f[3]] = list(f)
        snapshot = {
            'at': time.monotonic(),
            'date': job.left_date,
            'from_name': job.left_station,
            'to_name': job.arrive_station,
            'from_code': job.left_station_code,
            'to_code': job.arrive_station_code,
            'trains': entries,
        }
        with self.lock:
            self.snapshots[key] = snapshot
            # Avoid unbounded growth for edited/deleted tasks.
            if len(self.snapshots) > 64:
                oldest = min(self.snapshots, key=lambda k: self.snapshots[k]['at'])
                self.snapshots.pop(oldest, None)

        task = self._task(job.job_name)
        rule = (task or {}).get('auto_order') or {}
        if not rule.get('enabled') or not task or not Config().USER_ACCOUNTS:
            return
        key_account = str(rule.get('account_key', ''))
        members = rule.get('members') or []
        if not isinstance(members, list) or not 1 <= len(members) <= 5:
            return
        account = self._account(key_account)
        if not account or not account.is_ready:
            return
        for seat in task.get('seats') or []:
            for train, fields in entries.items():
                if self._is_available(fields, seat, len(members)):
                    try:
                        self.submit(job, snapshot, fields, seat, key_account,
                                    members, origin='auto')
                    except BookingError:
                        pass
                    return

    def manual(self, task_name, date, train, seat, account_key, members):
        if not all(isinstance(x, str) and x for x in (task_name, date, train, seat)):
            raise BookingError('车次、日期或席别无效')
        if not isinstance(members, list) or not 1 <= len(members) <= 5:
            raise BookingError('请选择 1～5 位乘车人')
        if len(set(members)) != len(members) or not all(isinstance(n, str) and 0 < len(n) <= 80 for n in members):
            raise BookingError('乘车人重复或格式无效')
        task = self._task(task_name)
        if not task:
            raise BookingError('任务不存在或已暂停')
        from py12306.query.query import Query
        job = next((j for j in list(Query().jobs) if j.job_name == task_name and j.is_alive), None)
        if not job:
            raise BookingError('查询任务未运行')
        with self.lock:
            matching = [(s, f[train]) for k, s in self.snapshots.items()
                        if k[0] == task_name and k[1] == date and train in s['trains']
                        for f in [s['trains']]]
        if len(matching) != 1:
            raise BookingError('没有唯一的最新余票记录，请等待刷新')
        snapshot, fields = matching[0]
        return self.submit(job, snapshot, fields, seat, str(account_key), members, origin='manual')

    def submit(self, job, snapshot, fields, seat, account_key, members, origin):
        if not Config().USER_ACCOUNTS or Config().is_cluster_enabled():
            raise BookingError('下单只支持本机非集群模式，并需要配置 USER_ACCOUNTS')
        if Config().IS_DEBUG:
            raise BookingError('请关闭 IS_DEBUG 后再使用真实下单')
        if time.monotonic() - snapshot['at'] > 90:
            raise BookingError('余票记录已过期，请刷新查询')
        task = self._task(job.job_name)
        if not task or snapshot['date'] not in (task.get('left_dates') or []):
            raise BookingError('任务日期已改变，拒绝使用旧余票记录')
        stations = task.get('stations') or []
        stations = [stations] if isinstance(stations, dict) else stations
        if not any(p.get('left') == snapshot['from_name'] and p.get('arrive') == snapshot['to_name']
                   for p in stations if isinstance(p, dict)):
            raise BookingError('任务路线已改变，拒绝使用旧余票记录')
        train = fields[3].upper()
        included = [t.upper() for t in task.get('train_numbers') or []]
        excluded = [t.upper() for t in task.get('except_train_numbers') or []]
        allowed_models = task.get('train_models') or ['复兴号', '和谐号', '火车']
        period = task.get('period') or {}
        if (seat not in (task.get('seats') or [])
                or (included and train not in included)
                or train in excluded
                or train_model_category(fields) not in allowed_models
                or not (period.get('from', '00:00') <= fields[8] <= period.get('to', '24:00'))):
            raise BookingError('任务筛选条件已改变，拒绝使用旧余票记录')
        if not self._has_right_filters(job, fields, seat):
            raise BookingError('车次或席别不在任务允许范围内')
        if not self._is_available(fields, seat, len(members)):
            raise BookingError('余票不足，未提交订单')
        user = self._account(account_key)
        if not user or not user.is_ready:
            raise BookingError('12306 账号未登录，请先完成扫码')
        # Membership must come from the account's successfully loaded contacts.
        known = {str(p.get('code') or p.get('passenger_name')) for p in user.passengers}
        known.update(str(p.get('passenger_name')) for p in user.passengers)
        if not known or any(str(m) not in known for m in members):
            raise BookingError('请先加载 12306 常用联系人并重新选择乘车人')

        with self.lock:
            if self.inflight:
                raise BookingError('已有订单正在提交或排队，请等待处理结果')
            if account_key in self.finished_accounts:
                raise BookingError('此账号本次运行已成功下单，为避免重复购票请勿再次提交')
            identity = (account_key, snapshot['date'], fields[3], seat)
            now = time.monotonic()
            if origin == 'auto' and now - self.last_attempts.get(identity, -1e9) < 180:
                raise BookingError('自动下单冷却中')
            if origin == 'auto':
                if account_key in self.auto_paused_accounts:
                    raise BookingError('上次下单未确认成功；请先在 12306 核对订单，再重新启用自动抢票')
                # Only attempt when the persisted rule still explicitly permits it.
                rule = (self._task(job.job_name) or {}).get('auto_order') or {}
                if not rule.get('enabled') or str(rule.get('account_key')) != account_key or list(rule.get('members') or []) != list(members):
                    raise BookingError('自动抢票已取消')
            self.last_attempts[identity] = now
            self.inflight = True
            self.status = {
                'state': 'working', 'mode': origin, 'job_name': job.job_name,
                'train': fields[3], 'seat': seat, 'date': snapshot['date'],
                'message': '正在向 12306 提交订单并等待排队结果',
                'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            }
            data = dict(self.status)

        # Never use the live query Job directly: it is mutated by the query thread.
        order_job = copy.copy(job)
        order_job.ticket_info = list(fields)
        order_job.left_date = snapshot['date']
        order_job.left_station = snapshot['from_name']
        order_job.arrive_station = snapshot['to_name']
        order_job.left_station_code = snapshot['from_code']
        order_job.arrive_station_code = snapshot['to_code']
        order_job.members = list(members)
        order_job.member_num = len(members)
        order_job.member_num_take = len(members)
        order_job.passengers = []
        order_job.set_seat(seat)
        try:
            threading.Thread(target=self._worker, args=(order_job, user, account_key), daemon=True,
                             name='py12306-reservation').start()
        except Exception:
            with self.lock:
                self.inflight = False
                self.status = {'state': 'failed', 'message': '无法启动下单线程'}
            raise BookingError('无法启动下单线程')
        return data

    def _persist_auto_pause(self, account_key):
        """Persist the failure latch first, then disable enabled env.py rules."""
        with self.lock:
            self.auto_paused_accounts.add(str(account_key))
            latch_saved = self._write_paused_accounts(self.auto_paused_accounts)

        try:
            from py12306.web.handler.manage import _lock, _save_jobs
            with _lock:
                current = list(Config().QUERY_JOBS)
                changed = False
                for idx, task in enumerate(current):
                    rule = task.get('auto_order') or {}
                    if rule.get('enabled') and str(rule.get('account_key')) == str(account_key):
                        updated = dict(task)
                        updated['auto_order'] = dict(rule, enabled=False)
                        current[idx] = updated
                        changed = True
                if changed:
                    _save_jobs(current)
            config_saved = True
        except Exception:
            config_saved = False
        return latch_saved, config_saved

    def _worker(self, job, user, account_key):
        result = False
        order = None
        order_id = None
        message = '12306 未确认下单成功，请在官方订单列表核实后再尝试'
        try:
            # Dedicated snapshot of contacts. Avoid UserJob's recursive retry path.
            if not user.passengers:
                raise BookingError('12306 常用联系人未加载')
            job.passengers = user.get_passengers_by_members(job.members)
            if not job.passengers or len(job.passengers) != len(job.members):
                raise BookingError('乘车人解析失败')
            from py12306.order.order import Order
            order = Order(query=job, user=user)
            result = bool(order.order())
            order_id = order.order_id if result else None
            if result and not order_id:
                result = False
                message = '12306 未返回订单号，不能确认购票成功'
            elif result:
                message = '12306 已返回订单号，请尽快在官方渠道支付'
            else:
                stage = getattr(order, 'failure_stage', '') or '未知阶段'
                reason = getattr(order, 'failure_reason', '') or '12306 未确认下单成功'
                message = '{}：{}；没有取得订单号'.format(stage, reason)
        except BookingError as e:
            message = str(e)
        except Exception:
            # A notification failure after queue confirmation must not turn success into failure.
            if order is not None and getattr(order, 'order_id', None) not in (None, 0, '', '0'):
                result = True
                order_id = order.order_id
                message = '12306 已返回订单号，通知可能失败；请及时支付'
            else:
                stage = (getattr(order, 'failure_stage', '') if order else '') or '订单初始化'
                message = '{}阶段发生异常，尚未取得订单号；请先核实 12306 官方订单'.format(stage)
        finally:
            with self.lock:
                if result:
                    self.finished_accounts.add(account_key)
                else:
                    self.auto_paused_accounts.add(account_key)
                    message += '；自动抢票已暂停，核实后可重新启用'
                self.status = {
                    **{k: v for k, v in self.status.items() if k != 'message'},
                    'state': 'success' if result else 'failed',
                    'message': message,
                    'finished_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                if order_id:
                    self.status['order_id'] = str(order_id)
                self.inflight = False
            if not result:
                latch_saved, config_saved = self._persist_auto_pause(account_key)
                with self.lock:
                    if not latch_saved:
                        self.status['message'] += (
                            '；暂停锁无法持久化，重启前请在 Web 手动关闭自动抢票')
                    if not config_saved:
                        self.status['message'] += (
                            '；env.py 自动抢票配置未能关闭，请手动检查')

    def resume_auto(self, account_key):
        with self.lock:
            remaining = self.auto_paused_accounts - {str(account_key)}
            if not self._write_paused_accounts(remaining):
                raise BookingError('无法保存自动抢票的重启安全状态，拒绝重新启用')
            self.auto_paused_accounts = remaining
            self.last_attempts = {k: v for k, v in self.last_attempts.items() if k[0] != account_key}

    def get_status(self):
        with self.lock:
            return dict(self.status)
