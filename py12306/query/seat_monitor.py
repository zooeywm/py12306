"""Track seat availability changes and expose a read-only in-memory dashboard."""

import copy
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime

from py12306.helpers.type import SeatType
from py12306.log.query_log import QueryLog


def decode_train_tags(fields):
    """Decode verified positional flags in 12306 leftTicket/query* rows.

    In the UI, classify G/D/C services with a usable dw_flag and without
    a Fuxing flag as Harmony (an inference, not a confirmed trainset model).
    Do not guess pet-transport availability from a train number.
    Field 36 is exchange_train_flag; field 46 is dw_flag (hash-delimited).
    Older payloads with no dw_flag do not receive an inferred model tag.
    """
    def field(index):
        return fields[index].strip() if len(fields) > index else ''

    parts = field(46).split('#')
    def part(index):
        return parts[index] if index < len(parts) else ''

    models = []
    services = []
    if part(1) == '1':
        models.append('复兴号')
    elif field(46) and field(3).upper().startswith(('G', 'D', 'C')):
        models.append('和谐号')  # UI fallback, not an official train model field
    if part(0) == '5':
        models.append('智能动车组')
    if not any(name in models for name in ('复兴号', '和谐号')):
        models.insert(0, '火车')  # Previously unclassified model in the dashboard.
    # Some newer flags encode Q=quiet coach, R=comfort sleeper.
    if part(2).startswith('Q'):
        services.append('静')
    elif part(2).startswith('R'):
        services.append('温馨动卧')
    if field(36) == '1':
        services.append('兑')
    if part(7) and part(7) != 'z':
        services.append('敬')
    if part(6) and part(6) != 'z':
        services.append('铺')
    if part(5) == 'D':
        services.append('动感号')
    return models, services


def train_model_category(fields):
    """Return the mutually exclusive task-filter category for a 12306 train."""
    models, _ = decode_train_tags(fields)
    return next((name for name in ('复兴号', '和谐号') if name in models), '火车')


# The leftTicket/query* response carries journey duration (field 10) and the
# ten-character price blocks of yp_info_new (field 39).  Parsing them locally
# avoids making a price request for each train on every polling cycle.
def format_trip_duration(fields):
    if len(fields) <= 10:
        return '—'
    pieces = fields[10].split(':')
    if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
        return '—'
    hours, minutes = map(int, pieces)
    if minutes >= 60:
        return '—'
    return f'{hours}小时{minutes:02d}分'


def decode_seat_prices(fields):
    """Extract reference fares by seat from yp_info_new; values are yuan strings.

    A block is: one seat code + five digits of price in 0.1 yuan + four
    metadata digits.  Per the 12306 query parser, last four digits >= 3000
    identify standing/no-seat prices, regardless of the first character.
    When multiple fares exist for a seat, display their range rather than
    arbitrarily selecting one. Unknown codes and malformed data stay blank.
    """
    if len(fields) <= 39:
        return {}
    encoded = fields[39].strip()
    if not encoded or len(encoded) % 10:
        return {}
    seat_names = {
        '9': '商务座', 'P': '特等座', 'M': '一等座', 'D': '一等座',
        'O': '二等座', 'S': '二等座', '4': '软卧', 'I': '软卧',
        '3': '硬卧', 'J': '硬卧', 'F': '动卧', '2': '软座',
        '1': '硬座', 'W': '无座',
    }
    fares = {}
    for start in range(0, len(encoded), 10):
        block = encoded[start:start + 10]
        if not (block[1:6].isdigit() and block[6:10].isdigit()):
            continue
        code = 'W' if int(block[6:10]) >= 3000 else block[0]
        seat = seat_names.get(code)
        amount = int(block[1:6])  # integer tenths of a yuan
        if seat and amount > 0:
            fares.setdefault(seat, set()).add(amount)

    def money(tenths):
        return f'¥{tenths // 10}.{tenths % 10}0'

    return {
        seat: (money(min(values)) if len(values) == 1
               else f'{money(min(values))}～{money(max(values))}')
        for seat, values in fares.items()
    }


def decode_price_response(data):
    """Parse human-readable CNY fares from the 12306 per-train price API.

    The API also returns compact numeric duplicates (e.g. '1': '465');
    only the display-oriented keys are used to avoid mixing units.
    """
    if not isinstance(data, dict):
        return {}
    codes = {
        'A9': '商务座', 'P': '特等座', 'M': '一等座',
        'O': '二等座', 'A4': '软卧', 'A3': '硬卧',
        'F': '动卧', 'A2': '软座', 'A1': '硬座',
        'WZ': '无座',
    }
    prices = {}
    for code, seat in codes.items():
        raw = data.get(code)
        if not isinstance(raw, str):
            continue
        value = raw.strip().lstrip('¥￥')
        if re.fullmatch(r'\d+(?:\.\d{1,2})?', value) and float(value) > 0:
            prices[seat] = f'¥{float(value):.2f}'
    return prices


class SeatMonitor:
    def __init__(self):
        # (date, from-code, to-code) -> {(train-id, seat): (label, normalized status)}
        self.snapshots = {}
        self.dashboard = {}
        self._lock = threading.RLock()
        # Keyed by date/train/route segment/seat-types; do not refetch on every poll.
        self._price_cache = {}
        self._next_price_request_at = 0.0

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

    def _missing_prices(self, job, fields):
        """Fill only absent inline fares; at most one price call per 10 seconds."""
        if len(fields) <= 35:
            return {}
        train_no, from_no, to_no, seat_types = (
            fields[2].strip(), fields[16].strip(),
            fields[17].strip(), fields[35].strip(),
        )
        if not all((train_no, from_no, to_no, seat_types)):
            return {}
        cache_key = (job.left_date, train_no, from_no, to_no, seat_types)
        now = time.monotonic()
        cached = self._price_cache.get(cache_key)
        if cached and now < cached[0]:
            return cached[1]
        if now < self._next_price_request_at:
            return cached[1] if cached else {}

        self._next_price_request_at = now + 10  # Do not flood 12306.
        prices = {}
        try:
            response = job.query.session.get(
                'https://kyfw.12306.cn/otn/leftTicket/queryTicketPrice',
                params={
                    'train_no': train_no,
                    'from_station_no': from_no,
                    'to_station_no': to_no,
                    'seat_types': seat_types,
                    'train_date': job.left_date,
                },
                timeout=3,
            )
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict) and payload.get('status') is True:
                    prices = decode_price_response(payload.get('data'))
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # Keep monitoring even if the extra price endpoint is unavailable.
        if not prices and cached:
            prices = cached[1]
        # Cache successes for one hour; failures only retry after ten minutes.
        self._price_cache[cache_key] = (time.monotonic() + (3600 if prices else 600), prices)
        return prices

    def update(self, job, results):
        route = (job.left_date, job.left_station_code, job.arrive_station_code)
        current = {}
        rows = {}
        # Keep the complete queried train list even when this task filters
        # train numbers or departure times. The editor must be able to add
        # trains that are not currently included in the monitored rows.
        train_options = {}
        seats = job.allow_seats or list(SeatType.dicts)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        for result in results:
            fields = result.split('|')
            if len(fields) <= max(job.INDEX_TRAIN_NO, job.INDEX_TRAIN_NUMBER,
                                  job.INDEX_LEFT_TIME, job.INDEX_ARRIVE_TIME,
                                  job.INDEX_TICKET_NUM):
                continue
            train_code = fields[job.INDEX_TRAIN_NUMBER].strip().upper()
            if train_code and re.fullmatch(r'[A-Z0-9]{1,8}', train_code):
                train_options[train_code] = {
                    'train': train_code,
                    'departure': fields[job.INDEX_LEFT_TIME],
                    'arrival': fields[job.INDEX_ARRIVE_TIME],
                }
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
            model_tags, service_tags = decode_train_tags(fields)
            duration = format_trip_duration(fields)
            seat_prices = decode_seat_prices(fields)
            # Conventional K/T/Z trains often lack usable inline fare blocks.
            if any(
                seat not in seat_prices and SeatType.dicts.get(seat, 999) < len(fields)
                and fields[SeatType.dicts[seat]].strip() not in ('', '无', '--', '*')
                for seat in seats
            ):
                for seat, price in self._missing_prices(job, fields).items():
                    seat_prices.setdefault(seat, price)
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
                    'model_tags': model_tags,
                    'service_tags': service_tags,
                    'departure': departure,
                    'arrival': arrival,
                    'duration': duration,
                    'seat': seat,
                    'price': seat_prices.get(seat, '—'),
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
                'train_options': sorted(train_options.values(),
                                        key=lambda item: (item['departure'], item['train'])),
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
