"""Only report changes in available seats during anonymous ticket monitoring."""

import shutil
import subprocess
from datetime import datetime

from py12306.helpers.type import SeatType
from py12306.log.query_log import QueryLog


class SeatMonitor:
    def __init__(self):
        # (date, from-code, to-code) -> {(train-id, seat): (label, status)}
        self.snapshots = {}

    def update(self, job, results):
        route = (job.left_date, job.left_station_code, job.arrive_station_code)
        current = {}
        seats = job.allow_seats or list(SeatType.dicts)

        for result in results:
            fields = result.split('|')
            if len(fields) <= max(job.INDEX_TRAIN_NO, job.INDEX_TRAIN_NUMBER,
                                  job.INDEX_LEFT_TIME, job.INDEX_TICKET_NUM):
                continue
            job.ticket_info = fields
            if not job.is_trains_number_valid():
                continue

            train_id = job.get_info_of_train_no()
            train_name = job.get_info_of_train_number()
            departure = job.get_info_of_train_left_time()
            if not train_id:
                continue

            bookable = job.is_has_ticket(fields)
            for seat in seats:
                index = SeatType.dicts.get(seat)
                if index is None or index >= len(fields):
                    continue
                raw = fields[index].strip()
                # Ignore '候补', '无', empty, '0', etc. as immediately available seats.
                available = bookable and (raw == '有' or (raw.isdigit() and int(raw) > 0))
                status = raw if available else '无'
                current[(train_id, seat)] = (f'{train_name} {departure}', status)

        previous = self.snapshots.get(route)
        self.snapshots[route] = current
        if previous is None:
            available = sum(status != '无' for _, status in current.values())
            QueryLog.add_quick_log(
                f'[余票监控就绪] {job.left_date} {job.left_station}→{job.arrive_station}：'
                f'检测 {len(current)} 个车次/席别组合，其中 {available} 个可售；首次不通知'
            ).flush()
            return

        changes = []
        for key, (label, status) in current.items():
            if key in previous:
                before = previous[key][1]
                if before != status:
                    changes.append(f'{label} {key[1]}：{before} → {status}')
            elif status != '无':
                changes.append(f'{label} {key[1]}：新增可售 ({status})')

        for key, (label, before) in previous.items():
            if key not in current and before != '无':
                changes.append(f'{label} {key[1]}：{before} → 车次未返回')

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
