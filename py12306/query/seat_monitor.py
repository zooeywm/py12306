"""Track seat availability changes and expose a read-only in-memory dashboard."""

import copy
import shutil
import subprocess
import threading
from datetime import datetime

from py12306.helpers.type import SeatType
from py12306.log.query_log import QueryLog


class SeatMonitor:
    def __init__(self):
        # (date, from-code, to-code) -> {(train-id, seat): (label, normalized status)}
        self.snapshots = {}
        self.dashboard = {}
        self._lock = threading.RLock()

    def get_dashboard(self):
        """Return a consistent snapshot without exposing mutable internal state."""
        with self._lock:
            payload = []
            for route in sorted(self.dashboard):
                entry = self.dashboard[route]
                item = {key: copy.deepcopy(value) for key, value in entry.items()
                        if key != 'rows_by_key'}
                item['rows'] = copy.deepcopy(list(entry['rows_by_key'].values()))
                payload.append(item)
            return payload

    def update(self, job, results):
        route = (job.left_date, job.left_station_code, job.arrive_station_code)
        current = {}
        rows = {}
        seats = job.allow_seats or list(SeatType.dicts)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for result in results:
            fields = result.split('|')
            if len(fields) <= max(job.INDEX_TRAIN_NO, job.INDEX_TRAIN_NUMBER,
                                  job.INDEX_LEFT_TIME, job.INDEX_ARRIVE_TIME,
                                  job.INDEX_TICKET_NUM):
                continue
            job.ticket_info = fields
            if not job.is_trains_number_valid():
                continue

            train_id = job.get_info_of_train_no()
            train_name = job.get_info_of_train_number()
            departure = job.get_info_of_train_left_time()
            arrival = fields[job.INDEX_ARRIVE_TIME]
            if not train_id:
                continue

            bookable = job.is_has_ticket(fields)
            for seat in seats:
                index = SeatType.dicts.get(seat)
                if index is None or index >= len(fields):
                    continue
                raw = fields[index].strip()
                available = bookable and (raw == '有' or (raw.isdigit() and int(raw) > 0))
                status = raw if available else '无'
                key = (train_id, seat)
                label = f'{train_name} {departure}'
                current[key] = (label, status)
                rows[key] = {
                    'train': train_name,
                    'departure': departure,
                    'arrival': arrival,
                    'seat': seat,
                    'quantity': raw if bookable and raw else ('不可预订' if not bookable else '无'),
                    'available': bool(available),
                    'change': '',
                    'changed_at': '',
                }

        with self._lock:
            previous = self.snapshots.get(route)
            old_dashboard = self.dashboard.get(route, {})
            old_rows = old_dashboard.get('rows_by_key', {})
            history = list(old_dashboard.get('changes', []))
            changes = []
            events = []

            if previous is not None:
                for key, (label, status) in current.items():
                    if key in previous:
                        before = previous[key][1]
                        if before != status:
                            changes.append(f'{label} {key[1]}：{before} → {status}')
                            description = f'{before} → {status}'
                        else:
                            description = None
                    elif status != '无':
                        changes.append(f'{label} {key[1]}：新增可售 ({status})')
                        description = f'新增可售 ({status})'
                    else:
                        description = None

                    if description:
                        rows[key]['change'] = description
                        rows[key]['changed_at'] = now
                        events.append({'time': now, 'train': rows[key]['train'],
                                       'seat': key[1], 'description': description})
                    elif key in old_rows:
                        rows[key]['change'] = old_rows[key].get('change', '')
                        rows[key]['changed_at'] = old_rows[key].get('changed_at', '')

                for key, (label, before) in previous.items():
                    if key not in current and before != '无':
                        changes.append(f'{label} {key[1]}：{before} → 车次未返回')
                        events.append({'time': now, 'train': label.split(' ')[0],
                                       'seat': key[1], 'description': f'{before} → 车次未返回'})

            self.snapshots[route] = current
            self.dashboard[route] = {
                'date': job.left_date,
                'left': job.left_station,
                'arrive': job.arrive_station,
                'updated_at': now,
                'rows_by_key': rows,
                'changes': (events + history)[:50],
            }
            is_first = previous is None

        if is_first:
            available_count = sum(status != '无' for _, status in current.values())
            QueryLog.add_quick_log(
                f'[余票监控就绪] {job.left_date} {job.left_station}→{job.arrive_station}：'
                f'检测 {len(current)} 个车次/席别组合，其中 {available_count} 个可售；首次不通知'
            ).flush()
            return

        if not changes:
            return

        title = f'12306 余票变化（{len(changes)} 项）'
        QueryLog.add_quick_log(f'[{datetime.now():%H:%M:%S}] {title}\n' + '\n'.join(changes)).flush()
        if shutil.which('notify-send'):
            summary = '\n'.join(changes[:6])
            if len(changes) > 6:
                summary += f'\n另有 {len(changes) - 6} 项变化，详见程序日志'
            try:
                subprocess.Popen(
                    ['notify-send', '-a', 'py12306', title, summary],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                pass
