"""Deterministic 12306 Web getQueueCount train_date formatter.

Keep the output independent of the host locale/timezone. The 12306 Chinese
Web client traditionally uses an explicit GMT+0800 date with Chinese timezone
display name; do not use the host machine's datetime locale or UTC offset.
"""
import datetime
import re

_WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
_MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
           'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')


def format_queue_train_date(travel_date):
    """Format YYYY-MM-DD as a Beijing-time JavaScript Date string.

    Only the calendar date is needed for this request. No conversion from
    local time or timezone is performed.
    """
    if not isinstance(travel_date, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', travel_date):
        raise ValueError('invalid travel date')
    try:
        departure = datetime.date.fromisoformat(travel_date)
    except ValueError as error:
        raise ValueError('invalid travel date') from error
    return '{} {} {:02d} {} 00:00:00 GMT+0800 (中国标准时间)'.format(
        _WEEKDAYS[departure.weekday()], _MONTHS[departure.month - 1],
        departure.day, departure.year)
